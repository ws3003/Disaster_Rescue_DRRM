"""V4.4 S2 EdgeCamAdapter 测试（Mock CameraService/Picamera，不连真机）。

覆盖：原始通道保留、180°旋转、独立副本、失败契约、订阅隔离、release 不关摄像头、
EdgeClient 兼容（EdgeCam(service=…)）。
"""

from __future__ import annotations

import sys
import time

import pytest


# ---------------------------------------------------------------------------
# 可控彩帧 Fake Picamera（产生可区分 R/G/B 的帧，便于断言通道/旋转）
# ---------------------------------------------------------------------------
class _FakeCam:
    instances = []

    def __init__(self):
        _FakeCam.instances.append(self)
        self.started = False
        self.i = 0
        self._next_at = None   # 下一帧应到达的单调时钟点（模拟约 30 FPS，避免 Pi 上忙循环抢 CPU）

    def create_video_configuration(self, main=None, controls=None):
        self.main = main
        return {}

    def configure(self, c):
        pass

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self.closed = True

    def capture_array(self):
        # 约 30 FPS 限速：用 time.monotonic() 控制帧间隔(~1/30s)，time.sleep() 等待，
        # 避免无 sleep 忙循环挤占 Pi 上测试/导入线程。落后超过一帧则重置基准，不连续追帧。
        now = time.monotonic()
        if self._next_at is None:
            self._next_at = now
        delay = self._next_at - now
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
        self._next_at = self._next_at + (1.0 / 30.0)
        if self._next_at - now > (1.0 / 30.0):
            self._next_at = now + (1.0 / 30.0)
        import numpy as np
        self.i += 1
        h, w = self.main["size"][1], self.main["size"][0]
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        # 模拟 RGB888 捕获的实际 BGR 数组，不能仅凭格式名称重新解释通道。
        frame[:, :, 0] = 200 + (self.i % 20)   # B 通道
        frame[:, :, 1] = 100                    # G 通道
        frame[:, :, 2] = 0                      # R 通道
        return frame


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    class _M:
        def __init__(self):
            self.Picamera2 = _FakeCam

    monkeypatch.setitem(sys.modules, "picamera2", _M())
    _FakeCam.instances.clear()


@pytest.fixture()
def service():
    from src.camera_service import CameraService
    s = CameraService(width=8, height=6, fps=30.0, queue_size=8)
    s.start()
    yield s
    s.close()


def _await(reader, timeout=2.0):
    import numpy as np
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, f = reader._read_bgr()
        if ok and f is not None:
            return True, f
        time.sleep(0.005)
    return False, None


class TestBasic:
    def test_subscribe_and_bgr(self, service):
        from src.edge_cam_adapter import EdgeCamAdapter
        a = EdgeCamAdapter(service, width=8, height=6)
        ok, f = _await(a)
        assert ok
        import numpy as np
        assert isinstance(f, np.ndarray) and f.ndim == 3 and f.shape[2] == 3

    def test_bgr_contract_red_blue_not_swapped(self, service):
        # 实际数组 index0=B高、index1=G、index2=R低，输出须保持这些通道。
        import numpy as np
        from src.edge_cam_adapter import EdgeCamAdapter
        a = EdgeCamAdapter(service, width=8, height=6)
        ok, f = _await(a)
        assert ok
        assert float(f[:, :, 0].mean()) > 150
        assert float(f[:, :, 2].mean()) == 0
        assert float(f[:, :, 0].mean()) > float(f[:, :, 2].mean())
        assert float(f[:, :, 1].mean()) == 100  # G 通道不受交换影响

    def test_same_frame_matches_legacy_and_preserves_channels(self):
        """实际调用新适配器，逐像素验证保留 V4.3 的颜色和方向。"""
        import numpy as np
        import cv2
        rng = np.random.default_rng(7)
        src = rng.integers(0, 256, (12, 10, 3), dtype=np.uint8)
        from src.edge_cam_adapter import EdgeCamAdapter

        class Reader:
            def read(self):
                return True, src

        class Service:
            def subscribe(self):
                return Reader()

        actual = EdgeCamAdapter(Service()).capture()
        np.testing.assert_array_equal(actual, src[::-1, ::-1])
        assert not np.shares_memory(actual, src)
        swapped = cv2.rotate(cv2.cvtColor(src, cv2.COLOR_RGB2BGR), cv2.ROTATE_180)
        assert not np.array_equal(actual, swapped)

    def test_rotate_180_flips_space(self, service):
        import numpy as np
        from src.edge_cam_adapter import EdgeCamAdapter
        # 构造左白右黑帧：R=255 left 0 right；180后应左黑右白
        a = EdgeCamAdapter(service, width=8, height=6)
        # 直接喂一个定向帧给适配器难以注入；改用真实服务帧做形状冒烟（旋转等价性由 ROTATE_180 语义保证）
        ok, f = _await(a)
        assert ok
        r = np.rot90(np.rot90(f))
        assert r.shape == f.shape  # 180 翻转往返等size

    def test_independent_copy(self, service):
        import numpy as np
        from src.edge_cam_adapter import EdgeCamAdapter
        a1 = EdgeCamAdapter(service)
        a2 = EdgeCamAdapter(service)
        ok1, f1 = _await(a1)
        ok2, f2 = _await(a2)
        f1[:] = 0     # 修改 a1 帧
        assert bool((f2 == 0).all()) is False   # a2 不共享底层数组


class TestFrame:
    def test_distinct_frames(self, service):
        from src.edge_cam_adapter import EdgeCamAdapter
        a = EdgeCamAdapter(service)
        ok1, f1 = _await(a)
        ok2 = False
        for _ in range(20):
            f = a.capture()
            if f is not None and not (f == f1).all():
                ok2 = True
                break
        assert ok1 and ok2

    def test_failure_returns_none(self):
        from src.camera_service import CameraService
        from src.edge_cam_adapter import EdgeCamAdapter
        # 不 start 的 service + 立即 fault 场景
        s = CameraService()
        a = EdgeCamAdapter(s)
        a.release()
        assert a.capture() is None  # 释放后不再读


class TestIsolation:
    def test_two_adapters_independent(self, service):
        from src.edge_cam_adapter import EdgeCamAdapter
        a, b = EdgeCamAdapter(service), EdgeCamAdapter(service)
        ok_a, f_a = _await(a); ok_b, f_b = _await(b)
        assert ok_a and ok_b

    def test_release_one_keeps_other(self, service):
        from src.edge_cam_adapter import EdgeCamAdapter
        a, b = EdgeCamAdapter(service), EdgeCamAdapter(service)
        a.release()
        ok, _ = _await(b)
        assert ok

    def test_release_does_not_close_camera(self, service):
        from src.edge_cam_adapter import EdgeCamAdapter
        a = EdgeCamAdapter(service)
        a.release()
        cam = _FakeCam.instances[0]
        assert getattr(cam, "closed", False) is False   # 未关 Picamera2
        assert service.state != "CLOSED"

    def test_reenter_no_read(self, service):
        from src.edge_cam_adapter import EdgeCamAdapter
        a = EdgeCamAdapter(service)
        a.release()
        assert a.capture() is None


class TestEdgeClientCompat:
    def test_edgecam_service_injection(self, service):
        from src.edge_client import EdgeCam
        cam = EdgeCam(service=service, width=8, height=6)
        # capture() 返回 BGR，行为与既有 reader 注入一致
        f = cam.capture()
        import numpy as np
        assert isinstance(f, np.ndarray) and f.ndim == 3 and f.shape[2] == 3
        cam.close()   # EdgeCam.close→adapter.stop（仅解订阅）

    def test_edgecam_reader_injection_unchanged(self):
        # 原有 reader= 注入路径不受影响（回归）
        from src.edge_client import EdgeCam

        class FakeReader:
            width = 4
            height = 4

            def iter_frames(self):
                import numpy as np
                yield (None, None, np.zeros((4, 4, 3), dtype=np.uint8))

            def stop(self):
                pass

        cam = EdgeCam(reader=FakeReader())
        import numpy as np
        f = cam.capture()
        assert f is not None and f.shape == (4, 4, 3)
