"""V4.4 B 第一批：EdgeClient 生产入口接入统一 CameraService 的装配/生命周期测试。

Mock PhotoPicCamera2（不连真机）。覆盖：
- `_open_camera`：创建并启动唯一 CameraService，EdgeCam 用同一 service 订阅；不新建第二个 Picamera。
- 初始化失败（start fault）→ 抛 CameraInitError 且清理已创建硬件。
- `_close_camera`：先释放 EdgeCam(EdgeCamAdapter 订阅)，再由同一进程统一 CameraService.stop/close（幂等）。
- 异常/重复关闭不会重复释放硬件。
- EdgeClient 帧来源 = 注入的 CameraService（无独立 CameraFrameReader / 第二个 Picamera）。
"""

from __future__ import annotations

import sys
import time

import pytest


class _FakeCam:
    instances = []

    def __init__(self):
        _FakeCam.instances.append(self)
        self.started = False
        self.closed = False
        self._i = 0

    def create_video_configuration(self, main=None, controls=None):
        self.main = main
        self.controls = controls
        return {"main": main, "controls": controls}

    def configure(self, cfg):
        self.configured = True

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def capture_array(self):
        self._i += 1
        import numpy as np
        return np.zeros((self.main["size"][1], self.main["size"][0], 3),
                        dtype=np.uint8) + (self._i % 255)


@pytest.fixture(autouse=True)
def _fake_picamera(monkeypatch):
    class _Mod:
        def __init__(self):
            self.Picamera2 = _FakeCam

    monkeypatch.setitem(sys.modules, "picamera2", _Mod())
    _FakeCam.instances.clear()


# 让 src.edge_client 可导入（repo 根在 sys.path）
import os
import sys as _sys
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

from src import edge_client as ec  # noqa: E402
from src.camera_service import CameraService  # noqa: E402

_CFG = {"camera": {"width": 8, "height": 6, "fps": 30.0}}


def _frame_of(svc, timeout=2.0):
    """从 service 取一帧（验证硬件在产帧）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if svc.latest() is not None:
            return True
        time.sleep(0.005)
    return svc.latest() is not None


class TestOpenCamera:
    def test_creates_single_camera_service_and_edgecam(self):
        svc, cam = ec._open_camera(_CFG["camera"])
        try:
            assert len(_FakeCam.instances) == 1          # 唯一硬件持有者
            assert isinstance(svc, CameraService)
            assert svc.state == "RUNNING"
            assert svc.session_id >= 1
            _frame_of(svc)
            # EdgeCam 从注入的同一 service 取帧，而非新建自己的读取器实例
            f = cam.capture()
            assert f is not None and f.ndim == 3
        finally:
            ec._close_camera(svc, cam)

    def test_edgecam_uses_injected_service_no_second_picam(self):
        svc, cam = ec._open_camera(_CFG["camera"])
        try:
            _frame_of(svc)
            f = cam.capture()                      # 从 CameraService 的注入帧
            assert f is not None
            assert len(_FakeCam.instances) == 1    # EdgeClient 未另建第二个 Picamera
        finally:
            ec._close_camera(svc, cam)

    def test_start_failure_raises_and_cleans_up(self, monkeypatch):
        # 注入一个 start 后即 fault 的 CameraService：_open_camera 应抛错并清理
        import src.camera_service as cs

        class _Faulting(cs.CameraService):
            def start(self):
                super().start()
                self._state = "FAULT"
                self._fault_reason = "boom"

        monkeypatch.setattr(cs, "CameraService", _Faulting)
        with pytest.raises(ec.CameraInitError, match="boom"):
            ec._open_camera(_CFG["camera"])
        # 硬件被清理：Picamera 已 close（无 RUNNING 残留）
        assert _FakeCam.instances and _FakeCam.instances[-1].closed is True


class TestCloseCamera:
    def test_close_releases_subscription_then_stops_and_closes(self):
        svc, cam = ec._open_camera(_CFG["camera"])
        _frame_of(svc)
        assert svc.state == "RUNNING"
        ec._close_camera(svc, cam)
        assert svc.state == "CLOSED"
        assert _FakeCam.instances[0].closed is True

    def test_delete_subscription_does_not_close_service_until_unified(self):
        # EdgeCam/EdgeCamAdapter.release 只解自身订阅：释放 cam 后 service 仍 RUNNING
        svc, cam = ec._open_camera(_CFG["camera"])
        cam.close()                                # 等价 EdgeCamAdapter.release()
        assert svc.state == "RUNNING"              # 不关 Service / 不关硬件
        assert _FakeCam.instances[0].closed is False
        ec._close_camera(svc, None)                # 统一生命周期再关 Service
        assert svc.state == "CLOSED"

    def test_close_is_idempotent(self):
        svc, cam = ec._open_camera(_CFG["camera"])
        ec._close_camera(svc, cam)
        ec._close_camera(svc, cam)                 # 重复关闭无副作用、不重复释放
        ec._close_camera(svc, None)                # cam 已 gone 也安全
        assert len(_FakeCam.instances) == 1

    def test_exception_in_body_still_cleans_up(self):
        svc, cam = ec._open_camera(_CFG["camera"])
        try:
            raise LookupError("main loop exception")
        except LookupError:
            pass
        finally:
            ec._close_camera(svc, cam)             # main 的 finally 兜底
        assert svc.state == "CLOSED"