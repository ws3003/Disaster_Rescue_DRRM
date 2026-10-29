"""X4 推理调用记录层（复用 `agent_logs` 表与 `save_agent_log` DAO；不改 schema）。

满足 REQ-X4-03 / REQ-E9-04 / REQ-E6-05 的记录需求：
- 复用现有 `agent_logs`（task_id, tool_name, input, output, latency_ms），不新增表/列。
- 每次外围 `EdgeFallbackRouter.infer` 调用记录 1 条最终结果（不拆云端尝试/边缘回退两条）。
- `output` 仅存结构化诊断：provider / model_id / success / detection_count / fallback_used / error_code。
  `input_meta` 仅存安全摘要/业务标识，**不存整图、Token、API Key 或敏感请求内容**。
- task_id：调用方提供的稳定 ID 优先；否则生成 `x4-<UUID>` 隔离。
- 写入失败：不抛给推理调用方、不改原推理结果；经标准 `logging` 记录一条诊断（不递归数据库日志）。
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.database.db import Database
from src.x4.contracts import InferenceResult

_log = logging.getLogger("x4.logging")


@dataclass(frozen=True)
class RecordResult:
    """记录结果：ok=是否成功写入；log_id=写入的 agent_log.id；reason=失败原因(可选)。

    reason ∈ {"latency_invalid", "write_failed", None}。新增 reason 为可选字段，
    旧构造 `RecordResult(ok=..., log_id=...)` 继续有效；ok/log_id 语义不变。
    """

    ok: bool
    log_id: Optional[int] = None
    reason: Optional[str] = None


def _coerce_latency(value: Any) -> Optional[int]:
    """延迟契约：仅接受有限、非负的数值，返回 DAO schema 的整数秒(毫秒)。其余一律 None(无效)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    if not math.isfinite(f) or f < 0.0:
        return None
    return int(round(f))


def _resolved_task_id(task_id: Optional[str]) -> str:
    if task_id is not None:
        s = str(task_id).strip()
        if s:
            return s
    return f"x4-{uuid.uuid4().hex}"


def _normalize_tool_name(tool_name: str) -> str:
    t = str(tool_name).strip() or "x4_infer"
    return t if t.startswith("x4") else f"x4_{t}"


def _safe_input_meta(input_meta: Any) -> Dict[str, Any]:
    """仅保留安全的元数据字典；非 dict/含图像键即忽略，绝不写入敏感/图像内容。"""
    if not isinstance(input_meta, dict):
        return {}
    safe = {}
    for k, v in input_meta.items():
        if not isinstance(v, bytes):  # 禁止字节(可能为图像/凭据负载)
            safe[k] = v
    return safe


def record_inference(
    db: Database,
    result: InferenceResult,
    *,
    task_id: Optional[str] = None,
    tool_name: str = "x4_infer",
    input_meta: Optional[Dict[str, Any]] = None,
    cloud_error: Optional[str] = None,
) -> RecordResult:
    """把一次推理最终结果写入 agent_logs。

    - 调用方已提供稳定/事件/帧 ID → task_id 优先使用；否则生成 `x4-<UUID>`。
    - 严格依据现有 `InferenceResult` 字段构造 output；latency_ms 按 `_coerce_latency` 契约校验。
    - 无效延迟：不调用 DAO，返回 reason="latency_invalid"，经 logging 诊断，不改原始 result。
    - 可选 `cloud_error`：仅在接受"非空稳定错误码字符串"时写入 output["cloud_error"]；
      非法/空/非字符串则安全拒绝该元数据(不写入)。
    - 写入失败：捕获并 logging 一条诊断，返回 ok=False reason="write_failed"（不改原推理结果、不抛给调用方）。
    """
    tid = _resolved_task_id(task_id)
    tname = _normalize_tool_name(tool_name)
    out: Dict[str, Any] = {
        "provider": str(result.provider),
        "model_id": str(result.model_id),
        "success": bool(result.success),
        "detection_count": int(len(result.detections)),
        "fallback_used": bool(result.fallback_used),
        "error_code": str(result.error_code or ""),
    }
    if cloud_error is not None:
        if isinstance(cloud_error, str) and cloud_error.strip():
            out["cloud_error"] = cloud_error
        # 非安全的 cloud_error(非字符串/空) → 安全拒绝该元数据, 不写入
    inp = _safe_input_meta(input_meta)
    latency = _coerce_latency(result.latency_ms)
    if latency is None:
        _log.warning("x4 record_inference rejected: invalid latency_ms=%r", result.latency_ms)
        return RecordResult(ok=False, log_id=None, reason="latency_invalid")
    try:
        log_id = db.save_agent_log(tid, tname, inp, out, latency)
        return RecordResult(ok=True, log_id=log_id)
    except Exception as exc:  # noqa: BLE001 —— 记录失败不得中断推理
        _log.warning("x4 record_inference failed: %s", exc)
        return RecordResult(ok=False, log_id=None, reason="write_failed")