"""X5 真实摄像头录像适配器（`src/evidence/camera_recorder.py`）。

与 `src/evidence/recorder.py` 的录像器接口兼容：`record(seconds, dest_path) -> str`。

设计：
- 捕获后端通过 `capture_factory` 注入（返回"类 cv2.VideoCapture"——有 read()/release()），
  **模块导入时不打开摄像头**；真实 Pi 后端（如 picamera2/OpenCV）在构造时显式传入。
- 按指定时长录制到调用方提供的目标路径；录制后校验文件存在、非空、且容器可读（尽力）。
- 失败：抛 `CameraRecorderError`（不生成伪成功）；best-effort 清理部分输出，并 release 捕获源。

已知边界：真实设备后端（Picamera2 等）尚未在此接入；未连接的构造直接抛 error，不伪称已实现真实录像。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional


class CameraRecorderError(Exception):
    """录像失败（可诊断）。"""


class _BaseCapture:
    """注入的捕获源应提供 read()->(bool, frame) 与 release()。"""

    def read(self) -> tuple[bool, Any]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def release(self) -> None:  # pragma: no cover
        raise NotImplementedError


class CameraRecorder:
    def __init__(
        self,
        *,
        capture_factory: Optional[Callable[[], Any]] = None,
        codec: str = "mp4v",
        fps: float = 20.0,
        verify_stream: bool = True,
    ):
        self._capture_factory = capture_factory
        self._codec = codec
        self._fps = float(fps)
        self._verify = verify_stream

    # ------------------------------------------------------------------
    def _open_capture(self) -> Any:
        if self._capture_factory is None:
            raise CameraRecorderError("未接线摄像头后端（capture_factory 为 None）")
        cap = self._capture_factory()
        if cap is None:
            raise CameraRecorderError("capture_factory 返回 None")
        return cap

    def record(self, seconds, dest_path) -> str:
        cap = self._open_capture()
        writer = None
        dest = str(dest_path)
        wrote = 0
        try:
            ok, frame = cap.read()
            if not ok or frame is None:
                raise CameraRecorderError("录制开始即读不到帧")
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            h, w = int(frame.shape[0]), int(frame.shape[1])
            fourcc = _fourcc(self._codec)
            writer = _open_writer(dest, fourcc, self._fps, w, h)
            if writer is None or not writer.isOpened():
                raise CameraRecorderError("VideoWriter 打开失败")
            writer.write(frame)
            wrote = 1
            end = time.monotonic() + max(0.0, float(seconds))
            while time.monotonic() < end:
                ok2, f2 = cap.read()
                if not ok2 or f2 is None:
                    break
                writer.write(f2)
                wrote += 1
            writer.release()
            writer = None
            if wrote == 0:
                raise CameraRecorderError("未写入任何帧")
            return self._validate(dest)
        except Exception as exc:  # noqa: BLE001
            self._cleanup(dest)
            if isinstance(exc, CameraRecorderError):
                raise
            raise CameraRecorderError(f"录像失败: {exc}") from exc
        finally:
            if writer is not None:
                try:
                    writer.release()
                except Exception:  # noqa: BLE001
                    pass
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass

    def _validate(self, dest: str) -> str:
        p = Path(dest)
        if not p.exists() or p.stat().st_size == 0:
            raise CameraRecorderError(f"输出文件缺失或为空: {dest}")
        if self._verify:
            if not _stream_readable(dest):
                raise CameraRecorderError(f"输出视频流不可读: {dest}")
        return str(p)

    def _cleanup(self, dest: str) -> None:
        try:
            Path(dest).unlink(missing_ok=True)
        except OSError:  # noqa: BLE001
            pass


def _fourcc(codec: str):
    import cv2
    return cv2.VideoWriter_fourcc(*codec)


def _open_writer(dest, fourcc, fps, w, h):
    import cv2
    return cv2.VideoWriter(dest, fourcc, fps, (w, h))


def _stream_readable(dest: str) -> bool:
    import cv2
    vc = cv2.VideoCapture(dest)
    try:
        ok, _ = vc.read()
        n = vc.get(cv2.CAP_PROP_FRAME_COUNT)
        return bool(ok) and n is not None and int(n) >= 1
    finally:
        vc.release()