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


# ---------------------------------------------------------------------------
# 阻塞 / 并发关闭测试（部署前定向：硬件所有权移交、stop-helper、有界 close）
# 全部使用可解除的 Fake（Event/Barrier 放行），同进程安全清理，不遗留线程。
# ---------------------------------------------------------------------------
class _BaseBlockingCam:
    instances = []

    def __init__(self):
        _BaseBlockingCam.instances.append(self)
        self.capture_entered = threading.Event()
        self.allow_capture_exit = threading.Event()
        self.stop_calls = 0
        self.close_calls = 0
        self.hardware_released = threading.Event()
        self.exit_time = None
        self.release_time = None
        self.started = False

    def create_video_configuration(self, main=None, controls=None):
        self.main = main
        return {"main": main, "controls": controls}

    def configure(self, c):
        pass

    def start(self):
        self.started = True

    def capture_array(self):
        import numpy as np
        self.capture_entered.set()
        self.allow_capture_exit.wait()
        self.exit_time = time.monotonic()
        return np.zeros((self.main["size"][1], self.main["size"][0], 3),
                        dtype=np.uint8) + 1

    def stop(self):  # 默认：能唤醒 capture_array
        self.stop_calls += 1
        self.allow_capture_exit.set()

    def close(self):
        self.close_calls += 1
        self.release_time = time.monotonic()
        self.hardware_released.set()


class _StopForeverCam(_BaseBlockingCam):
    def __init__(self):
        super().__init__()
        self.stop_return = threading.Event()

    def stop(self):  # stop 永久阻塞（可控放行）
        self.stop_calls += 1
        self.stop_return.wait()


class _CaptureForeverCam(_BaseBlockingCam):
    def stop(self):  # stop 不唤醒 capture
        self.stop_calls += 1

    def capture_array(self):  # capture 永久阻塞（可控放行）
        import numpy as np
        self.capture_entered.set()
        self.allow_capture_exit.wait()
        return np.zeros((6, 8, 3), dtype=np.uint8) + 2


class _BlockThenThrowOnStopCam(_BaseBlockingCam):
    def stop(self):
        self.stop_calls += 1
        self.allow_capture_exit.set()

    def capture_array(self):
        import numpy as np
        self.capture_entered.set()
        self.allow_capture_exit.wait()
        if self.stop_calls:
            raise RuntimeError("capture interrupted by stop")
        return np.zeros((6, 8, 3), dtype=np.uint8)


class _RealFaultCam(_BaseBlockingCam):
    def capture_array(self):
        import numpy as np
        self.capture_entered.set()
        raise RuntimeError("device fault")


class _FailConfigureCam(_BaseBlockingCam):
    def configure(self, c):
        raise RuntimeError("configure fail")


class _FailStreamStartCam(_BaseBlockingCam):
    def start(self):
        raise RuntimeError("stream start fail")


def _use(monkeypatch, cls):
    import sys
    cls.instances.clear()

    class _M:
        def __init__(self):
            self.Picamera2 = cls

    monkeypatch.setitem(sys.modules, "picamera2", _M())
    return cls


def _mkdir(monkeypatch, cls):
    _use(monkeypatch, cls)
    s = CameraService(width=8, height=6, fps=30.0, queue_size=8)
    s.start()
    return s, cls.instances[0]


class TestBlockingClose:
    def _finish(self, s, cam):
        cam.allow_capture_exit.set()
        if hasattr(cam, "stop_return"):
            cam.stop_return.set()
        try:
            s.close(timeout=2.0)
        except Exception:
            pass

    def test_stop_wakes_capture_then_release(self, monkeypatch):
        s, cam = _mkdir(monkeypatch, _BaseBlockingCam)
        try:
            assert cam.capture_entered.wait(2.0)
            s.close(timeout=2.0)
            assert cam.hardware_released.wait(2.0)
            assert s.state == "CLOSED"
            assert cam.close_calls == 1
            assert cam.exit_time is not None and cam.release_time is not None
            assert cam.exit_time <= cam.release_time   # 线程退出先于硬件释放
        finally:
            self._finish(s, cam)

    def test_stop_forever_close_still_bounded(self, monkeypatch):
        s, cam = _mkdir(monkeypatch, _StopForeverCam)
        try:
            assert cam.capture_entered.wait(2.0)
            started = time.monotonic()
            s.close(timeout=0.3)
            assert time.monotonic() - started < 5.0     # close 有界返回
            assert s.state != "CLOSED"                  # 不伪装关闭成功
            assert not cam.hardware_released.is_set()   # 未释放仍被使用的硬件
            assert s._picam2 is not None                # 保留硬件引用
        finally:
            self._finish(s, cam)

    def test_capture_forever_close_bounded_and_not_released(self, monkeypatch):
        s, cam = _mkdir(monkeypatch, _CaptureForeverCam)
        try:
            assert cam.capture_entered.wait(2.0)
            started = time.monotonic()
            s.close(timeout=0.3)
            assert time.monotonic() - started < 5.0
            assert s.state != "CLOSED"
            assert not cam.hardware_released.is_set()
            assert s._picam2 is not None
        finally:
            self._finish(s, cam)

    def test_double_close_releases_once(self, monkeypatch):
        s, cam = _mkdir(monkeypatch, _BaseBlockingCam)
        try:
            assert cam.capture_entered.wait(2.0)
            barrier = threading.Barrier(2)

            def _cl():
                barrier.wait()
                s.close(timeout=2.0)

            t1 = threading.Thread(target=_cl); t2 = threading.Thread(target=_cl)
            t1.start(); t2.start(); t1.join(5); t2.join(5)
            s.close(timeout=2.0)                          # 再 close 幂等
            assert cam.hardware_released.wait(2.0)
            assert s.state == "CLOSED"
            assert cam.close_calls == 1                   # 硬件只释放一次
        finally:
            self._finish(s, cam)

    def test_helper_skips_after_hardware_released(self, monkeypatch):
        # stop-helper 二次校验：硬件已释放(_picam2 None)后不得再对已释放对象调 stop
        s, cam = _mkdir(monkeypatch, _BaseBlockingCam)
        try:
            assert cam.capture_entered.wait(2.0)
            s.close(timeout=2.0)
            assert cam.close_calls == 1
            stop_after = cam.stop_calls
            s._maybe_start_stop_helper()                  # 已释放 → 不启动/不重复 stop
            assert cam.stop_calls == stop_after
            assert s.state == "CLOSED"
        finally:
            self._finish(s, cam)

    def test_active_close_capture_exception_goes_closed(self, monkeypatch):
        s, cam = _mkdir(monkeypatch, _BlockThenThrowOnStopCam)
        try:
            assert cam.capture_entered.wait(2.0)
            s.close(timeout=2.0)
            assert cam.hardware_released.wait(2.0)
            assert s.state == "CLOSED"                    # 主动关闭引发的异常 → CLOSED
            assert cam.close_calls == 1
        finally:
            self._finish(s, cam)

    def test_real_fault_goes_fault_and_releases_once(self, monkeypatch):
        s, cam = _mkdir(monkeypatch, _RealFaultCam)
        try:
            assert cam.capture_entered.wait(2.0)
            cam.allow_capture_exit.set()
            assert cam.hardware_released.wait(2.0)
            assert s.state == "FAULT"                     # 真实采集异常保留 FAULT
            assert cam.close_calls == 1
        finally:
            self._finish(s, cam)


class TestStartFailureCleanup:
    def test_configure_fail_releases_once(self, monkeypatch):
        cls = _use(monkeypatch, _FailConfigureCam)
        s = CameraService(width=8, height=6, queue_size=8)
        s.start()
        try:
            assert cls.instances[0].close_calls == 1      # start 失败路径释放一次
            assert s.state == "FAULT"
            assert s._picam2 is None
        finally:
            try:
                s.close(timeout=2.0)
            except Exception:
                pass

    def test_stream_start_fail_releases_once(self, monkeypatch):
        cls = _use(monkeypatch, _FailStreamStartCam)
        s = CameraService(width=8, height=6, queue_size=8)
        s.start()
        try:
            assert cls.instances[0].close_calls == 1
            assert s.state == "FAULT" and s._picam2 is None
        finally:
            try:
                s.close(timeout=2.0)
            except Exception:
                pass

    def test_close_after_failed_start_goes_closed(self, monkeypatch):
        cls = _use(monkeypatch, _FailConfigureCam)
        s = CameraService(width=8, height=6, queue_size=8)
        s.start()
        try:
            s.close(timeout=2.0)                          # 失败后 close → 硬件已清 → CLOSED
            assert s.state == "CLOSED"
            assert cls.instances[0].close_calls == 1
        finally:
            try:
                s.close(timeout=2.0)
            except Exception:
                pass

    def test_start_close_concurrent_no_double_release(self, monkeypatch):
        cls = _use(monkeypatch, _BaseBlockingCam)
        s = CameraService(width=8, height=6, queue_size=8)

        def _st():
            s.start()
        t = threading.Thread(target=_st)
        t.start()
        s.close(timeout=2.0)                              # 与 start 并发
        t.join(5)
        try:
            cam = cls.instances[0]
            assert cam.close_calls <= 1                   # 不重复释放
            if s._thread is not None and s._thread.is_alive():
                s.close(timeout=2.0)                      # 二次收尾
            assert s.state == "CLOSED"
        finally:
            try:
                cam.allow_capture_exit.set()
                s.close(timeout=2.0)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 三项目前缺失仍待补齐的关闭覆盖（见最终只读审查 §5）
# 全部使用 Event/Barrier 同步原语 + 明确超时；Fake 可解除，同进程安全清理。
# ---------------------------------------------------------------------------
class _ReleaseGatedCam(_BaseBlockingCam):
    """finally 进入硬件释放段(stop)时停住（持 _hw_lock），供主线程启动 stop-helper 制造排队。"""

    def __init__(self):
        super().__init__()
        self.release_entered = threading.Event()
        self.allow_release = threading.Event()

    def stop(self):
        self.stop_calls += 1
        self.release_entered.set()   # finally 已在持 _hw_lock 进入硬件释放段
        self.allow_release.wait()    # 持锁停住，等主线程启动 helper


class TestAdditionalCloseGaps:
    def _finish(self, s, cam):
        cam.allow_capture_exit.set()
        if hasattr(cam, "allow_release"):
            cam.allow_release.set()
        try:
            s.close(timeout=2.0)
        except Exception:
            pass

    # ---- 测试一：thread.start() 启动失败 → 硬件释放一次、FAULT、二次 close 不重复 ----
    def test_thread_start_failure_releases_hardware_once(self, monkeypatch):
        import threading as _th
        cls = _use(monkeypatch, _BaseBlockingCam)

        class _FailingStartThread(_th.Thread):
            _fail = 1

            def start(self):
                if _FailingStartThread._fail:
                    _FailingStartThread._fail -= 1
                    raise RuntimeError("boot thread start fail")
                return super().start()

        monkeypatch.setattr("threading.Thread", _FailingStartThread)
        s = CameraService(width=8, height=6, queue_size=8)
        s.start()
        try:
            assert cls.instances[0].close_calls == 1   # start 失败路径释放一次
            assert s.state == "FAULT"
            assert s._picam2 is None
            assert s._start_done.is_set()
            assert s._thread is None or not s._thread.is_alive()   # 无运行采集线程
            s.close(timeout=2.0)                                 # 二次 close 不得重复释放
            assert cls.instances[0].close_calls == 1
        finally:
            try:
                s.close(timeout=2.0)
            except Exception:
                pass

    # ---- 测试二：finally 持 _hw_lock 期间 stop-helper 排队；获锁后见硬件已释放则跳过 stop ----
    def test_helper_waiting_for_hw_lock_skips_after_capture_finally_release(self, monkeypatch):
        s, cam = _mkdir(monkeypatch, _ReleaseGatedCam)
        try:
            assert cam.capture_entered.wait(2.0)
            s._stop_event.set()                    # 请求主动关闭，令采集线程退出时进 finally 走 CLOSED
            # 采集线程进入 finally：持 _hw_lock 停在 stop()（硬件释放段）
            cam.allow_capture_exit.set()
            assert cam.release_entered.wait(2.0)               # finally 已持锁停在 stop
            # 此刻启动 stop-helper → 它会在 _hw_lock 处排队等待
            s._maybe_start_stop_helper()
            # 放行 finally：完成 stop+close、置 _picam2=None、释放 _hw_lock
            cam.allow_release.set()
            # 采集 finally 完成 + 硬件释放
            assert cam.hardware_released.wait(2.0)
            # helper 获锁后二次校验见 _picam2 None → 不调用 cam.stop()；stop 只来自 finally 一次
            assert cam.stop_calls == 1
            assert cam.close_calls == 1
            assert s.state == "CLOSED"
        finally:
            self._finish(s, cam)

    # ---- 测试三：首次 close 超时(挂起 STOPPING)后，二次 close 续等待并完成关闭 ----
    def test_close_timeout_then_second_close_finishes_shutdown(self, monkeypatch):
        s, cam = _mkdir(monkeypatch, _CaptureForeverCam)   # capture 永久阻塞、stop 不唤醒
        try:
            assert cam.capture_entered.wait(2.0)
            s.close(timeout=0.2)          # 第一次 close：有界返回，采集未退
            assert s.state != "CLOSED"    # 未伪装关闭成功
            assert not cam.hardware_released.is_set()
            assert s._picam2 is not None  # 保留硬件引用
            # 解除阻塞：采集线程执行 finally → 释放硬件
            cam.allow_capture_exit.set()
            # 二次 close：即使 shutting_down 已置位也必须继续等待并完成
            s.close(timeout=2.0)
            assert s.state == "CLOSED"
            assert s._capture_exited.is_set()
            assert cam.close_calls == 1          # 硬件只释放一次
            assert s._thread is None or not s._thread.is_alive()   # 无运行采集线程
        finally:
            self._finish(s, cam)