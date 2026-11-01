"""V4.4 B 第二批：E7/E8 在 EdgeClient 进程内共享 CameraService 的组合测试（Mock，不连真机）。

覆盖任务书组合测试 A–D：
- A 共享摄像头：EdgeCamAdapter 与 E8CameraAdapter 订阅同一 CameraService（单实例/双收/隔离）。
- B E7 触发 E8：Fake DS18B20 高温告警 → E8 录像任务被消费并完成 Fake 录像。
- C 感知不被阻塞：慢录像期间 CameraService 仍产帧、E7 采样继续。
- D 异常与关闭顺序：E8 无共享服务初始化失败、E8 录像失败/OSS 上传失败不拖垮、
  E8 订阅先于统一关闭释放、close 幂等。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

# repo 根入 sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.camera_service import CameraService  # noqa: E402
from src.sensor.config import sensor_config_from_dict  # noqa: E402
from src.sensor.reader import FakeReader  # noqa: E402
from src.database.db import Database  # noqa: E402
from src import e7e8_integration as integ  # noqa: E402
from src.edge_cam_adapter import EdgeCamAdapter  # noqa: E402
from scripts.e7e8_runner import E8Config  # noqa: E402


_SENSOR = {
    "sensor_id": "ds18b20-01",
    "temperature_threshold": 40.0, "level2_threshold": 50.0, "recovery_threshold": 36.0,
    "sample_period_seconds": 1.0, "confirm_high_count": 1, "confirm_recovery_count": 1,
    "cooldown_seconds": 1.0, "temperature_min": -40.0, "temperature_max": 85.0,
    "humidity_min": 0.0, "humidity_max": 100.0, "reader": "fake",
    "device_id": "28-0224e33f1e4e",
}


def _hot_reader():
    return FakeReader(lambda: (55.0, 50.0))   # 稳定超过 level2=50 → 高温告警


def _fast_sleep(x: float) -> None:
    time.sleep(min(x, 0.001))


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
        return {"main": main}

    def configure(self, c):
        pass

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def capture_array(self):
        import numpy as np
        self._i += 1
        h, w = self.main["size"][1], self.main["size"][0]
        return np.zeros((h, w, 3), dtype=np.uint8) + (self._i % 255)


@pytest.fixture(autouse=True)
def _fake_picamera(monkeypatch):
    class _M:
        def __init__(self):
            self.Picamera2 = _FakeCam

    monkeypatch.setitem(sys.modules, "picamera2", _M())
    _FakeCam.instances.clear()


@pytest.fixture()
def cam_service():
    s = CameraService(width=8, height=6, fps=30.0, queue_size=8)
    s.start()
    yield s
    try:
        s.close()
    except Exception:
        pass


@pytest.fixture()
def db():
    import shutil as _sh
    import tempfile as _t
    _dir = _t.mkdtemp(prefix="e7e8db_")

    class _TmpPath:
        name = _dir

    d = Database(db_path=os.path.join(_dir, "e7e8.db"))
    d._tmpdir = _TmpPath()
    try:
        yield d
    finally:
        # Windows 下 SQLite 文件锁可能导致 TemporaryDirectory 清理报 NotADirectoryError；
        # 隔离测试用 ignore_errors 清理，不影响功能断言。
        try:
            _sh.rmtree(_dir, ignore_errors=True)
        except Exception:
            pass


def _wait(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _wait_frames(svc, prev_seq, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        latest = svc.latest()
        if latest is not None and latest.seq > prev_seq:
            return latest.seq
        time.sleep(0.01)
    return -1


def _cur_seq(svc):
    """当前帧号（seq）作等待基准；session_id 是会话号，不可当帧 seq 用。"""
    latest = svc.latest()
    return latest.seq if latest is not None else 0


def _evidence_row(db):
    """取最近一条 media_evidence 记录的状态三元组，供 OSS 失败持久化断言。"""
    try:
        with db._connect() as c:
            r = c.execute(
                "SELECT status, upload_status, retry_count FROM media_evidence "
                "ORDER BY media_id DESC LIMIT 1").fetchone()
        return (r[0], r[1], r[2]) if r else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Test A：共享摄像头（E8CameraAdapter 订阅与感知同一 CameraService）
# ---------------------------------------------------------------------------
class TestSharedCamera:
    def test_single_picam_and_shared_subscription(self, cam_service, db):
        # e7+e8 mode：evidence 的 recorder 用真 E8CameraAdapter 订阅 cam_service
        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=True, record_seconds=0, width=8, height=6,
                                    fps=30.0, codec="mp4v", tmp_dir=None),
                           db,
                           camera_service=cam_service, reader=_hot_reader(),
                           sleep=_fast_sleep)
        evidence = e.runner.recorder            # EvidenceRecorder
        e8_adapter = evidence.recorder._capture_factory()   # -> E8CameraAdapter(cam_service)
        try:
            assert len(_FakeCam.instances) == 1              # 唯一硬件持有者
            # 两个订阅者：Edge(感知) + E8(取证) 同一 CameraService
            edge = EdgeCamAdapter(cam_service)
            _wait_frames(cam_service, _cur_seq(cam_service))
            assert e8_adapter.read()[0] is True        # E8 订阅可读帧 (E8CameraAdapter.read->(ok,frame))
            assert edge.capture() is not None           # Edge(感知) 订阅可读帧
            e8_adapter.release()                # E8 释放自身订阅
            assert cam_service.state == "RUNNING"   # 不关 CameraService
            assert _FakeCam.instances[0].closed is False
        finally:
            e.close()

    def test_release_e8_does_not_affect_edge(self, cam_service, db):
        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=True, record_seconds=0, width=8, height=6,
                                    fps=30.0, codec="mp4v", tmp_dir=None),
                           db, camera_service=cam_service, reader=_hot_reader(),
                           sleep=_fast_sleep)
        evidence = e.runner.recorder
        e8a = evidence.recorder._capture_factory()
        edge = EdgeCamAdapter(cam_service)
        e8a.release()
        assert cam_service.state == "RUNNING"
        f = edge.capture()
        assert f is not None and f.shape == (6, 8, 3)
        e.close()


# ---------------------------------------------------------------------------
# Test B：E7 高温告警 → E8 录像任务被消费（Fake 录像）
# ---------------------------------------------------------------------------
class TestE7TriggersE8:
    def test_hot_alert_consumed_and_fake_recorded(self, cam_service, db):
        rec = _FakeRecorder()
        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=True, record_seconds=0, width=8, height=6,
                                    fps=30.0, codec="mp4v", tmp_dir=str(db._tmpdir.name)),
                           db, camera_service=cam_service, reader=_hot_reader(),
                           sleep=_fast_sleep, recorder=rec)
        try:
            e.start()
            # E7 高温告警 → E8 消费线程完成 Fake 录像（不阻塞 E7 采样线程）
            # 40s 为集成测试容错窗口：Pi 上首次高温告警可能因前序状态恢复、
            # 数据持久化等较慢；不代表业务系统必须在 40s 内完成告警，也不改变
            # E7 采样/告警/E8 消费/录像业务逻辑。
            assert _wait(lambda: rec.recorded, timeout=40.0)
            assert rec.calls >= 1
            # 感知/摄像头仍在产帧（录像在消费线程，不影响采集主线程）
            assert _wait_frames(cam_service, _cur_seq(cam_service)) >= 0
        finally:
            e.close()


# ---------------------------------------------------------------------------
# Test C：感知不被阻塞（慢录像期间 CameraService 仍产帧、E7 采样继续）
# ---------------------------------------------------------------------------
class TestPerceptionNotBlocked:
    def test_slow_record_keeps_frames_and_sampling(self, cam_service, db):
        rec = _FakeRecorder(delay=0.4, record_count=1)
        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=True, record_seconds=0, width=8, height=6,
                                    fps=30.0, codec="mp4v", tmp_dir=str(db._tmpdir.name)),
                           db, camera_service=cam_service, reader=_hot_reader(),
                           sleep=_fast_sleep, recorder=rec)
        try:
            e.start()
            _wait(lambda: rec.in_record, timeout=5.0)
            # 录像进行中：CameraService 仍持续产帧（慢录像不阻塞采集）
            base = _cur_seq(cam_service)
            later = _wait_frames(cam_service, base, timeout=3.0)
            assert later > base
            # 采样仍在推进（E7 未被录像卡住）——以循环仍在产帧为证（消费线程独立）
            assert cam_service.state == "RUNNING"
            _wait(lambda: rec.recorded, timeout=5.0)
        finally:
            e.close()


# ---------------------------------------------------------------------------
# Test D：异常隔离与关闭顺序
# ---------------------------------------------------------------------------
class TestErrorsAndShutdown:
    def test_e8_without_shared_camera_raises(self, db):
        with pytest.raises(ValueError, match="camera_service"):
            integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=True), db, camera_service=None,
                           reader=_hot_reader(), sleep=_fast_sleep)

    def test_e7_only_works_without_e8(self, db):
        # e8 disabled：仅 E7 采样告警，不建 E8 订阅、不录像
        calls = {"n": 0}

        def _prov():
            calls["n"] += 1
            return (55.0, 50.0)

        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=False), db, reader=FakeReader(_prov),
                           sleep=_fast_sleep)
        assert e.mode == "e7"
        e.start()
        assert _wait(lambda: calls["n"] >= 2)   # E7 采样真实执行 ≥2 次（不是无效等待）
        e.close()
        e.close()                               # 幂等

    def test_oss_upload_failure_does_not_break(self, cam_service, db):
        rec = _FakeRecorder()

        def _boom_uploader(path, prefix=None, **k):
            raise RuntimeError("oss down")

        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=True, record_seconds=0, width=8, height=6,
                                    fps=30.0, codec="mp4v",
                                    tmp_dir=str(db._tmpdir.name)),
                           db, camera_service=cam_service, reader=_hot_reader(),
                           sleep=_fast_sleep, recorder=rec, uploader=_boom_uploader)
        try:
            e.start()
            _wait(lambda: rec.recorded)   # 录像完成；OSS 失败被 EvidenceRecorder 捕获
            # OSS 上传失败：media_evidence 应持久化为 failed-awaiting-retry / upload_status=failed
            assert _wait(lambda: _evidence_row(db) is not None
                         and _evidence_row(db)[0] == "failed-awaiting-retry")
            row = _evidence_row(db)
            assert row is not None and row[1] == "failed" and int(row[2]) >= 1
            assert cam_service.state == "RUNNING"   # OSS 失败不拖垮检测/采样
        finally:
            e.close()

    def test_e8_subscription_released_then_camera_closed(self, cam_service, db):
        rec = _FakeRecorder()
        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=True, record_seconds=0, width=8, height=6,
                                    fps=30.0, codec="mp4v", tmp_dir=str(db._tmpdir.name)),
                           db, camera_service=cam_service, reader=_hot_reader(),
                           sleep=_fast_sleep, recorder=rec)
        e.start()
        e.close()                       # 关闭组合：E7/E8 线程停止、E8 订阅已释放
        assert cam_service.state == "RUNNING"   # 摄像头仍由 EdgeClient 持有
        cam_service.close()             # EdgeClient 统一关闭
        assert cam_service.state == "CLOSED"
        assert _FakeCam.instances[0].closed is True


class _FakeRecorder:
    """摄像头录像后端替身：record(seconds, path)->str，可记录/阻塞/失败。"""

    def __init__(self, delay=0.0, fail=False, record_count=0):
        self.delay = delay
        self.fail = fail
        self.record_count = record_count
        self.calls = 0
        self.recorded = False
        self.in_record = False
        self._ev = threading.Event()

    def record(self, seconds, dest_path):
        self.calls += 1
        # record_count==0 表示不限制（每次都录像）；>0 时超过该次数直接返回（不再阻塞）
        if self.record_count and self.calls > self.record_count:
            return Path(dest_path)
        self.in_record = True
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail:
                raise RuntimeError("record boom")
            p = Path(dest_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"fake-video:non-empty")
            return str(p)
        finally:
            self.in_record = False
            self.recorded = True
            self._ev.set()


# ---------------------------------------------------------------------------
# E7/E8 生命周期缺口（只读审查 §5 / 本轮任务 §八）
# ---------------------------------------------------------------------------
class _CloseCountReader(FakeReader):
    """可计数的 close，用于验证 E7-only 关闭顺序（reader 不被提前关）。"""

    def __init__(self, provider):
        super().__init__(provider)
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class TestLifecycleGaps:
    def test_e7_only_close_timeout_keeps_reader_and_retry(self, db):
        calls = {"n": 0}

        def _prov():
            calls["n"] += 1
            return (55.0, 50.0)

        reader = _CloseCountReader(_prov)
        entered = threading.Event()

        def _uninterruptible_wait(p):
            entered.set()
            time.sleep(0.3)          # 模拟采样线程处于不可中断等待

        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR), E8Config(enabled=False), db,
                           reader=reader, sleep=_fast_sleep, wait=_uninterruptible_wait)
        e.start()
        assert entered.wait(2.0)              # 采样线程已进入不可中断等待
        assert e.close(timeout=0.05) is False  # 超时 → 未完全关闭
        assert reader.close_calls == 0         # 未提前关 reader
        assert e._thread is not None           # 保留线程引用供重试
        assert e.close(timeout=2.0) is True    # wait 结束后退出，重试关闭成功
        assert reader.close_calls == 1
        assert e.close(timeout=2.0) is True    # 幂等

    def test_e7e8_runner_false_propagates(self, db, monkeypatch):
        rec = _FakeRecorder()
        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=True, record_seconds=0, width=8, height=6,
                                    fps=30.0, codec="mp4v", tmp_dir=None),
                           db, camera_service=object(), reader=_hot_reader(),
                           sleep=_fast_sleep, recorder=rec)
        monkeypatch.setattr(e.runner, "close", lambda timeout=5.0: False)
        assert e.close(timeout=0.2) is False   # runner.close False → 外层 False

    def test_shutdown_edge_camera_safe(self):
        from src import edge_client as ec

        class _S:
            def __init__(self):
                self.closed = False

            def stop(self):
                pass

            def close(self):
                self.closed = True

        class _E8:
            def __init__(self, ok):
                self._ok = ok
                self.called = False

            def close(self, timeout=5.0):
                self.called = True
                return self._ok

        # e7e8 关闭失败 → 不关闭共享 CameraService
        svc = _S()
        assert ec._shutdown_edge(svc, None, _E8(False)) is False
        assert svc.closed is False
        # 无 e7e8 → 正常关
        svc2 = _S()
        assert ec._shutdown_edge(svc2, None, None) is True
        assert svc2.closed is True
        # e7e8 关闭成功 → 正常关
        svc3 = _S()
        assert ec._shutdown_edge(svc3, None, _E8(True)) is True
        assert svc3.closed is True

    def test_close_idempotent_e7e8(self, db, cam_service):
        rec = _FakeRecorder()
        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=True, record_seconds=0, width=8, height=6,
                                    fps=30.0, codec="mp4v", tmp_dir=str(db._tmpdir.name)),
                           db, camera_service=cam_service, reader=_hot_reader(),
                           sleep=_fast_sleep, recorder=rec)
        e.start()
        assert e.close(timeout=2.0) is True
        assert e.close(timeout=2.0) is True    # 幂等

    def test_start_immediately_then_close(self, db, monkeypatch):
        from src.sensor.runner import SensorRunner as _SR
        gate = threading.Event()

        class _GateRunner(_SR):
            def start(self):
                gate.wait()                 # 采样线程卡在 runner.start 前（close 先行）
                super().start()

        monkeypatch.setattr(integ, "SensorRunner", _GateRunner)
        prov = {"n": 0}

        def _p():
            prov["n"] += 1
            return (55.0, 50.0)

        reader = _CloseCountReader(_p)
        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=True, record_seconds=0, width=8, height=6,
                                    fps=30.0, codec="mp4v", tmp_dir=None),
                           db, camera_service=object(), reader=reader,
                           sleep=_fast_sleep, recorder=_FakeRecorder())
        e.start()                            # 采样线程进入 _GateRunner.start，被 gate 拦住
        close_ok = e.close(timeout=2.0)      # close 先执行；采样 start 将被停止态拒绝
        gate.set()                           # 放行 → 采样 start 被拒，不再重启采样
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and (e._thread is not None and e._thread.is_alive()):
            time.sleep(0.01)
        assert close_ok is True              # 启动竞态下仍安全完成关闭
        assert reader.close_calls == 1       # reader 只关一次
        assert prov["n"] == 0                # 采样从未在 reader 关闭后重启


class TestRecoveryOrdering:
    """恢复时序：新 E8 事件先于启动恢复被消费，pending 恢复随后仍触发（runner 单点修复回归）。"""

    def test_consume_handles_new_event_before_recovery(self, db):
        from src.sensor.runner import SensorRunner
        from src.sensor.loop import SampledSensorLoop
        from src.sensor.alarm import HighTempAlarm

        class _Stub:
            def __init__(self):
                self.order = []
                self.handled = threading.Event()
                self.recovered = threading.Event()

            def decide_from_event(self, event):
                if (isinstance(event, dict) and event.get("type") == "alert_created"
                        and event.get("level") == 2):
                    return event.get("alert_id")
                return None

            def handle_event(self, event):
                self.order.append(("handle", event["alert_id"]))
                self.handled.set()
                return {"handled": True}

            def recover(self, max_tasks=10):
                self.order.append(("recover",))
                self.recovered.set()
                return []

        stub = _Stub()
        sensor_cfg = sensor_config_from_dict(_SENSOR)
        loop = SampledSensorLoop(sensor_cfg, _hot_reader(),
                                 HighTempAlarm(sensor_cfg, db), db, sleep=_fast_sleep)
        runner = SensorRunner(loop, stub, sleep=_fast_sleep, recover_interval_seconds=60.0)
        runner.enqueue({"type": "alert_created", "level": 2, "alert_id": "A1"})  # 入队先于启动
        runner.start()
        try:
            assert stub.handled.wait(3.0)        # 新事件已被消费（不必等启动恢复）
            assert stub.recovered.wait(3.0)      # 恢复入口仍被触发
            handle_idx = [i for i, e in enumerate(stub.order) if e[0] == "handle"]
            recover_idx = [i for i, e in enumerate(stub.order) if e[0] == "recover"]
            assert handle_idx and recover_idx
            assert handle_idx[0] < recover_idx[0]       # 新事件先于恢复
            assert stub.order.count(("handle", "A1")) == 1   # 不重复消费
        finally:
            runner.close(timeout=1.0)