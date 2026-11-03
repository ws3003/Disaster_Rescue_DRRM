"""Picamera2 → E8 `CameraRecorder` 捕获源薄适配器（`src/evidence/picamera_capture.py`）。

让树莓派真实摄像头 (Picamera2, OV5647 等) 作为 `CameraRecorder` 的 `capture_factory` 捕获源。

接口：`read() -> (bool, bgr_frame)`、`release()`；构造即打开硬件（延迟导入 picamera2，**模块导入不启动摄像头**）。

关键点：
- Picamera2 RGB888 原始数组直接供 OpenCV 使用；与 V4.3 一致保留通道并旋转 180°，输出 **BGR**。
- 捕获异常 → 抛 `PicameraCaptureError`（可定位），**绝不以制造假帧/假成功**；未打开/已关闭 → `(False, None)`。
- `release()` 幂等；初始化失败时清理已分配资源。
- 供 `CameraRecorder(capture_factory=lambda: PicameraFrameSource(...))` 使用。
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

DEFAULT_WIDTH = 1280          # 与 src/camera.py 默认一致
DEFAULT_HEIGHT = 720
DEFAULT_FPS = 15.0
_FORMAT = "RGB888"


class PicameraCaptureError(Exception):
    """Picamera2 捕获/初始化可定位错误。"""


class PicameraFrameSource:
    def __init__(self, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT,
                 fps: float = DEFAULT_FPS):
        self._w = int(width)
        self._h = int(height)
        self._fps = float(fps)
        self._picam2 = None
        self._closed = False
        self._open()          # 实例初始化时打开硬件（延迟导入 picamera2，模块导入不打开）

    # ------------------------------------------------------------------
    def _open(self) -> None:
        if self._picam2 is not None:
            return
        try:
            import picamera2 as _p
        except Exception as exc:  # noqa: BLE001
            raise PicameraCaptureError(
                f"无法导入 picamera2（请确认运行在 Raspberry Pi 且已 apt 安装 python3-picamera2）: {exc}"
            ) from exc
        cam = None
        try:
            cam = _p.Picamera2()
            cfg = cam.create_video_configuration(
                main={"size": (self._w, self._h), "format": _FORMAT},
                controls={"FrameRate": self._fps},
            )
            cam.configure(cfg)
            cam.start()
        except Exception as exc:  # noqa: BLE001
            self._cleanup(cam)     # 初始化失败：释放已分配资源，不遗留
            raise PicameraCaptureError(f"Picamera2 初始化失败: {exc}") from exc
        self._picam2 = cam
        self._closed = False

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if self._picam2 is None or self._closed:
            return False, None
        try:
            import cv2
            rgb = self._picam2.capture_array()
            # 与 V4.3 CameraFrameReader 同一处理；真机额外交换通道会产生偏蓝。
            bgr = cv2.rotate(rgb, cv2.ROTATE_180)
            return True, np.asarray(bgr, dtype=rgb.dtype)
        except Exception as exc:  # noqa: BLE001 —— 捕获失败必须显式暴露，不伪造帧/成功
            raise PicameraCaptureError(f"Picamera2 捕获失败: {exc}") from exc

    def release(self) -> None:
        cam, self._picam2 = self._picam2, None
        if cam is not None:
            self._cleanup(cam)
        self._closed = True

    @staticmethod
    def _cleanup(cam) -> None:
        if cam is None:
            return
        for method in ("stop", "close"):
            fn = getattr(cam, method, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001 —— 尽力释放，不吞主异常
                    pass

    def __enter__(self) -> "PicameraFrameSource":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
