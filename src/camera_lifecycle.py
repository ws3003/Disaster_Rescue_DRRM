"""V4.4 S4 —— 摄像头生命周期统一管理（单进程内单 CameraService 所有权 + 顺序释放）。

提供：
- `CameraServiceSession`：context manager，创建并启动**唯一** CameraService，登记消费者适配器，
  退出时按"消费者 release → service.stop → service.close"逆序释放；异常也保证清理；幂等。
- `RunCameraSharedReader`：供 `src/main.py::run_camera` 使用的鸭子 reader
  （`fps/width/height` + `iter_frames(stride)` → `(idx, ts, BGR)`，V4.3 变换：ROTATE180+copy，不交换通道），
  退订只 release 自身 FrameReader。

说明：本仓库中 main.py(摄像头 demo) 与 e7e8_runner(E8)/edge_client(感知) 是**各自独立进程**；
本模块保证"任一持有摄像头的进程内只有一个 CameraService 且顺序释放"，并由其他进程持有者
（E8 shared/E7）复用同一实例。跨进程共享需 IPC，不在 S4 范围。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Optional, Tuple

from src.camera_service import CameraService

_LOGGER = logging.getLogger("camera_lifecycle")

_DEF = dict(width=1280, height=720, fps=15.0, queue_size=24)


@contextmanager
def camera_session(
    width: int = _DEF["width"],
    height: int = _DEF["height"],
    fps: float = _DEF["fps"],
    queue_size: int = _DEF["queue_size"],
) -> Iterator[CameraService]:
    """上下文：创建/启动唯一 CameraService；正常或异常退出时按序释放。

    用法：
      with camera_session() as service:
          r = subscribe_reader(service)   # 调用方自行 track 并 release
          ...
    """
    service: Optional[CameraService] = None
    try:
        service = CameraService(width=width, height=height, fps=fps, queue_size=queue_size)
        service.start()
        if service.fault():
            raise RuntimeError(f"CameraService 启动失败: {service.fault()}")
        yield service
    finally:
        _shutdown_service(service)


def _shutdown_service(service: Optional[CameraService]) -> None:
    if service is None:
        return
    try:
        service.stop()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("CameraService.stop 异常（继续 close）")
    try:
        service.close()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("CameraService.close 异常")


class RunCameraSharedReader:
    """供 main.py::run_camera 的鸭子 reader（fps/width/height + iter_frames(stride)）。

    - 每次 create 订阅该 CameraService 的 FrameReader。
    - iter_frames(stride) 产 `(frame_idx, ts, BGR)`；保留原始通道并 ROTATE_180 + copy。
    - release() 只解除本订阅，不关服务/摄像头。
    """

    def __init__(self, service: CameraService, width: int, height: int, fps: float) -> None:
        self._frame_reader = service.subscribe()
        self.fps = float(fps)
        self.width = int(width)
        self.height = int(height)

    def iter_frames(self, stride: int = 1):
        stride = max(1, int(stride))
        idx = 0
        while True:
            ok, frame = self._read()
            if not ok or frame is None:
                return
            if idx % stride == 0:
                yield idx, self._reader_ts(), frame
            idx += 1

    def _read(self):
        import cv2
        ok, frame = self._frame_reader.read()
        if not ok or frame is None:
            return False, None
        try:
            bgr = cv2.rotate(frame, cv2.ROTATE_180)
        except Exception:  # noqa: BLE001
            return False, None
        return True, bgr.copy()

    @staticmethod
    def _reader_ts() -> float:
        import time
        return time.monotonic()

    def release(self) -> None:
        self._frame_reader.release()


def release_consumers(*consumers: Any) -> None:
    """逆序释放消费者适配器（后创建先释放）；每个 release 幂等。"""
    for c in reversed(consumers):
        fn = getattr(c, "release", None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("release 消费者失败")
