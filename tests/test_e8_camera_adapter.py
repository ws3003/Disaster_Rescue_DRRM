"""V4.4 S3 —— E8CameraAdapter 测试（Mock Picamera2 & CameraService，不连真机）。

覆盖：原始通道保留、180° 安装方向修正、ndarray/独立副本、
CameraRecorder 兼容（read 契约/多帧录制/释放）、订阅隔离（不 close 摄像头、
Edge/A8 并存、release 幂等）。
"""

from __future__ import annotations

import sys
import time

import pytest


class _DotCam:
    """生成 BGR 不对称彩帧，同时捕捉错误通道交换与方向。"""

    instances = []

    def __init__(self):
        _DotCam.instances.append(self)

    def create_video_configuration(self, main=None, controls=None):
        self.main = main
        return {}

    def configure(self, c):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        self.closed = True

    def capture_array(self):
        import numpy as np
        h, w = self.main["size"][1], self.main["size"][0]
        f = np.zeros((h, w, 3), dtype=np.uint8)
        f[0, 0, 0] = 255          # 左上 B
        f[0, w - 1, 1] = 255      # 右上 G
        f[h - 1, w - 1, 2] = 255  # 右下 R
        return f


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    class _M:
        def __init__(self):
            self.Picamera2 = _DotCam

    monkeypatch.setitem(sys.modules, "picamera2", _M())
    _DotCam.instances.clear()


@pytest.fixture()
def service():
    from src.camera_service import CameraService
    s = CameraService(width=8, height=6, fps=30.0, queue_size=8)
    s.start()
    yield s
    s.close()


def _first(adapter, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, f = adapter.read()
        if ok and f is not None:
            return True, f
        time.sleep(0.005)
    return False, None


class TestConversion:
    def test_subscribes_and_returns_ndarray(self, service):
        from src.e8_camera_adapter import E8CameraAdapter
        a = E8CameraAdapter(service)
        ok, f = _first(a)
        assert ok
        import numpy as np
        assert isinstance(f, np.ndarray) and f.ndim == 3 and f.shape[2] == 3
        assert f.dtype == np.uint8

    def test_preserves_channels_and_corrects_mount_rotation(self, service):
        from src.e8_camera_adapter import E8CameraAdapter
        a = E8CameraAdapter(service)
        ok, f = _first(a)
        assert ok
        import numpy as np
        expected = np.zeros((6, 8, 3), dtype=np.uint8)
        expected[5, 7, 0] = 255  # 蓝色从左上移至右下，通道不变
        expected[5, 0, 1] = 255  # 绿色从右上移至左下
        expected[0, 0, 2] = 255  # 红色从右下移至左上
        np.testing.assert_array_equal(f, expected)

    def test_independent_copy(self, service):
        from src.e8_camera_adapter import E8CameraAdapter
        a1 = E8CameraAdapter(service)
        a2 = E8CameraAdapter(service)
        _, f1 = _first(a1)
        _, f2 = _first(a2)
        f1[:] = 0
        assert bool((f2 == 0).all()) is False


class TestCameraRecorderCompat:
    def test_read_contract(self, service):
        from src.e8_camera_adapter import E8CameraAdapter
        a = E8CameraAdapter(service)
        ok, f = _first(a)
        assert isinstance(ok, bool) and f is not None

    def test_records_multiple_frames(self, service, tmp_path):
        from src.e8_camera_adapter import E8CameraAdapter
        a = E8CameraAdapter(service)
        dest = str(tmp_path / "trace.mp4")
        # 用 E8 适配器直接驱动 CameraRecorder 录多帧（不连真实摄像头）
        from src.evidence.camera_recorder import CameraRecorder
        rec = CameraRecorder(capture_factory=lambda: a, fps=10.0, verify_stream=False)
        out = rec.record(0.5, dest)  # 0.5s@10fps ≈ 5+ 帧
        import os
        assert os.path.exists(out) and os.path.getsize(out) > 0

    def test_failure_after_release(self, service):
        from src.e8_camera_adapter import E8CameraAdapter
        a = E8CameraAdapter(service)
        a.release()
        ok, f = a.read()
        assert ok is False and f is None


class TestIsolation:
    def test_release_only_unsubscribes(self, service):
        from src.e8_camera_adapter import E8CameraAdapter
        a = E8CameraAdapter(service)
        a.release()
        cam = _DotCam.instances[0]
        assert getattr(cam, "closed", False) is False
        assert service.state == "RUNNING"

    def test_e8_and_edge_adapter_cocxist(self, service):
        from src.e8_camera_adapter import E8CameraAdapter
        from src.edge_cam_adapter import EdgeCamAdapter
        e8 = E8CameraAdapter(service)
        ec = EdgeCamAdapter(service)
        _, f8 = _first(e8)
        f2 = ec.capture()   # EdgeCamAdapter 用 capture()（无 read）
        assert f8 is not None and f2 is not None

    def test_e8_release_keeps_edge(self, service):
        from src.e8_camera_adapter import E8CameraAdapter
        from src.edge_cam_adapter import EdgeCamAdapter
        e8 = E8CameraAdapter(service)
        ec = EdgeCamAdapter(service)
        e8.release()
        f = ec.capture()
        assert f is not None

    def test_edge_release_keeps_e8(self, service):
        from src.e8_camera_adapter import E8CameraAdapter
        from src.edge_cam_adapter import EdgeCamAdapter
        e8 = E8CameraAdapter(service)
        ec = EdgeCamAdapter(service)
        ec.release()
        ok, _ = _first(e8)
        assert ok

    def test_release_idempotent(self, service):
        from src.e8_camera_adapter import E8CameraAdapter
        a = E8CameraAdapter(service)
        a.release()
        a.release()  # 幂等安全
        ok, _ = a.read()
        assert ok is False
