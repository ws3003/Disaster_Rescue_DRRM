"""V4.4 E7→E8 方案 A Runner 测试（全 Fake：SeqReader / FakeRecorder / FakeUploader + 临时库）。

覆盖 §3C：一级不触发、二级到达 recorder、alert_upgraded 判定、同告警去重、
队列满不阻塞 + 诊断、消费者异常不终止、关闭排空、recover 启动一次。
不连真实硬件/OSS/正式库。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time

import pytest

from src.database.db import Database
from src.evidence.recorder import EvidenceRecorder
from src.sensor.model import Reading
from src.sensor.config import SensorConfig
from src.sensor.runner import SensorRunner, build_runner
from src.sensor.sensor_errors import SensorReadError


def _iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SeqReader:
    def __init__(self, temps):
        self.temps = list(temps)
        self.i = 0

    def read(self, now=None):
        if self.i >= len(self.temps):
            raise SensorReadError("exhausted")
        t = self.temps[self.i]
        self.i += 1
        return Reading(temperature_c=t, humidity_pct=None,
                       captured_at=(now() if now else _iso()))


class OkUploader:
    def __init__(self):
        self.calls = []

    def upload(self, path, prefix="evidence"):
        self.calls.append((path, prefix))
        return {"ok": True, "url": f"http://x/{prefix}/{os.path.basename(path)}",
                "provider": "aliyun_oss"}


def _cfg() -> SensorConfig:
    return SensorConfig(
        sensor_id="s1", temperature_threshold=40.0, level2_threshold=50.0,
        recovery_threshold=36.0, sample_period_seconds=1.0,
        confirm_high_count=3, confirm_recovery_count=3, cooldown_seconds=600.0)


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "runner_test.db"))
    d._tmpdir = tmp
    return d


def _mk(db, temps, uploader=None, **kw):
    return build_runner(_cfg(), db=db, reader=SeqReader(temps), uploader=uploader, **kw)


class TestEventRouting:
    def test_level1_no_video(self, db):
        up = OkUploader()
        r = _mk(db, [45, 45, 45], uploader=up)
        r.sample(3)          # 3×45 → 一级告警创建；decide 判 skip
        assert r.dropped == []           # 无一二级投递
        with db._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM media_evidence").fetchone()[0]
        assert n == 0
        assert up.calls == []            # 未录像/未上传

    def test_level2_reaches_recorder(self, db):
        up = OkUploader()
        r = _mk(db, [45, 45, 45, 55, 55, 55], uploader=up, recover_interval_seconds=3600)
        r.sample(6)                      # 一级创建 + 二级升级
        r.start(); ok = r.stop()
        assert ok is True
        with db._connect() as conn:
            rows = conn.execute("SELECT ref_id,status,upload_status FROM media_evidence").fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "success" and rows[0][2] == "success"
        assert len(up.calls) == 1
        # ref_id 确为告警
        with db._connect() as conn:
            a = conn.execute("SELECT alert_id FROM sensor_alerts ORDER BY alert_id").fetchall()
        assert rows[0][0] in {x[0] for x in a}

    def test_decide_rule(self):
        assert EvidenceRecorder.decide_from_event({"type": "alert_created", "level": 1, "alert_id": "A"}) is None
        assert EvidenceRecorder.decide_from_event({"type": "alert_created", "level": 2, "alert_id": "A"}) == "A"
        assert EvidenceRecorder.decide_from_event({"type": "alert_upgraded", "alert_id": "A"}) == "A"
        assert EvidenceRecorder.decide_from_event({"type": "alert_resolved", "alert_id": "A"}) is None
        assert EvidenceRecorder.decide_from_event("not-a-dict") is None

    def test_same_alert_dedupe(self, db):
        up = OkUploader()
        # 先造一个真实告警作为 ref
        rid = db.save_sensor_alert(type="sensor_hightemp", temperature=52.0, threshold=50.0,
                                   device_captured_at=_iso())
        r = _mk(db, [], uploader=up, recover_interval_seconds=3600)
        ev = {"type": "alert_created", "level": 2, "alert_id": rid,
              "device_captured_at": _iso()}
        assert r.enqueue(ev) == "enqueued"
        assert r.enqueue(ev) == "enqueued"       # 同一告警重复事件
        r.start(); assert r.stop() is True
        with db._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM media_evidence WHERE ref_id=?", (rid,)).fetchone()[0]
        assert n == 1                            # 不重复建/不重复录


class TestQueueAndConsumer:
    def test_queue_full_drops_not_block(self, db):
        r = _mk(db, [], recover_interval_seconds=3600)   # 队列默认 1000, 不启动消费者
        r._queue = type(r._queue)(maxsize=1)             # 缩小队列以触发满
        rid1 = db.save_sensor_alert(type="sensor_hightemp", temperature=52.0, threshold=50.0,
                                    device_captured_at=_iso())
        outs = [r.enqueue({"type": "alert_created", "level": 2, "alert_id": rid1,
                           "device_captured_at": _iso()}) for _ in range(5)]
        assert outs[0] == "enqueued"
        assert outs[1:] == ["dropped"] * 4        # put_nowait 非阻塞, 丢弃不假报
        d = r.dropped
        assert d and all(x["reason"] == "queue_full" for x in d)
        assert d[0]["alert_id"] == rid1 and d[0]["event_type"] == "alert_created"

    def test_consumer_exception_continues(self, db, monkeypatch):
        up = OkUploader()
        rid1 = db.save_sensor_alert(type="sensor_hightemp", temperature=52.0, threshold=50.0,
                                    device_captured_at=_iso())
        rid2 = db.save_sensor_alert(type="sensor_hightemp", temperature=55.0, threshold=50.0,
                                    device_captured_at=_iso())
        r = _mk(db, [], uploader=up, recover_interval_seconds=3600)
        orig = r.recorder.handle_event
        state = {"count": 0}

        def flaky(ev):
            state["count"] += 1
            if state["count"] == 1:
                raise RuntimeError("consumer boom")
            return orig(ev)

        r.recorder.handle_event = flaky
        r.enqueue({"type": "alert_created", "level": 2, "alert_id": rid1, "device_captured_at": _iso()})
        r.enqueue({"type": "alert_created", "level": 2, "alert_id": rid2, "device_captured_at": _iso()})
        r.start(); assert r.stop() is True
        assert any(x["reason"].startswith("consumer_exception") for x in r.dropped)
        with db._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM media_evidence WHERE ref_id=?", (rid2,)).fetchone()[0]
        assert n == 1                             # 异常后仍处理了后续事件


class TestLifecycle:
    def test_stop_drains_and_closes(self, db, monkeypatch):
        rid = db.save_sensor_alert(type="sensor_hightemp", temperature=52.0, threshold=50.0,
                                   device_captured_at=_iso())
        r = _mk(db, [], uploader=OkUploader(), recover_interval_seconds=3600)
        closed = {"ok": False}
        monkeypatch.setattr(r.loop, "close", lambda: closed.update(ok=True))
        r.enqueue({"type": "alert_created", "level": 2, "alert_id": rid, "device_captured_at": _iso()})
        r.start()
        assert r.stop() is True                  # 排空 + 消费者结束
        assert r.close() is True
        assert closed["ok"] is True              # 关闭 Reader

    def test_recover_runs_once_not_per_sample(self, db):
        up = OkUploader()
        r = _mk(db, [45, 45, 45, 55, 55, 55], uploader=up, recover_interval_seconds=3600)
        calls = {"n": 0}
        orig = r.recorder.recover
        r.recorder.recover = lambda **kw: (calls.__setitem__("n", calls["n"] + 1) or orig(**kw))
        r.sample(6)                              # 采样阶段不触发 recover
        assert calls["n"] == 0
        r.start()                                # 消费者启动时一次
        time.sleep(0.3)
        assert r.stop() is True
        assert calls["n"] >= 1                   # 至少一次（启动时）
        assert calls["n"] < 3                    # 未在每个采样步骤中无节制触发


def _new_alert_rows(db, n=1):
    return [db.save_sensor_alert(type="sensor_hightemp", temperature=52.0 + i, threshold=50.0,
                                 device_captured_at=_iso()) for i in range(n)]


class TestShutdownRace:
    def test_consumer_timeout_returns_false_bounded(self, db):
        rid = _new_alert_rows(db)[0]
        r = _mk(db, [], uploader=OkUploader())
        entered = threading.Event()
        proceed = threading.Event()
        orig = r.recorder.handle_event

        def blk(ev):
            entered.set()
            proceed.wait(timeout=5)
            return orig(ev)

        r.recorder.handle_event = blk
        r.enqueue({"type": "alert_created", "level": 2, "alert_id": rid, "device_captured_at": _iso()})
        r.start()
        assert entered.wait(timeout=2), "消费者未进入 handle_event"
        t0 = time.monotonic()
        res = r.close(timeout=0.3)                       # 消费者被阻塞 -> 有限时间内返回 False
        elapsed = time.monotonic() - t0
        assert res is False
        assert elapsed < 2                               # 不无限等待
        assert any(x["reason"] == "close_timeout" for x in r.dropped)
        proceed.set()
        assert r.stop(timeout=3) is True                 # 放行后消费者可退出

    def test_sampling_running_reader_not_closed(self, db):
        rid = _new_alert_rows(db)[0]
        r = _mk(db, [], uploader=OkUploader(), recover_interval_seconds=3600)
        release = threading.Event()
        started = threading.Event()

        def blocking_sleep(_s):
            started.set()
            release.wait(timeout=5)

        r._sleep = blocking_sleep
        closed = {"n": 0}
        orig_close = r.loop.close
        r.loop.close = lambda: (closed.__setitem__("n", closed["n"] + 1) or orig_close())
        t = threading.Thread(target=r.run, kwargs={"steps_per_cycle": 1}, daemon=True)
        t.start()
        assert started.wait(timeout=3), "采样循环未进入 sleep"
        # 采样线程仍在运行 -> close 不应提前关闭 reader
        assert r.close(timeout=0.2) is False
        assert closed["n"] == 0                          # reader 未关闭
        # P0 严格顺序：stop 先等生产者(sampling_done)；采样未退出时记录 producer_still_running
        assert any(x["reason"] == "producer_still_running" for x in r.dropped)
        release.set()                                    # 采样线程退出
        t.join(timeout=3)
        assert t.is_alive() is False
        assert r.close(timeout=2) is True                # 采样退出后再关 reader
        assert closed["n"] == 1                          # reader 恰好关闭一次

    def test_stop_waits_sampler_before_consumer(self, db):
        # P0 严格顺序：stop() 必须先等采样线程退出（sampling_done），才能排空消费者。
        _new_alert_rows(db)
        r = _mk(db, [], uploader=OkUploader(), recover_interval_seconds=3600)
        release = threading.Event()
        started = threading.Event()

        def blocking_sleep(_s):
            started.set()
            release.wait(timeout=5)

        r._sleep = blocking_sleep
        t = threading.Thread(target=r.run, kwargs={"steps_per_cycle": 1}, daemon=True)
        t.start()
        assert started.wait(timeout=3), "采样循环未进入 sleep"
        # 采样仍阻塞（未退出）→ stop 不得假装完成
        assert r.stop(timeout=0.2) is False
        assert any(x["reason"] == "producer_still_running" for x in r.dropped)
        release.set()                                   # 采样线程可退出
        t.join(timeout=3)
        assert t.is_alive() is False
        assert r.stop(timeout=2) is True                # 采样退出后 stop 排空并成功

    def test_duplicate_close_idempotent(self, db):
        r = _mk(db, [], uploader=OkUploader(), recover_interval_seconds=3600)
        closed = {"n": 0}
        orig_close = r.loop.close
        r.loop.close = lambda: (closed.__setitem__("n", closed["n"] + 1) or orig_close())
        assert r.close() is True
        assert r.close() is True
        assert closed["n"] == 1                          # 幂等: reader 只关闭一次

    def test_start_only_then_close(self, db):
        r = _mk(db, [], uploader=OkUploader(), recover_interval_seconds=3600)
        closed = {"n": 0}
        orig_close = r.loop.close
        r.loop.close = lambda: (closed.__setitem__("n", closed["n"] + 1) or orig_close())
        r.start()                                        # 只启动消费者, 不启动 run()
        assert r._thread is not None and r._thread.is_alive()
        assert r.close() is True                         # 消费者已退出, 采样未运行 → 正常关闭
        assert r._thread.is_alive() is False             # 消费者线程已退出
        assert closed["n"] == 1                          # reader 只关闭一次

    def test_queue_drained_on_stop(self, db):
        rids = _new_alert_rows(db, 3)
        r = _mk(db, [], uploader=OkUploader(), recover_interval_seconds=3600)
        handled = {"n": 0}
        orig = r.recorder.handle_event
        r.recorder.handle_event = lambda ev: (handled.__setitem__("n", handled["n"] + 1) or orig(ev))
        for rid in rids:
            r.enqueue({"type": "alert_created", "level": 2, "alert_id": rid, "device_captured_at": _iso()})
        r.start()
        assert r.stop() is True
        assert handled["n"] == 3                         # 已接受事件被处理


class TestRecoverScheduling:
    def test_recover_interval_triggers(self, db):
        r = _mk(db, [], uploader=OkUploader(), recover_interval_seconds=0.05)
        calls = {"n": 0}
        orig = r.recorder.recover
        r.recorder.recover = lambda **kw: (calls.__setitem__("n", calls["n"] + 1) or orig(**kw))
        r.start()
        time.sleep(0.7)          # 足够多次空转：首空转执行一次恢复，此后按 recover_interval 节流多次（不再含启动同步一次）
        r.stop()
        assert calls["n"] >= 2   # 空转+间隔触发多次恢复

    def test_recover_exception_continues(self, db):
        r = _mk(db, [], uploader=OkUploader(), recover_interval_seconds=0.05)
        calls = {"n": 0}
        orig = r.recorder.recover

        def flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("recover boom")
            return orig(**kw)

        r.recorder.recover = flaky
        r.start()
        time.sleep(0.7)          # 多次空转：首空转失败一次(如前)后按 interval 继续
        assert r.stop() is True
        assert calls["n"] >= 2   # 一次失败后仍继续调度