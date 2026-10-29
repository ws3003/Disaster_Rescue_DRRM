"""X4 云端 YOLO 推理 + 边缘回退（独立模块，不触碰冻结检测链）。"""

from .cloud_yolo import (
    CloudConnectionError,
    CloudError,
    CloudInvalidResponseError,
    CloudTimeoutError,
    CloudUnavailableError,
    CloudYoloClient,
)
from .config import X4Config, load_x4_config
from .contracts import Detection, InferenceResult, validate_detections
from .edge_fallback import EdgeFallbackRouter

__all__ = [
    "CloudConnectionError",
    "CloudError",
    "CloudInvalidResponseError",
    "CloudTimeoutError",
    "CloudUnavailableError",
    "CloudYoloClient",
    "Detection",
    "EdgeFallbackRouter",
    "InferenceResult",
    "X4Config",
    "load_x4_config",
    "validate_detections",
]