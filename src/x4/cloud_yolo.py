"""X4 云端 YOLO 推理客户端（可注入；真实远端服务未接入时段以 Fake/未接线处理）。"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .contracts import (
    ERROR_CLOUD_CONNECTION,
    ERROR_CLOUD_INVALID_RESPONSE,
    ERROR_CLOUD_TIMEOUT,
    ERROR_CLOUD_UNAVAILABLE,
    Detection,
    validate_detections,
)


class CloudError(Exception):
    """云端推理可分类错误。"""

    code = ""

    def __init__(self, message: str = ""):
        super().__init__(message or self.code)
        self.message = message or self.code


class CloudTimeoutError(CloudError):
    code = ERROR_CLOUD_TIMEOUT


class CloudConnectionError(CloudError):
    code = ERROR_CLOUD_CONNECTION


class CloudInvalidResponseError(CloudError):
    code = ERROR_CLOUD_INVALID_RESPONSE


class CloudUnavailableError(CloudError):
    code = ERROR_CLOUD_UNAVAILABLE


class CloudYoloClient:
    """把云端推理封装为独立客户端。

    - 输入：BGR 图像（np.ndarray，与边缘检测器一致）。
    - 输出：校验通过的 Detection 元组，或抛 CloudError 子类（超时/连接/无效/不可用）。
    - `infer_func`/`health_func` 可注入（Fake 测试）；未注入则视为未接入真实服务。
    - 不做无限重试：单次调用后由调用方/配置决定是否回退。
    """

    def __init__(
        self,
        *,
        infer_func: Optional[Callable[[Any, Optional[float]], Any]] = None,
        health_func: Optional[Callable[[], bool]] = None,
        model_id: str = "yolov26l",
    ):
        self._infer = infer_func
        self._health = health_func
        self.model_id = model_id

    def infer(self, image_bgr: Any, timeout: Optional[float] = None) -> tuple[Detection, ...]:
        if self._infer is None:
            raise CloudUnavailableError("cloud client not wired")
        try:
            raw = self._infer(image_bgr, timeout)
        except CloudError:
            raise
        except TimeoutError:
            raise CloudTimeoutError() from None
        except OSError:
            raise CloudConnectionError() from None
        except Exception:
            raise CloudConnectionError() from None
        try:
            return validate_detections(raw)
        except ValueError as exc:
            raise CloudInvalidResponseError(str(exc)) from exc

    def health(self) -> bool:
        """健康检查。失败/未接入一律返回 False，不代表下次推理必成功。"""
        if self._health is None:
            return False
        try:
            return bool(self._health())
        except Exception:  # noqa: BLE001
            return False