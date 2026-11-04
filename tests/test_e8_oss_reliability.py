"""V4.4 E8→OSS 可靠性修复测试（Mock / 临时目录 / 临时 DB，不连真实 OSS、不上传）。

覆盖：processing 安全恢复、uploader=None 诊断、Runner 严格停机顺序/不丢事件、
flock 单实例锁生命周期（POSIX 才实际断言）。
明确：本文件均为 Mock 测试，不构成真实 OSS/云端验收。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.database.db import Database
from src.evidence.recorder import EvidenceRecorder, FakeRecorder
from src.sensor.alarm import HighTempAlarm
from src.sensor.config import SensorConfig
from src.sensor.runner import SensorRunner

import scripts.e7e8_runner as er  # noqa: E402

_POSIX = (sys.platform.startswith("linux")
          or sys.platform in ("darwin", "freebsd", "openbsd", "netbsd", "solaris"))


# ---------------------------------------------------------------------------
# 上传器/事件桩（Mock）
# ---------------------------------------------------------------------------
class OkUploader:
    def upload(self, path, prefix="evidence"):
        return {"ok": True, "url": "https://x/" + prefix + "/" + Path(path).name,
                "provider": "aliyun_oss"}


class FailUploader:
    def upload(self, path, prefix="evidence"):
        return {"ok": False, "error": "boom"}


def _level2(alert_id):
    return {"type": "alert_created", "level": 2, "alert_id": alert_id,
            "device_captured_at": "2026-11-05T00:00:01+00:00"}


def _create_alert(db, temp, level2=None):
    """经 HighTempAlarm 生成一条真实 sensor_alert（ref_id 供 media 校验引用）。"""
    cfg = SensorConfig(
        sensor_id="test-sensor", temperature_threshold=40.0,
        level2_threshold=level2, sample_period_seconds=1.0,
        confirm_high_count=1, confirm_recovery_count=1, cooldown_seconds=600.0,
    )
    ev = HighTempAlarm(cfg, db).process(temp, None, "2026-11-05T00:00:01+08:00")
    assert ev is not None and ev.get("alert_id")
    return ev["alert_id"]


@pytest.fixture
def db(tmp_path):
    return Database(db_path=str(tmp_path / "test.db"))


def _seed_processing(db, media_id, local_path, ref, capture="ok", write_file=True):
    """预置一条 processing 记录（capture_result 与本地文件可控）。"""
    if capture == "ok" and write_file:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(b"video-bytes:non-empty")
    return db.save_media_evidence(
        source="sensor", ref_id=ref, media_type="video", local_path=str(local_path),
        captured_at="2026-11-05T00:00:01+00:00", created_at="2026-11-05T00:00:02+00:00",
        status="processing", capture_result=capture, media_id=media_id,
    )


# ---------------------------------------------------------------------------
# A. processing 安全恢复
# ---------------------------------------------------------------------------
class TestProcessingRecovery:
    def test_processing_ok_file_recovered(self, db, tmp_path):
        mid = "M-TEST-0001"
        local = str(tmp_path / "m" / f"{mid}.mp4")
        ref = _create_alert(db, 55.0, level2=50.0)
        _seed_processing(db, mid, local, ref=ref, capture="ok")
        rec = EvidenceRecorder(db, recorder=FakeRecorder(), uploader=OkUploader())
        rec.recover(max_tasks=10, allow_processing=True)
        row = db.get_media_evidence(mid)
        assert row["status"] == "success"
        assert row["upload_status"] == "success"
        assert row["object_key"] == f"evidence/{mid}.mp4"
        assert row["uploaded_at"] is not None

    def test_processing_failure_marks_failed(self, db, tmp_path):
        mid = "M-TEST-0002"
        local = str(tmp_path / "m" / f"{mid}.mp4")
        ref = _create_alert(db, 55.0, level2=50.0)
        _seed_processing(db, mid, local, ref=ref, capture="ok")
        rec = EvidenceRecorder(db, recorder=FakeRecorder(), uploader=FailUploader())
        rec.recover(max_tasks=10, allow_processing=True)
        row = db.get_media_evidence(mid)
        assert row["status"] == "failed-awaiting-retry"
        assert row["upload_status"] == "failed"
        assert row["retry_count"] == 1
        assert row["last_error"]

    def test_processing_missing_file_not_recovered(self, db, tmp_path):
        MID = "M-TEST-0003"
        ref = _create_alert(db, 55.0, level2=50.0)
        local = str(tmp_path / "does_not_exist" / "x.mp4")  # 文件缺失
        _seed_processing(db, MID, local, ref=ref, capture="ok", write_file=False)
        rec = EvidenceRecorder(db, recorder=FakeRecorder(), uploader=OkUploader())
        rec.recover(max_tasks=10, allow_processing=True)
        row = db.get_media_evidence(MID)
        assert row["status"] == "processing"  # 未被补传/重录
        assert row.get("object_key") is None

    def test_recover_default_excludes_processing(self, db, tmp_path):
        MID = "M-TEST-0004"
        local = str(tmp_path / "m" / f"{MID}.mp4")
        ref = _create_alert(db, 55.0, level2=50.0)
        _seed_processing(db, MID, local, ref=ref, capture="ok")
        rec = EvidenceRecorder(db, recorder=FakeRecorder(), uploader=OkUploader())
        rec.recover(max_tasks=10)  # 默认 allow_processing=False
        row = db.get_media_evidence(MID)
        assert row["status"] == "processing"

    def test_uploader_none_warns_and_stays_processing(self, db, caplog):
        aid = _create_alert(db, 55.0, level2=50.0)
        rec = EvidenceRecorder(db, recorder=FakeRecorder(), uploader=None)
        with caplog.at_level("WARNING", logger="x4.evidence"):
            out = rec.handle_event(_level2(aid))
        assert out["ok"] is True
        row = db.get_media_evidence(out["media_id"])
        assert row["status"] == "processing"
        assert "uploader=None" in caplog.text
        assert "OSS 上传被跳过" in caplog.text

    def test_handle_event_upload_success(self, db):
        aid = _create_alert(db, 55.0, level2=50.0)
        rec = EvidenceRecorder(db, recorder=FakeRecorder(), uploader=OkUploader())
        out = rec.handle_event(_level2(aid))
        row = db.get_media_evidence(out["media_id"])
        assert row["status"] == "success" and row["upload_status"] == "success"
        assert row["object_key"] == f"evidence/{row['media_id']}.mp4"

    def test_handle_event_upload_failure(self, db):
        aid = _create_alert(db, 55.0, level2=50.0)
        rec = EvidenceRecorder(db, recorder=FakeRecorder(), uploader=FailUploader())
        out = rec.handle_event(_level2(aid))
        row = db.get_media_evidence(out["media_id"])
        assert row["status"] == "failed-awaiting-retry" and row["upload_status"] == "failed"


# ---------------------------------------------------------------------------
# B. Runner 严格停机顺序（生产者先退、消费者后排空，不丢事件、在途完成）
# ---------------------------------------------------------------------------
class _CollectRecorder:
    def __init__(self, delay=0.0):
        self.events = []
        self.delay = delay

    def decide_from_event(self, event):
        return event.get("alert_id") if isinstance(event, dict) else None

    def handle_event(self, event):
        if self.delay:
            time.sleep(self.delay)
        self.events.append(event)


class _DummyLoop:
    class _Cfg:
        sample_period_seconds = 0.002

    sample_period_seconds = 0.002
    cfg = _Cfg()

    def __init__(self, total=100000):
        self.total = total
        self.n = 0

    def step(self, now=None):
        if self.n < self.total:
            self.n += 1
            return {"status": "valid", "event": _level2(f"E-{self.n}")}
        return {"status": "valid", "event": None}

    def close(self):
        pass


class TestRunnerShutdownOrder:
    def test_queued_events_all_processed_before_close(self):
        c = _CollectRecorder()
        r = SensorRunner(_DummyLoop(), c, sleep=time.sleep)
        r.start()
        ids = [f"E-q{i}" for i in range(4)]
        for i in ids:
            assert r.enqueue(_level2(i)) == "enqueued"
        assert r.stop(timeout=5) is True
        assert {e["alert_id"] for e in c.events} == set(ids)
        assert r._thread is None or not r._thread.is_alive()

    def test_inflight_finishes_before_stop_returns(self):
        c = _CollectRecorder(delay=0.3)  # 模拟慢录像/上传
        r = SensorRunner(_DummyLoop(), c, sleep=time.sleep)
        r.start()
        r.enqueue(_level2("E-inflight"))
        t0 = time.monotonic()
        ok = r.stop(timeout=5)
        assert ok is True
        # 停在"在途事件已完成"之后（非立即返回）
        assert [e["alert_id"] for e in c.events] == ["E-inflight"]
        assert time.monotonic() - t0 >= 0.25

    def test_sampler_exits_then_consumer_drains_no_loss(self):
        c = _CollectRecorder()
        loop = _DummyLoop()
        r = SensorRunner(loop, c, sleep=time.sleep)
        producer = threading.Thread(target=r.run, kwargs={"steps_per_cycle": 1}, daemon=True)
        producer.start()
        time.sleep(0.15)  # 让采样线程产生若干事件
        ok = r.stop(timeout=10)
        assert ok is True
        producer.join(timeout=5)
        assert not producer.is_alive()          # 生产者已退出
        assert len(c.events) == loop.n          # 全部已入队事件被消费，无丢失


# ---------------------------------------------------------------------------
# C. flock 单实例锁生命周期（POSIX）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _POSIX, reason="flock 仅 POSIX")
class TestSingletonLockLifecycle:
    def test_held_after_owner_scope_returns(self, tmp_path):
        # 锁由 raw fd 持有；返回 fd 后（即便"所有者函数已返回"）锁仍有效 → 第二次获取失败
        dbp = str(tmp_path / "db.sqlite")
        fd1 = er._acquire_e8_singleton_lock(dbp)
        assert fd1 is not None
        try:
            with pytest.raises(RuntimeError):
                er._acquire_e8_singleton_lock(dbp)
        finally:
            os.close(fd1)

    def test_clean_release_then_reacquire(self, tmp_path):
        dbp = str(tmp_path / "db.sqlite")
        fd1 = er._acquire_e8_singleton_lock(dbp)
        assert fd1 is not None
        os.close(fd1)                            # 全部线程退出后的干净释放路径
        fd2 = er._acquire_e8_singleton_lock(dbp)
        assert fd2 is not None
        os.close(fd2)

    def test_lock_file_not_unlinked(self, tmp_path):
        dbp = str(tmp_path / "db.sqlite")
        fd = er._acquire_e8_singleton_lock(dbp)
        assert fd is not None
        lockp = Path(er._e8_lock_path(dbp))
        assert lockp.exists()                    # 未 unlink、未 O_TRUNC
        os.close(fd)
        lockp.unlink()                           # 仅测试清理，非生产释放方式