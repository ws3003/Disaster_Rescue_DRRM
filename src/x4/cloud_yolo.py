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
    - **有界重试归客户端**：`infer` 对瞬时错误（CloudTimeoutError / CloudConnectionError，含映射自
      TimeoutError/OSError/其它异常）在 `max_attempts` 内重试（默认 1=只试一次，行为不变）；
      非瞬时错误（无效/不可用等）立即失败，不重试。服务层不重试，只消费客户端最终结果。
    """

    def __init__(
        self,
        *,
        infer_func: Optional[Callable[[Any, Optional[float]], Any]] = None,
        health_func: Optional[Callable[[], bool]] = None,
        model_id: str = "yolov26l",
        max_attempts: int = 1,
    ):
        # 校验：正整数，显式拒绝 bool/浮点/字符串/0/负值
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0:
            raise ValueError(f"max_attempts 必须为正整数, 得到 {max_attempts!r}")
        self._infer = infer_func
        self._health = health_func
        self.model_id = model_id
        self.max_attempts = int(max_attempts)

    def infer(self, image_bgr: Any, timeout: Optional[float] = None) -> tuple[Detection, ...]:
        if self._infer is None:
            raise CloudUnavailableError("cloud client not wired")
        for attempt in range(self.max_attempts):
            try:
                raw = self._infer(image_bgr, timeout)
            except CloudTimeoutError as exc:
                if attempt == self.max_attempts - 1:
                    raise
                continue                        # 瞬时错误，还有尝试次数
            except CloudConnectionError as exc:
                if attempt == self.max_attempts - 1:
                    raise
                continue
            except TimeoutError:
                if attempt == self.max_attempts - 1:
                    raise CloudTimeoutError() from None
                continue
            except OSError:
                if attempt == self.max_attempts - 1:
                    raise CloudConnectionError() from None
                continue
            except CloudError:
                raise                           # 非瞬时（无效/不可用等）：不重试
            except Exception:
                if attempt == self.max_attempts - 1:
                    raise CloudConnectionError() from None
                continue
            # 成功：校验并返回（已消耗一次尝试；无需再试）
            try:
                return validate_detections(raw)
            except ValueError as exc:
                raise CloudInvalidResponseError(str(exc)) from exc
        raise CloudConnectionError()            # 循环耗尽未返回/未抛（防御性）

    def health(self) -> bool:
        """健康检查。失败/未接入一律返回 False，不代表下次推理必成功。"""
        if self._health is None:
            return False
        try:
            return bool(self._health())
        except Exception:  # noqa: BLE001
            return False