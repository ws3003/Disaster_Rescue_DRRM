"""X4 云端 YOLO 推理 + 边缘回退：统一结果契约。

坐标/格式约定（与既有 `src/detector.py` 的 Detection 一致）：
- bbox 采用 `(x1, y1, x2, y2)` 像素坐标，与边缘检测器输出完全同空间。
- 云端 / 边缘结果必须遵循同一 Detection / InferenceResult 结构，保证可比对、可回退。

错误码（error_code，无错误为空串 ""）：
  cloud_disabled / cloud_unavailable / cloud_timeout / cloud_connection_error /
  cloud_invalid_response / edge_failed
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence, Tuple

# ---- 错误码 ----
ERROR_NONE = ""
ERROR_CLOUD_DISABLED = "cloud_disabled"
ERROR_CLOUD_UNAVAILABLE = "cloud_unavailable"
ERROR_CLOUD_TIMEOUT = "cloud_timeout"
ERROR_CLOUD_CONNECTION = "cloud_connection_error"
ERROR_CLOUD_INVALID_RESPONSE = "cloud_invalid_response"
ERROR_EDGE_FAILED = "edge_failed"

# 边缘本地模型标识（回退用）
EDGE_MODEL_ID = "disaster_best"


@dataclass(frozen=True)
class Detection:
    """单个检测框。"""

    class_id: int
    class_name: str
    confidence: float
    bbox: Tuple[float, float, float, float]  # (x1, y1, x2, y2) 像素


@dataclass(frozen=True)
class InferenceResult:
    """统一推理结果。"""

    success: bool
    provider: str                 # "cloud" | "edge"
    model_id: str
    detections: Tuple[Detection, ...]
    latency_ms: float
    fallback_used: bool
    error_code: str = ERROR_NONE
    error_message: str = ""

    @property
    def failed(self) -> bool:
        return not self.success


def validate_detections(raw: Any) -> Tuple[Detection, ...]:
    """把任意输入规整为校验通过的 Detection 元组；非法抛 ValueError（调用方归类）。"""
    if raw is None:
        raise ValueError("detections is None")
    if isinstance(raw, (bytes, str, dict)):
        raise ValueError("detections must be a sequence")
    out: list[Detection] = []
    for item in (raw if isinstance(raw, Sequence) else list(raw)):
        d = _coerce(item)
        if not (isinstance(d.class_id, int) and isinstance(d.class_name, str)
                and isinstance(d.confidence, (int, float)) and 0.0 <= float(d.confidence) <= 1.0
                and len(d.bbox) == 4):
            raise ValueError("malformed detection")
        for v in d.bbox:
            if not isinstance(v, (int, float)) or float(v) != float(v):  # 排除 NaN
                raise ValueError("malformed bbox")
        out.append(d)
    return tuple(out)


def _coerce(item: Any) -> Detection:
    if isinstance(item, Detection):
        return item
    if isinstance(item, dict):
        try:
            return Detection(
                class_id=int(item.get("class_id", -1)),
                class_name=str(item.get("class_name", "")),
                confidence=float(item.get("confidence", 0.0)),
                bbox=tuple(float(x) for x in (item.get("bbox") or [])),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("malformed detection") from exc
    # 支持"对象"fetch class_id/class_name/confidence/bbox（如 detector.py 的 Detection）
    try:
        return Detection(
            class_id=int(item.class_id),
            class_name=str(item.class_name),
            confidence=float(item.confidence),
            bbox=tuple(float(x) for x in item.bbox),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("malformed detection") from exc