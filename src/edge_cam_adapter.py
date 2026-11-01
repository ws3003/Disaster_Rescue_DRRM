"""V4.4 S2 —— EdgeClient 兼容摄像头适配器。

让既有 EdgeClient（其 EdgeCam 依赖 reader 提供 ``iter_frames()``/``stop()``/``width``/``height``，
``capture()`` 取 ``(_,_,frame)`` 第三元）改为从 S1 CameraService 订阅取帧，同时保持 V4.3 原有语义：

   CameraService(RGB888) → FrameReader.read() → 本适配器本地：copy + ROTATE_180（不转通道，与旧 CameraFrameReader 一致）

- 本适配器不拥有/不关闭摄像头生命周期；release()/stop() 仅解除自身订阅（其余订阅者不受影响）。
- 失败（FAULT/CLOSED/读取失败）：iter_frames 以 StopIteration 结束，capture() 返回 None，不伪造、不冒充新帧。
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional, Tuple

from src.camera_service import CameraService, FrameReader

_LOGGER = logging.getLogger("edge_cam_adapter")


class EdgeCamAdapter:
    """把一个已启动的 CameraService 包装成 EdgeCam 兼容 reader（copy + 180° 旋转，不转通道）。

    图像处理与 V4.3 既有 ``src/camera.py::CameraFrameReader`` 完全一致：
    capture_array(RGB888) → 独立副本 → cv2.rotate(ROTATE_180)，**不做 RGB→BGR 转换**。
    这是为保持换用统一 CameraService 后感知帧的通道/旋转语义与旧读取器逐像素等价。
    """

    def __init__(
        self,
        service: CameraService,
        width: int = 1280,
        height: int = 720,
        fps: float = 15.0,
    ) -> None:
        self._service = service
        self._width = int(width)
        self._height = int(height)
        self._fps = float(fps)
        self._reader: FrameReader = service.subscribe()
        self.width = int(width)
        self.height = int(height)

    # ------------------------------------------------------------------
    # 兼容接口（与 src/camera.py CameraFrameReader 的 iter_frames/stop 约定一致）
    # ------------------------------------------------------------------
    def iter_frames(self) -> Iterator[Tuple[Any, Any, Any]]:
        """生成 (_, _, BGR ndarray)。读取失败/服务失效 → 停止（StopIteration）。"""
        while True:
            ok, frame = self._read_bgr()
            if not ok or frame is None:
                return
            yield (None, None, frame)

    def capture(self) -> Optional[Any]:
        """取一帧 BGR ndarray；失败/失效返回 None（不伪造、不冒充新帧）。"""
        ok, frame = self._read_bgr()
        return frame if ok else None

    def _read_bgr(self) -> Tuple[bool, Optional[Any]]:
        ok, frame = self._reader.read()
        if not ok or frame is None:
            return False, None
        try:
            import cv2
            # 与旧 CameraFrameReader 一致：只做 180° 旋转，不做 RGB→BGR 转换
            out = cv2.rotate(frame, cv2.ROTATE_180)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("EdgeCamAdapter 图像旋转失败: %s", exc)
            return False, None
        return True, out.copy()

    def stop(self) -> None:
        """仅释放本订阅；不关闭 CameraService / Picamera2。幂等。"""
        self._reader.release()

    # 便于调用方/测试：等价别称
    def release(self) -> None:
        self.stop()

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