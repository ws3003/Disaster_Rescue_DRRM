"""
server/api.py — Disaster_Rescue_DRRM 推理 API Gateway (Stage6)
==============================================================
使 Raspberry Pi Edge Layer 可通过 HTTP 调用 Member A 的 VLM + LLM 链路。

端点:
  POST /api/v1/analyze
    请求: {"image_path": str, "person_events": [{person_id, state, risk_score, priority}]}
    成功: {"success": true, "vlm_output": {}, "llm_output": {}, "error": null}
    失败: 统一结构 success=false, 并返回相应 HTTP 状态码。

内部流程:
  VLMClient(provider="qwen").analyze(image_path)
    → src.vlm.parser.parse() → vlm_output
    → LLMClient(provider="qwen").infer(vlm_output, person_events)
    → src.llm.parser.parse() → llm_output
    → 返回 JSON

未修改 src/vlm、src/llm、schemas、parser、prompts 任何内容。
API Key 继续复用环境变量 VLM_API_KEY / LLM_API_KEY。

运行:
  uvicorn server.api:app --host 0.0.0.0 --port 8000

测试注入: 端点通过 get_clients() 依赖创建客户端; 测试可用
  app.dependency_overrides[get_clients] 注入带 FakeTransport 的客户端,
  从而不调用真实 Qwen API。
"""

from __future__ import annotations

from typing import Tuple

import server.schemas as sch
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
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
    description="Member A VLM + LLM 推理网关 (供 Edge Layer HTTP 调用)",
    version="0.1.0",
)


def get_clients() -> Tuple[VLMClient, LLMClient]:
    """按请求创建真实 Qwen 客户端。

    transport 缺省为 requests.post; 测试通过 dependency_overrides 替换本函数,
    注入 FakeTransport 客户端以离线运行。
    """
    return VLMClient(provider="qwen"), LLMClient(provider="qwen")


def _error_response(status: int, message: str) -> JSONResponse:
    body = sch.fail(message)
    return JSONResponse(status_code=status, content=body.model_dump())


@app.post("/api/v1/analyze", response_model=sch.AnalyzeResponse)
def analyze(
    req: sch.AnalyzeRequest,
    clients: Tuple[VLMClient, LLMClient] = Depends(get_clients),
):
    vlm_client, llm_client = clients

    # [1] VLM 调用 → 标准化 vlm_output
    try:
        vlm_raw = vlm_client.analyze(req.image_path)
        vlm_output = parse_vlm(vlm_raw).to_dict()
    except VLMInputError as exc:
        return _error_response(sch.HTTP_NOT_FOUND, f"图片不可用: {exc}")
    except VLMConfigError as exc:
        return _error_response(sch.HTTP_CONFIG_ERROR, f"VLM 配置错误: {exc}")
    except VLMProviderError as exc:
        return _error_response(sch.HTTP_UPSTREAM_ERROR, f"VLM 调用失败: {exc}")
    except SceneParseError as exc:
        return _error_response(sch.HTTP_UPSTREAM_ERROR, f"VLM 输出解析失败: {exc}")

    # [2] LLM 调用 → 标准化 llm_output
    persons = [p.model_dump() for p in req.person_events]
    try:
        llm_raw = llm_client.infer(vlm_output, persons)
        llm_output = parse_llm(llm_raw).to_dict()
    except LLMConfigError as exc:
        return _error_response(sch.HTTP_CONFIG_ERROR, f"LLM 配置错误: {exc}")
    except LLMProviderError as exc:
        return _error_response(sch.HTTP_UPSTREAM_ERROR, f"LLM 调用失败: {exc}")
    except LLMParseError as exc:
        return _error_response(sch.HTTP_UPSTREAM_ERROR, f"LLM 输出解析失败: {exc}")

    return sch.ok(vlm_output, llm_output)