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
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import server.schemas as sch
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
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
from src.storage import upload_snapshot

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


def _save_upload(image: UploadFile) -> Tuple[str, bytes]:
    """读取并校验上传图片, 写入临时文件, 返回 (临时路径, 原始字节)。"""
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
    return path, data


def _save_snapshot(data: bytes) -> Optional[str]:
    """把上传图片原字节保存一份到 data/snapshots/, 返回文件名(或 None)。"""
    try:
        suffix = _detect_suffix(data)
        if suffix is None:
            return None
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_analysis{suffix}"
        (SNAP_DIR / name).write_bytes(data)
        return name
    except Exception:  # noqa: BLE001 — snapshot 保存失败不影响 analyze 主流程
        return None


def _load_storage_base_url() -> Optional[str]:
    """从 config/storage.yaml 读取 storage.local.base_url (公网前缀)。

    不解析完整 storage 配置, 仅取本项目 snapshot 所需字段。
    返回 None 当配置缺失/不可读。
    """
    try:
        import yaml
        cfg_path = Path(__file__).resolve().parents[1] / "config" / "storage.yaml"
        if not cfg_path.exists():
            return None
        with open(cfg_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        local = (data.get("storage", {}) or {}).get("local", {}) or {}
        base = str(local.get("base_url") or "").strip()
        return base.rstrip("/") if base else None
    except Exception:  # noqa: BLE001
        return None


def _snapshot_url(request: Request, filename: str) -> Optional[str]:
    """构造 snapshot 完整 URL。

    优先级:
        1. 环境变量 STORAGE_BASE_URL
        2. config/storage.yaml 的 storage.local.base_url (公网/隧道地址)
        3. 兜底: 当前请求 base_url

    注意: 钉钉等公网服务无法访问局域网 IP, 故必须优先用显式配置的公网地址,
          而不是用请求的局域网 host 覆盖。
    """
    if not filename:
        return None
    base = os.getenv("STORAGE_BASE_URL", "").strip().rstrip("/")
    if not base:
        base = _load_storage_base_url() or ""
    if not base:
        try:
            base = str(request.base_url).rstrip("/")
        except Exception:
            base = ""
    return f"{base}/snapshots/{filename}" if base else None


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


# ----------------------------------------------------------------------
# A→B authoritative observation Contract (Phase 2-B)
# ----------------------------------------------------------------------
# Edge FACT + LLM INFERENCE + LLM ACTION 分离; 人员事实唯一来自 Edge person_events。
_ACTION_ALIAS = {"alert": "send_alert", "notify": "send_alert", "log": "save_event"}
_ALLOWED_ACTIONS = {"send_alert", "save_event", "get_snapshot", "get_ranking", "assess_person"}
# LLM 禁止在 payload 携带的人员事实字段(A 侧一律剥离; 权威以 persons[] 为准)
_PAYLOAD_FORBIDDEN = ("state", "risk_score", "priority", "state_conf", "bbox", "frame_idx")
# 需要有效 person 目标的 action
_REQUIRE_PERSON_TARGET = ("send_alert", "assess_person")


def _iso_now() -> str:
    """observation 层创建时间戳: ISO 8601 带时区。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _build_persons(edge_persons: List[dict]) -> List[dict]:
    """由 Edge person_events 构造 persons[](Edge FACT); 缺失字段一律 null, 不伪造。"""
    out: List[dict] = []
    for p in edge_persons:
        out.append({
            "person_id": p.get("person_id"),
            "state": p.get("state"),
            "state_conf": None,
            "risk_score": p.get("risk_score"),
            "priority": p.get("priority"),
            "bbox": None,
            "frame_idx": None,
        })
    return out


def _sanitize_proposal(proposal: dict, person_ids: set) -> Optional[dict]:
    """规整单条 task_proposal: 对齐 B 词表、剥离 payload 事实字段、校验 target。"""
    if not isinstance(proposal, dict):
        return None
    action = _ACTION_ALIAS.get(str(proposal.get("action") or ""), str(proposal.get("action") or ""))
    if action not in _ALLOWED_ACTIONS:
        return None  # 未知 action 丢弃

    target = proposal.get("target")
    if isinstance(target, bool):
        target = None
    elif isinstance(target, str):
        target = int(target.strip()) if target.strip().lstrip("-").isdigit() else None
    if not isinstance(target, int):
        target = None

    payload = dict(proposal.get("payload") or {})
    for f in _PAYLOAD_FORBIDDEN:
        payload.pop(f, None)  # 剥离 LLM 可能带去的事实字段

    # target 必须是 observation 内 person_id(非数组下标); 需要 person 的 action 不得为 null
    if action in _REQUIRE_PERSON_TARGET and (target is None or str(target) not in person_ids):
        return None
    if target is not None and str(target) not in person_ids:
        return None

    return {"action": action, "target": target, "payload": payload}


def _assemble_contract(observation: dict, persons: List[dict], llm: dict) -> dict:
    """组装 authoritative llm_output = observation + persons(Edge FACT) + LLM(INFERENCE/ACTION)。"""
    person_ids = {str(p["person_id"]) for p in persons if p.get("person_id") is not None}
    proposals: List[dict] = []
    for p in (llm.get("task_proposals") or []):
        s = _sanitize_proposal(p, person_ids)
        if s is not None:
            proposals.append(s)

    return {
        "contract_version": sch.CONTRACT_VERSION,
        "observation": observation,
        "persons": persons,
        "event_summary": llm.get("event_summary", ""),
        "risk_explanations": llm.get("risk_explanations", []),
        "rescue_suggestions": llm.get("rescue_suggestions", []),
        "task_proposals": proposals,
    }


@app.post("/api/v1/analyze", response_model=sch.AnalyzeResponse)
def analyze(
    request: Request,
    image: UploadFile = File(..., description="灾害现场图片 (jpg/jpeg/png)"),
    person_events: Optional[str] = Form(None, description="人员事件 JSON 数组字符串"),
    clients: Tuple[VLMClient, LLMClient] = Depends(get_clients),
):
    vlm_client, llm_client = clients
    temp_path: Optional[str] = None
    snap_filename: Optional[str] = None
    try:
        temp_path, image_data = _save_upload(image)
        persons = _parse_persons(person_events)

        # [A→B Contract] observation layer: 一次 HTTP 请求一个 observation_id; timestamp 由本层创建
        observation = {
            "observation_id": f"obs-{uuid.uuid4().hex}",
            "timestamp": _iso_now(),
            "frame_idx": None,
        }
        persons_facts = _build_persons(persons)  # Edge FACT (缺失字段 null, 不伪造)

        # [1] VLM → 标准化 vlm_output
        vlm_raw = vlm_client.analyze(temp_path)
        vlm_output = parse_vlm(vlm_raw).to_dict()

        # [2] LLM → 标准化 llm_output (INFERENCE + ACTION)
        llm_raw = llm_client.infer(vlm_output, persons)
        llm = parse_llm(llm_raw).to_dict()

        # [3] 组装 authoritative Contract: 由 server 注入 persons(非 LLM), 剥离 payload 事实字段
        llm_output = _assemble_contract(observation, persons_facts, llm)

        # [4] VLM/LLM 成功后: 上传图片保存一份到 data/snapshots/ 供静态访问
        snap_filename = _save_snapshot(image_data)
        # 统一接入 OSS: 上传刚生成的快照取公网 URL; OSS 失败回退本地 snapshot_url
        snap_url = None
        if snap_filename:
            try:
                _snap_file = SNAP_DIR / snap_filename
                if _snap_file.is_file():
                    _up = upload_snapshot(str(_snap_file))
                    if _up.get("ok") and _up.get("url"):
                        snap_url = _up["url"]
            except Exception:  # noqa: BLE001 — OSS 失败仅回退本地, 不影响 analyze 主流程
                snap_url = None
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

    return sch.ok(vlm_output, llm_output,
                  snapshot_url=snap_url if snap_url else _snapshot_url(request, snap_filename))


# ======================================================================
# Snapshot 静态访问 (Phase 2.7 整合)
# ----------------------------------------------------------------------
# 从 server/snapshot_server.py 迁移: 把 data/snapshots/ 作为 HTTP 静态资源
# 暴露, 供 DingTalk picURL 及后续 VLM/LLM 拉取现场图片。
# 与 analyze() 共用同一 FastAPI app, 统一监听 8000。
# ======================================================================

# 图片扩展名白名单
SNAP_ALLOWED_EXT = {".jpg", ".jpeg", ".png"}
# snapshot 根目录 (相对 repo 根; 运行 uvicorn 时 cwd 应为 repo 根)
SNAP_DIR = Path(__file__).resolve().parents[1] / "data" / "snapshots"


@app.get("/snapshots/{filename}")
def get_snapshot(filename: str) -> FileResponse:
    """返回一张 snapshot 图片; 校验扩展名白名单 + 存在性。

    与原 snapshot_server 行为一致(安全白名单 + 防路径穿越), 不影响图片链接。
    """
    if not filename or Path(filename).suffix.lower() not in SNAP_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="仅允许 jpg/jpeg/png 图片")
    if not re.fullmatch(r"[\w\-.]+", filename, flags=re.ASCII):
        raise HTTPException(status_code=400, detail="非法文件名")
    fpath = SNAP_DIR / filename
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail="snapshot 不存在")
    return FileResponse(fpath, media_type="image/jpeg")