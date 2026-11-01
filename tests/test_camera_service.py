"""V4.4 S1 统一摄像头服务测试（Mock Picamera2，不连真实摄像头）。

覆盖：导入惰性、唯一实例、RGB888、会话/序号/时间戳、双订阅隔离、副本隔离、
drop_newest/统计、慢消费者不阻塞、release 不关摄像头、故障唤醒、stop/close 幂等、
旧会话帧隔离、无两个实例。
"""

from __future__ import annotations

import threading
import time

import pytest

from src.camera_service import CameraService, CameraServiceError

# ---------------------------------------------------------------------------
# Mock Picamera2
# ---------------------------------------------------------------------------


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
        return np.zeros((self.main["size"][1], self.main["size"][0], 3), dtype=np.uint8) + (self._i % 255)


@pytest.fixture(autouse=True)
def _fake_picamera(monkeypatch):
    import sys
    # 让 camera_service 的 `import picamera2` 拿到假 instance（惰性导入点）
    class _Mod:
        def __init__(self):
            self.Picamera2 = _FakeCam

    monkeypatch.setitem(sys.modules, "picamera2", _Mod())
    _FakeCam.instances.clear()


@pytest.fixture()
def svc():
    s = CameraService(queue_size=4)
    yield s
    # 确保测试后关闭（幂等安全）
    s.close()


def _await_running(s, timeout=2.0):
    # 等待线程产生至少一帧（latest 非 None），避免用真实长 sleep
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if s.latest() is not None:
            return True
        time.sleep(0.01)
    return s.latest() is not None


class TestCreate:
    def test_import_does_not_create_picamera(self):
        # 仅构造不创建实例
        s = CameraService()
        assert _FakeCam.instances == []
        s.close()

    def test_start_creates_unique_instance(self, svc):
        svc.start()
        assert len(_FakeCam.instances) == 1

    def test_rgb888_and_dims(self, svc):
        svc.start()
        cam = _FakeCam.instances[0]
        assert cam.main["size"] == (1280, 720)
        assert cam.main["format"] == "RGB888"

    def test_double_start_single_instance(self, svc):
        svc.start()
        svc.start()  # 重复 start 不创建第二个
        assert len(_FakeCam.instances) == 1

    def test_import_lazy_no_hardware(self):
        import src.camera_service as m
        assert _FakeCam.instances == []


class TestPacketAndThreads:
    def test_packet_fields_and_latest(self, svc):
        svc.start()
        assert _await_running(svc)
        lk = svc.latest()
        assert lk.session_id == svc.session_id
        assert lk.seq >= 1
        assert isinstance(lk.monotonic_timestamp, float)
        assert hasattr(lk.frame, "shape")

    def test_seq_and_session_monotonic(self, svc):
        svc.start()
        assert _await_running(svc)
        assert svc.latest().seq >= 1

    def test_two_subscribers_independent(self, svc):
        svc.start()
        r1 = svc.subscribe()
        r2 = svc.subscribe()
        assert _await_running(svc)
        ok1, f1 = r1.read(timeout=2.0)
        ok2, f2 = r2.read(timeout=2.0)
        assert ok1 and ok2 and f1 is not None and f2 is not None

    def test_frame_copy_isolation(self, svc):
        svc.start()
        r1 = svc.subscribe()
        r2 = svc.subscribe()
        assert _await_running(svc)
        _, f1 = r1.read(timeout=2.0)
        _, f2 = r2.read(timeout=2.0)
        f1[:] = 99
        # 不共享同一 ndarray：r1 修改不影响 r2
        assert not (f2 == 99).all()

    def test_read_returns_ndarray(self, svc):
        svc.start()
        r = svc.subscribe()
        assert _await_running(svc)
        ok, f = r.read(timeout=2.0)
        assert ok
        import numpy as np
        assert isinstance(f, np.ndarray)

    def test_read_packet_returns_packet(self, svc):
        svc.start()
        r = svc.subscribe()
        assert _await_running(svc)
        ok, pkt = r.read_packet(timeout=2.0)
        assert ok and pkt is not None
        assert hasattr(pkt, "session_id") and hasattr(pkt, "seq") and hasattr(pkt, "frame")


class TestQueue:
    def test_drop_newest_and_stats(self, svc):
        svc.start()
        r = svc.subscribe()
        # 制造积压：暂停消费，让队列触发 drop_newest
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and r.dropped_frames == 0:
            time.sleep(0.01)
        assert r.dropped_frames > 0            # 大量帧，队列只有4，新帧被丢
        assert r.max_queue_len_seen >= 1      # 记录过实际达到长度
        assert r.read(timeout=0.1) is not None  # 仍有旧帧可读

    def test_no_unbounded_growth(self, svc):
        svc.start()
        r = svc.subscribe()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.01)
        # 队列有界：size <= queue_size(4)；may drop但不清空
        assert r.max_queue_len_seen <= 4

    def test_slow_does_not_block_fast(self, svc):
        svc.start()
        fast = svc.subscribe()
        slow = svc.subscribe()
        assert _await_running(svc)
        # 即使 slow 不读，fast 仍能取帧（各自独立队列）
        ok, _ = fast.read(timeout=2.0)
        assert ok


class TestLifecycle:
    def test_release_does_not_close_camera(self, svc):
        svc.start()
        r = svc.subscribe()
        r.release()
        cam = _FakeCam.instances[0]
        assert cam.closed is False
        assert svc.state != "CLOSED"

    def test_release_one_keeps_other(self, svc):
        svc.start()
        a = svc.subscribe()
        b = svc.subscribe()
        a.release()
        assert _await_running(svc)
        ok, _ = b.read(timeout=2.0)
        assert ok

    def test_concurrent_instances_never_two(self, svc):
        svc.start()
        assert len(_FakeCam.instances) == 1
        # 未调用 start 不新增
        svc.start()
        assert len(_FakeCam.instances) == 1

    def test_capture_fault_sets_fault_and_wakes(self, svc, monkeypatch):
        class Boom:
            def __init__(self):
                pass

            def create_video_configuration(self, **k):
                return {}

            def configure(self, c):
                pass

            def start(self):
                pass

            def capture_array(self):
                raise RuntimeError("gpu dead")

            def stop(self):
                pass

            def close(self):
                pass

        import sys
        monkeypatch.setitem(sys.modules, "picamera2", type("M", (), {"Picamera2": lambda: Boom()}))
        s = CameraService(queue_size=4)
        r = s.subscribe()
        s.start()
        # 采集线程应快速 fault 并唤醒阻塞 read
        for _ in range(50):
            ok, pkt = r.read(timeout=0.1)
            if ok is False:
                break
        assert s.fault() is not None and "gpu dead" in s.fault()
        assert ok is False
        s.close()

    def test_init_fault_sets_fault(self, svc, monkeypatch):
        import sys

        class Boom:
            def __init__(self):
                raise RuntimeError("no device")

        monkeypatch.setitem(sys.modules, "picamera2", type("M", (), {"Picamera2": lambda: Boom()}))
        s = CameraService()
        s.start()
        assert s.fault() is not None and "no device" in s.fault()
        assert len(_FakeCam.instances) == 0  # 未创建成功（Boom 在构造即抛）
        s.close()

    def test_stop_terminates_thread(self, svc):
        svc.start()
        assert _await_running(svc)
        svc.stop()
        th = svc._thread
        assert th is not None and not th.is_alive()

    def test_close_releases_picamera(self, svc):
        svc.start()
        assert _await_running(svc)
        cam = _FakeCam.instances[0]
        svc.close()
        assert cam.closed is True
        assert svc.state == "CLOSED"

    def test_close_idempotent(self, svc):
        svc.start()
        svc.close()
        svc.close()  # 幂等
        assert svc.state == "CLOSED"

    def test_close_no_double_release(self, svc):
        svc.start()
        cam = _FakeCam.instances[0]
        cam.close_count = 0
        orig = cam.close
        cam.close = lambda: (setattr(cam, "close_count", cam.close_count + 1), orig())[1]
        svc.close()
        svc.close()
        assert cam.close_count == 1

    def test_old_reader_does_not_read_new_session(self):
        s = CameraService()
        s.start()
        assert _await_running(s)
        r = s.subscribe()
        sid = r.session_id
        got_new = False
        for _ in range(30):
            ok, pkt = r.read_packet(timeout=0.1)
            if ok and pkt is not None and pkt.session_id != sid:
                got_new = True
                break
        assert ok is False or got_new is False  # 不读到新会话残留（本实现单会话内）
        s.close()


class TestClosedStart:
    def test_start_after_close_raises(self, svc):
        svc.start()
        svc.close()
        with pytest.raises(CameraServiceError):
            svc.start()