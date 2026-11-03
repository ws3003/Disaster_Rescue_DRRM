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

# X4 日志专用 SQLite 锁等待秒数（不改变 Database 全局默认 5.0）。
# 正式推理与事件日志写入时显式传入，避免锁等待长时间阻塞检测主链。
_X4_LOG_TIMEOUT = 1.0

# record_event 允许的事件白名单（熔断/恢复探测），拒绝任意非白名单事件名。
_ALLOWED_EVENTS = frozenset({"x4_circuit", "x4_probe"})


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
        log_id = db.save_agent_log(tid, tname, inp, out, latency, timeout=_X4_LOG_TIMEOUT)
        return RecordResult(ok=True, log_id=log_id)
    except Exception as exc:  # noqa: BLE001 —— 记录失败不得中断推理
        _log.warning("x4 record_inference failed: %s", exc)
        return RecordResult(ok=False, log_id=None, reason="write_failed")


def record_event(
    db: Database,
    event: str,
    *,
    task_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    output: Optional[Dict[str, Any]] = None,
    timeout: float = _X4_LOG_TIMEOUT,
) -> RecordResult:
    """记录非推理事件（熔断/恢复探测）到 agent_logs（不新增表/列）。

    - 事件名必须精确命中 `_ALLOWED_EVENTS`（当前 x4_circuit / x4_probe）；其余拒绝。
    - task_id 复用 `_resolved_task_id`（缺省 `x4-<UUID>`；不伪造 frame_id、不做幂等键）。
    - tool_name 使用该合法事件名；input 经 `_safe_input_meta` 清洗；output 缺省空 dict，
      以 `out.setdefault("event", ev)` 保留调用方已提供的 event 字段。
    - 非推理事件 latency_ms 传 None → agent_logs.latency_ms 存 SQL NULL。
    - 数据库写入显式传 timeout（默认 `_X4_LOG_TIMEOUT`）。
    - 异常隔离：任何参数处理/序列化/写库异常均捕获并 logging 一条诊断，返回失败 RecordResult，
      不向调用方传播（不改变 X4 推理异常处理语义）。
    """
    try:
        ev = event if isinstance(event, str) else None
        if ev is None:
            raise ValueError("event must be a string")
        ev = ev.strip()
        if not ev or ev not in _ALLOWED_EVENTS:
            raise ValueError(f"invalid event: {event!r}")
        tid = _resolved_task_id(task_id)
        tname = _normalize_tool_name(ev)
        inp = _safe_input_meta(meta)
        # output 校验：仅接受 dict（或 None→空 dict）；非 dict 视为调用错误并拒绝
        out = dict(output) if isinstance(output, dict) else {}
        out.setdefault("event", ev)
        try:
            log_id = db.save_agent_log(tid, tname, inp, out, None, timeout=timeout)
            return RecordResult(ok=True, log_id=log_id)
        except Exception as exc:  # noqa: BLE001 —— 序列化/写库失败
            _log.warning("x4 record_event failed: %s", exc)
            return RecordResult(ok=False, log_id=None, reason="write_failed")
    except ValueError as exc:
        _log.warning("x4 record_event rejected: %s", exc)
        return RecordResult(ok=False, log_id=None, reason="invalid_event")
    except Exception as exc:  # noqa: BLE001 —— 预留兜底：参数处理异常不得外泄
        _log.warning("x4 record_event rejected (unexpected): %s", exc)
        return RecordResult(ok=False, log_id=None, reason="invalid_event")