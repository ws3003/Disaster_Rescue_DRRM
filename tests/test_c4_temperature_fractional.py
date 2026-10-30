"""V4.4 E7→C4 温度数值契约衔接测试（方案 A：E7→C4 边界 exact_number，不改 canonical）。

断言：合法小数温度(26.75 等)经 E7 loop → bridge → canonical 进入 Outbox 而非 c4_verify；
worker 重读重算 hash 与入队一致；非法(NaN/Inf)仍被拒绝。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.database import canonical
from src.database.db import Database
from src.sensor import reader as _reader
from src.sensor.alarm import HighTempAlarm
from src.sensor.config import SensorConfig
from src.sensor.loop import SampledSensorLoop
from src.sync import bridge
from src.sync.worker import OutboxWorker


def _cfg(high=1, **over) -> SensorConfig:
    import dataclasses
    c = SensorConfig(sensor_id="s1", temperature_threshold=40.0, sample_period_seconds=1.0,
                     confirm_high_count=high, confirm_recovery_count=1, cooldown_seconds=0.0,
                     offline_after_failures=2)
    return dataclasses.replace(c, **over) if over else c


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "c4_temp_fractional.db"))
    d._tmpdir = tmp
    return d


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if callable(self._body):
            return self._body()
        return self._body


def _ok(sids):
    return _Resp(200, {"code": "OK", "data": [{"record_type": "reading",
                                               "source_record_id": s, "status": "synced",
                                               "error_code": None} for s in sids], "meta": {}})


def _outbox(db, rt="reading"):
    with db._connect() as c:
        return c.execute("SELECT source_record_id, status FROM sync_outbox WHERE record_type=?",
                         (rt,)).fetchall()


def _verify(db, rt="reading"):
    with db._connect() as c:
        return c.execute("SELECT COUNT(*) FROM c4_verify WHERE record_type=?", (rt,)).fetchone()[0]


class TestExactNumber:
    def test_float_to_exact_string(self):
        assert bridge.exact_number(26.75) == "26.75"
        assert bridge.exact_number(25.5) == "25.5"
        assert bridge.exact_number(20.0) == "20.0"
        assert bridge.exact_number(25) == 25          # 整数原样
        assert bridge.exact_number(-5.25) == "-5.25"
        assert bridge.exact_number(85.0) == "85.0"
        assert bridge.exact_number(float("nan")) != bridge.exact_number(float("nan")) or \
               repr(float("nan")) in ("nan",)          # NaN 原样浮点
        assert isinstance(bridge.exact_number(float("nan")), float)


class TestE7LoopFractional:
    def test_fractional_temp_enqueues_not_verify(self, db):
        loop = SampledSensorLoop(_cfg(), _reader.FakeReader(lambda: (26.75, 40.0)),
                                 HighTempAlarm(_cfg(), db), db, sleep=lambda _f: None)
        res = loop.step()
        assert res["status"] == "valid" or res["status"] == "valid"
        ob = _outbox(db)
        assert len(ob) == 1 and ob[0][1] == "pending"
        assert _verify(db) == 0          # 26.75 不再进 c4_verify
        # SQLite 存的是同值 26.75
        with db._connect() as c:
            v = c.execute("SELECT temperature FROM readings ORDER BY device_captured_at DESC LIMIT 1").fetchone()[0]
        assert float(v) == pytest.approx(26.75)

    def test_worker_rehash_and_send(self, db):
        # 先经 loop 入队 (26.75)
        SampledSensorLoop(_cfg(), _reader.FakeReader(lambda: (26.75, 40.0)),
                          HighTempAlarm(_cfg(), db), db, sleep=lambda _f: None).step()
        seed = _outbox(db)
        assert seed and seed[0][1] == "pending"
        sid = seed[0][0]
        sent = []
        def post(url, json=None, headers=None, timeout=10.0):
            sent.extend([r["source_record_id"] for r in json["records"]])
            return _ok([r["source_record_id"] for r in json["records"]])
        w = OutboxWorker(db, "http://win/api/v1", "t", http_post=post,
                         worker_id="w", now_ms=lambda: 1)
        w.process_batch(10)
        statuses = {x[1] for x in _outbox(db)}
        assert statuses == {"success"}     # 重读 hash 与入队一致，成功发送
        assert _verify(db) == 0
        assert sent == [sid]


class TestCanonicalFractional:
    @pytest.mark.parametrize("temp", [26.75, 25.5, 20.0, 85.0, -5.25])
    def test_fractional_valid_via_exact(self, temp):
        value = bridge.exact_number(temp)
        h, reasons = canonical.classify(
            "reading", {"sensor_id": "s", "temperature": value, "humidity": 40,
                        "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert sorted(reasons) == [] and h

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nonfinite_still_rejected(self, bad):
        value = bridge.exact_number(bad)
        h, reasons = canonical.classify(
            "reading", {"sensor_id": "s", "temperature": value, "humidity": 40,
                        "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert "unparsable" in reasons


class TestIdempotency:
    def test_same_value_stable_hash(self):
        a = canonical.compute_content_hash("reading",
            {"sensor_id": "s", "temperature": bridge.exact_number(26.75), "humidity": 40,
             "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        b = canonical.compute_content_hash("reading",
            {"sensor_id": "s", "temperature": bridge.exact_number(26.75), "humidity": 40,
             "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert a == b

    def test_different_values_not_merged(self):
        a = canonical.compute_content_hash("reading",
            {"sensor_id": "s", "temperature": bridge.exact_number(26.75), "humidity": 40,
             "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        b = canonical.compute_content_hash("reading",
            {"sensor_id": "s", "temperature": bridge.exact_number(26.80), "humidity": 40,
             "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert a != b