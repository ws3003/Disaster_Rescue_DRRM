"""X4 边缘回退路由器：云端优先，失败按策略回退到本地边缘检测器。"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from .cloud_yolo import CloudError, CloudYoloClient
from .config import X4Config
from .contracts import (
    EDGE_MODEL_ID,
    ERROR_CLOUD_CONNECTION,
    ERROR_CLOUD_DISABLED,
    ERROR_EDGE_FAILED,
    InferenceResult,
    validate_detections,
)


class EdgeFallbackRouter:
    """云端优先、失败回退边缘。

    依赖注入：`cloud`（CloudYoloClient，可 None）与 `edge_detect`（Callable[[image], list]，可 None），
    以**不修改 src/detector.py** 的方式接入既有边缘检测器（生产由调用方包一层 `lambda img: Detector(...).detect_frame(img)`）。
    """

    def __init__(
        self,
        cloud: Optional[CloudYoloClient],
        edge_detect: Optional[Callable[[Any], Any]],
        cfg: X4Config,
        *,
        now: Callable[[], float] = time.monotonic,
    ):
        self.cloud = cloud
        self.edge_detect = edge_detect
        self.cfg = cfg
        self._now = now
        # 实例级"最近一次云端错误码"(仅稳定 CloudError.code, 非异常文本/无敏感)。
        # 每次 infer() 开始重置；仅供外部审计 last_cloud_error；本实例非线程安全，需串行使用。
        self.last_cloud_error = ""

    def health(self) -> bool:
        """云端健康检查（与推理分离）；失败/未配置→False，不伪造成功。"""
        if self.cfg.enabled and self.cloud is not None:
            if self.cfg.cloud_health_enabled:
                return self.cloud.health()
        return False

    def infer(self, image_bgr: Any) -> InferenceResult:
        self.last_cloud_error = ""   # 每次调用起始重置，避免上次结果污染本次
        t0 = self._now()
        cloud_error = ERROR_CLOUD_DISABLED

        if self.cfg.enabled and self.cloud is not None:
            try:
                dets = self.cloud.infer(image_bgr, timeout=self.cfg.request_timeout_seconds)
                return InferenceResult(
                    success=True, provider="cloud", model_id=self.cloud.model_id,
                    detections=dets,
                    latency_ms=self._ms(t0), fallback_used=False,
                )
            except CloudError as exc:
                cloud_error = exc.code
            except Exception:  # noqa: BLE001——云端异常不得吞成"空检测成功"
                cloud_error = ERROR_CLOUD_CONNECTION
            # 仅记录稳定错误码(云端成功会在上面 return, 不会走到此处)
            self.last_cloud_error = cloud_error

        # 回退到边缘
        if self.cfg.fallback_enabled and self.edge_detect is not None:
            try:
                raw = self.edge_detect(image_bgr)
                dets = validate_detections(raw)
                return InferenceResult(
                    success=True, provider="edge", model_id=EDGE_MODEL_ID,
                    detections=dets,
                    latency_ms=self._ms(t0), fallback_used=True,
                )
            except Exception as exc:  # noqa: BLE001
                return InferenceResult(
                    success=False, provider="edge", model_id=EDGE_MODEL_ID,
                    detections=(), latency_ms=self._ms(t0), fallback_used=True,
                    error_code=ERROR_EDGE_FAILED, error_message=str(exc),
                )

        # 云端不可用 / 被禁用，且无回退（或回退失败已在上方返回）
        return InferenceResult(
            success=False,
            provider="cloud" if (self.cfg.enabled and self.cloud is not None) else "cloud",
            model_id=(self.cloud.model_id if self.cloud is not None else ""),
            detections=(), latency_ms=self._ms(t0), fallback_used=False,
            error_code=cloud_error,
            error_message="edge fallback disabled or unavailable",
        )

    def _ms(self, t0: float) -> float:
        return round((self._now() - t0) * 1000.0, 1)