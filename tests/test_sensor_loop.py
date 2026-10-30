"""V4.4 E7a SampledSensorLoop 单元测试（Windows 离线,临时库+Fake 驱动,可注入 sleep）。"""

from __future__ import annotations

import dataclasses
import os
import tempfile

import pytest

from src.database.db import Database
from src.sensor import reader as _reader
from src.sensor.alarm import ALERT_TYPE, HighTempAlarm, SensorPersistError
from src.sensor.config import SensorConfig
from src.sensor.loop import SampledSensorLoop
from src.sensor.model import Reading
from src.sensor.sensor_errors import SensorReadError


def _cfg(high=1, _overrides=None) -> SensorConfig:
    cfg = SensorConfig(
        sensor_id="dht11-01",
        temperature_threshold=40.0,
        sample_period_seconds=1.0,
        confirm_high_count=high,
        confirm_recovery_count=1,
        cooldown_seconds=0.0,
        offline_after_failures=2,
    )
    if _overrides:
        cfg = dataclasses.replace(cfg, **_overrides)
    return cfg


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "loop_test.db"))
    d._tmpdir = tmp  # 保留引用, 防止临时目录被 GC 删除
    return d


def _no_sleep(_: float) -> None:
    return None


class _RawReader:
    """直接返回 Reading（不经 build_reading 默认量程），用于专门测试 loop 量程门。"""

    def __init__(self, temp, hum):
        self._t, self._h = temp, hum

    def read(self, now=None):
        ts = now() if now else "2026-10-26T09:00:00+08:00"
        return Reading(temperature_c=self._t, humidity_pct=self._h, captured_at=ts)



class TestLoop:
    def test_valid_reading_persists_and_alerts(self, db):
        loop = SampledSensorLoop(
            cfg=_cfg(high=1),
            reader=_reader.FakeReader(lambda: (45.0, 40.0)),
            alarm=HighTempAlarm(_cfg(high=1), db),
            db=db,
            sleep=_no_sleep,
        )
        res = loop.step()
        assert res["status"] == "valid" and res["event"]["type"] == "alert_created"
        assert len(db.query_readings(sensor_id="dht11-01", limit=1000)) == 1
        assert db.query_sensor_alerts(status="active", type=ALERT_TYPE, limit=1000)

    def test_invalid_no_persist_and_offline(self, db):
        loop = SampledSensorLoop(
            cfg=_cfg(),
            reader=_reader.FakeReader(lambda: (_ for _ in ()).throw(SensorReadError("fault"))),
            alarm=HighTempAlarm(_cfg(), db),
            db=db,
            sleep=_no_sleep,
        )
        r1 = loop.step()
        r2 = loop.step()
        assert r1["status"] == "error" and r2["fault_count"] == 2 and r2["offline"] is True
        assert len(db.query_readings(sensor_id="dht11-01", limit=1000)) == 0

    def test_valid_resets_after_fault(self, db):
        loop = SampledSensorLoop(
            cfg=_cfg(),
            reader=_reader.FakeReader(lambda: (_ for _ in ()).throw(SensorReadError("fault"))),
            alarm=HighTempAlarm(_cfg(), db),
            db=db, sleep=_no_sleep,
        )
        loop.step()
        assert loop.fault_count == 1
        # 换回有效读数
        loop.reader = _reader.FakeReader(lambda: (25.0, 40.0))
        r = loop.step()
        assert r["fault_count"] == 0 and r["offline"] is False
        assert len(db.query_readings(sensor_id="dht11-01", limit=1000)) == 1

    def test_persist_failure_surfaces(self, db):
        real = db

        class FailWriteDB:
            # __getattr__ 委托真实 db, 仅在 bridge 的事务写边界注入失败
            def __getattr__(self, name):
                return getattr(real, name)

            def _reading_write_core(self, *a, **k):
                raise RuntimeError("db write fail")

        done = {"closed": False}

        class ClosableReader(_reader.FakeReader):
            def close(self):
                done["closed"] = True

        loop = SampledSensorLoop(
            cfg=_cfg(),
            reader=ClosableReader(lambda: (25.0, 40.0)),
            alarm=HighTempAlarm(_cfg(), FailWriteDB()),
            db=FailWriteDB(),
            sleep=_no_sleep,
        )
        with pytest.raises(SensorPersistError):
            loop.step()
        loop.close()
        assert done["closed"] is True
        # 失败后无残留: reading/outbox/verify 均不应留下半成品
        assert len(real.query_readings(sensor_id="dht11-01", limit=1000)) == 0
        with real._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM c4_idem").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM c4_verify").fetchone()[0] == 0

    def test_close_called(self, db):
        done = {"closed": False}
        r = _reader.FakeReader(lambda: (25.0, 40.0))
        r.close = lambda: done.__setitem__("closed", True)  # 合法追加
        loop = SampledSensorLoop(_cfg(), r, HighTempAlarm(_cfg(), db), db, sleep=_no_sleep)
        loop.close()
        assert done["closed"] is True


class TestRangeEnforcement:
    """E7-02：量程越界的读数不持久化、不触发告警，走故障路径（A1）。"""

    def test_out_of_range_no_persist_no_alarm(self, db):
        loop = SampledSensorLoop(
            cfg=_cfg(high=1),
            reader=_RawReader(120.0, 40.0),  # 直接返回越界 Reading
            alarm=HighTempAlarm(_cfg(high=1), db),
            db=db,
            sleep=_no_sleep,
        )
        res = loop.step()
        # 数据无效 → invalid，不入 readings，不触发告警，不判离线
        assert res["status"] == "invalid"
        assert "量程" in (res.get("reason") or "")
        assert res["offline"] is False and res["fault_count"] == 0
        assert len(db.query_readings(sensor_id="dht11-01", limit=1000)) == 0
        assert db.query_sensor_alerts(status="active", type=ALERT_TYPE, limit=1000) == []

    def test_configurable_range_gates_alarm(self, db):
        # 调低温度上限到 30 → 45℃ 被量程拦截，不再告警/落库/判离线
        cfg = _cfg(high=1, _overrides={"temperature_max": 30.0})
        loop = SampledSensorLoop(
            cfg=cfg,
            reader=_RawReader(45.0, 40.0),
            alarm=HighTempAlarm(cfg, db),
            db=db,
            sleep=_no_sleep,
        )
        res = loop.step()
        assert res["status"] == "invalid" and "量程" in (res.get("reason") or "")
        assert len(db.query_readings(sensor_id="dht11-01", limit=1000)) == 0

    def test_boundary_valid(self, db):
        # 边界 85℃/100%RH 视为有效
        loop = SampledSensorLoop(
            cfg=_cfg(high=1),
            reader=_reader.FakeReader(lambda: (85.0, 100.0)),
            alarm=HighTempAlarm(_cfg(high=1), db),
            db=db,
            sleep=_no_sleep,
        )
        res = loop.step()
        assert res["status"] == "valid"
        assert len(db.query_readings(sensor_id="dht11-01", limit=1000)) == 1

    def test_many_invalid_do_not_offline(self, db):
        """单次/多次越界（数据质量问题）不得把仍连接的设备判为离线。"""
        loop = SampledSensorLoop(
            cfg=_cfg(high=1),
            reader=_RawReader(120.0, 40.0),
            alarm=HighTempAlarm(_cfg(high=1), db),
            db=db,
            sleep=_no_sleep,
        )
        for _ in range(5):
            res = loop.step()
        assert res["status"] == "invalid"
        assert loop.offline is False
        assert loop.fault_count == 0
        assert loop.quality_failures == 5
        assert db.get_sensor_status("dht11-01")["status"] == "degraded"


class TestDeviceFaultPersistence:
    """E7-07：设备读取故障→离线持久化→恢复→重启恢复（A3）。"""

    def _loop(self, db, reader):
        return SampledSensorLoop(
            cfg=_cfg(high=1),
            reader=reader,
            alarm=HighTempAlarm(_cfg(high=1), db),
            db=db,
            sleep=_no_sleep,
        )

    def test_device_fault_persists_offline(self, db):
        def fail():
            raise SensorReadError("crc fail")
        loop = self._loop(db, _reader.FakeReader(fail))
        # offline_after_failures 默认 2：第二次读取达到离线
        loop.step(); r2 = loop.step()
        assert r2["status"] == "offline" and r2["offline"] is True
        st = db.get_sensor_status("dht11-01")
        assert st is not None and st["status"] == "offline" and st["fault_type"] == "device"
        assert len(db.list_sensor_fault_events("dht11-01")) >= 1

    def test_recovery_persists_online(self, db):
        def fail():
            raise SensorReadError("x")
        loop = self._loop(db, _reader.FakeReader(fail))
        loop.step(); loop.step()  # offline
        # 设备恢复：换回有效读数
        loop.reader = _reader.FakeReader(lambda: (25.0, 40.0))
        r = loop.step()
        assert r["status"] == "valid" and r["recovered"] is True
        st = db.get_sensor_status("dht11-01")
        assert st["status"] == "online" and st["recovered_at"]
        assert st["consecutive_failures"] == 0

    def test_restart_restores_offline(self, db):
        def fail():
            raise SensorReadError("y")
        loop = self._loop(db, _reader.FakeReader(fail))
        loop.step(); loop.step()  # 进入 offline 并持久化
        assert db.get_sensor_status("dht11-01")["status"] == "offline"
        # 模拟 "重启"：重建 loop，不读 new 硬件数据，先 restore
        loop2 = self._loop(db, _reader.FakeReader(lambda: (25.0, 40.0)))
        summary = loop2.restore_state()
        assert summary["restored"] is True and summary["offline"] is True
        assert loop2.offline is True
        # 重启后首次有效读数 → 恢复 online（不会把已恢复误判为未恢复）
        r = loop2.step()
        assert r["status"] == "valid" and db.get_sensor_status("dht11-01")["status"] == "online"
