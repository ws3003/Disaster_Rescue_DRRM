"""V4.4 S3 —— E8 视频取证共享帧适配器。

让 E8（CameraRecorder 的 capture source）改从 S1 CameraService 订阅取帧，而非自建第二个 Picamera2。

转换（保持 E8 原有方向语义：无 180° 旋转）：
  CameraService(RGB888) → FrameReader.read() → 本适配器本地 RGB→BGR + copy。绝不 ROTATE_180。

接口完全兼容 CameraRecorder 期望的 capture source：
- read() -> (bool, ndarray)   （VideoWriter 需要的 BGR 帧；成功必为独立副本）
- release()                   （只解除自身订阅；绝不 close CameraService / Picamera2）

失败/FAULT/CLOSED：read() 返回 (False, None)，不无限阻塞，不伪造帧、不冒充新帧。
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from src.camera_service import CameraService, FrameReader

_LOGGER = logging.getLogger("e8_camera_adapter")


class E8CameraAdapter:
    """一个已启动 CameraService 的 E8 专用订阅读句柄（CameraRecorder 兼容）。"""

    def __init__(self, service: CameraService) -> None:
        self._service = service
        self._reader: FrameReader = service.subscribe()

    def read(self) -> Tuple[bool, Optional[Any]]:
        """返回 (ok, bgr ndarray)；失败/失效返回 (False, None)。不旋转。"""
        ok, frame = self._reader.read()
        if not ok or frame is None:
            return False, None
        try:
            import cv2
            bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("E8 图像转换失败: %s", exc)
            return False, None
        return True, bgr.copy()

    def release(self) -> None:
        """解除本订阅；不关闭 CameraService / Picamera2。幂等。"""
        self._reader.release()

    # ------------------------------------------------------------------
    @property
    def dropped_frames(self) -> int:
        return self._reader.dropped_frames

    @property
    def max_queue_len_seen(self) -> int:
        return self._reader.max_queue_len_seen

    @property
    def session_id(self) -> int:
        return self._reader.session_id