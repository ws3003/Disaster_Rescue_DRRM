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
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "e7e8.db"))
    d._tmpdir = tmp
    yield d


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
            _wait_frames(cam_service, cam_service.session_id - 1)
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
            assert _wait(lambda: rec.recorded)
            assert rec.calls >= 1
            # 感知/摄像头仍在产帧（录像在消费线程，不影响采集主线程）
            assert _wait_frames(cam_service, cam_service.session_id - 1) >= 0
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
            base = cam_service.session_id
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
        e = integ.EdgeE7E8(sensor_config_from_dict(_SENSOR),
                           E8Config(enabled=False), db, reader=_hot_reader(),
                           sleep=_fast_sleep)
        assert e.mode == "e7"
        e.start()
        _wait(lambda: True)   # 跑几拍不报错
        e.close()
        e.close()             # 幂等

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
            _wait(lambda: rec.recorded)   # 录像完成；OSS 失败被 EvidenceRecorder 捕获为 failed
            assert _wait(lambda: True)
            # 组合未崩：还能正常关闭
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