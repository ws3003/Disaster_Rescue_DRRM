"""
server/api.py — Disaster_Rescue_DRRM 推理 API Gateway (Stage7-A: 图片上传化)
============================================================================
使 Raspberry Pi Edge Layer 可通过 HTTP 上传灾害图片并调用 Member A 的
VLM + LLM 链路。改用 multipart/form-data, 适配"树莓派与服务器不同文件系统"
的真实部署(不再依赖 image_path 本机路径)。

端点:
  POST /api/v1/analyze  (multipart/form-data)
    字段:
      image          文件类型 (jpg/jpeg/png), UploadFile
      person_events  人员事件 JSON 字符串 (可省略)

处理流程:
  multipart 上传 → 校验图片(空/非图片/不可读) → 存临时文件 server/uploads/tmp_*.ext
  → VLMClient(provider="qwen").analyze(tmp_path) → vlm parser → vlm_output
  → LLMClient(provider="qwen").infer(vlm_output, person_events) → llm parser
  → 返回统一 JSON; finally 删除临时文件。

HTTP 状态码: 200 成功 / 400 图片错误 / 422 参数错误 / 500 缺 Key / 502 模型或解析失败。
未修改 src/vlm、src/llm、schemas、parser、prompts 任何内容。
API Key 复用环境变量 VLM_API_KEY / LLM_API_KEY。

运行:
  uvicorn server.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import os
import uuid
from typing import List, Optional, Tuple

import server.schemas as sch
from fastapi import Depends, FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from src.llm.client import LLMClient, LLMConfigError, LLMProviderError
from src.llm.parser import LLMParseError, parse as parse_llm
from src.vlm.client import (
    VLMClient,
    VLMConfigError,
    VLMInputError,
    VLMProviderError,
)
from src.vlm.parser import SceneParseError, parse as parse_vlm

app = FastAPI(
    title="Disaster_Rescue_DRRM Inference API",
    description="Member A VLM + LLM 推理网关 (multipart 图片上传, 供 Edge Layer HTTP 调用)",
    version="0.2.0",
)

# 临时上传目录 (运行时创建; 已加入 .gitignore)
_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")

# 支持的图片后缀 (内容按魔数识别, 不信任客户端后缀)
_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
}


class ApiImageError(ValueError):
    """图片错误: 空文件 / 非图片 / 不可读 → HTTP 400。"""


class ApiRequestError(ValueError):
    """请求参数错误: person_events 非法 → HTTP 422。"""


def get_clients() -> Tuple[VLMClient, LLMClient]:
    """按请求创建真实 Qwen 客户端 (transport 缺省 requests.post)。

    测试通过 app.dependency_overrides[get_clients] 注入 FakeTransport 客户端。
    """
    return VLMClient(provider="qwen"), LLMClient(provider="qwen")


# ----------------------------------------------------------------------
# 上传图片处理
# ----------------------------------------------------------------------
def _detect_suffix(data: bytes) -> Optional[str]:
    for magic, suffix in _IMAGE_MAGIC.items():
        if data.startswith(magic):
            return suffix
    return None  # 非支持的图片


def _save_upload(image: UploadFile) -> str:
    """读取并校验上传图片, 写入临时文件, 返回临时路径。"""
    data = image.file.read()
    if not data:
        raise ApiImageError("图片文件为空")
    suffix = _detect_suffix(data)
    if suffix is None:
        raise ApiImageError(f"非支持的图片类型或无法读取 (仅支持 jpg/jpeg/png)")
    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    path = os.path.join(_UPLOAD_DIR, f"tmp_{uuid.uuid4().hex}{suffix}")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _cleanup_temp(path: Optional[str]) -> None:
    if path:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass  # 清理失败不阻断响应


# ----------------------------------------------------------------------
# person_events 解析
# ----------------------------------------------------------------------
def _parse_persons(raw: Optional[str]) -> List:
    """解析 multipart 字段 person_events (JSON 字符串) 为 list[dict]。"""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiRequestError(f"person_events 不是合法 JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ApiRequestError("person_events 需为 JSON 数组")
    persons: List[dict] = []
    try:
        for item in data:
            persons.append(sch.PersonEvent.model_validate(item).model_dump())
    except ValidationError as exc:
        raise ApiRequestError(f"person_events 字段不符: {exc}") from exc
    return persons


def _error_response(status: int, message: str) -> JSONResponse:
    body = sch.fail(message)
    return JSONResponse(status_code=status, content=body.model_dump())


@app.post("/api/v1/analyze", response_model=sch.AnalyzeResponse)
def analyze(
    image: UploadFile = File(..., description="灾害现场图片 (jpg/jpeg/png)"),
    person_events: Optional[str] = Form(None, description="人员事件 JSON 数组字符串"),
    clients: Tuple[VLMClient, LLMClient] = Depends(get_clients),
):
    vlm_client, llm_client = clients
    temp_path: Optional[str] = None
    try:
        temp_path = _save_upload(image)
        persons = _parse_persons(person_events)

        # [1] VLM → 标准化 vlm_output
        vlm_raw = vlm_client.analyze(temp_path)
        vlm_output = parse_vlm(vlm_raw).to_dict()

        # [2] LLM → 标准化 llm_output
        llm_raw = llm_client.infer(vlm_output, persons)
        llm_output = parse_llm(llm_raw).to_dict()
    except ApiImageError as exc:
        return _error_response(sch.HTTP_IMAGE_ERROR, f"图片错误: {exc}")
    except ApiRequestError as exc:
        return _error_response(sch.HTTP_UNPROCESSABLE, f"请求参数错误: {exc}")
    except VLMInputError as exc:
        return _error_response(sch.HTTP_IMAGE_ERROR, f"图片错误: {exc}")
    except VLMConfigError as exc:
        return _error_response(sch.HTTP_CONFIG_ERROR, f"VLM 配置错误: {exc}")
    except LLMConfigError as exc:
        return _error_response(sch.HTTP_CONFIG_ERROR, f"LLM 配置错误: {exc}")
    except VLMProviderError as exc:
        return _error_response(sch.HTTP_UPSTREAM_ERROR, f"VLM 调用失败: {exc}")
    except LLMProviderError as exc:
        return _error_response(sch.HTTP_UPSTREAM_ERROR, f"LLM 调用失败: {exc}")
    except SceneParseError as exc:
        return _error_response(sch.HTTP_UPSTREAM_ERROR, f"VLM 输出解析失败: {exc}")
    except LLMParseError as exc:
        return _error_response(sch.HTTP_UPSTREAM_ERROR, f"LLM 输出解析失败: {exc}")
    finally:
        _cleanup_temp(temp_path)

    return sch.ok(vlm_output, llm_output)