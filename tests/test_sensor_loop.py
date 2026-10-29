"""V4.4 E7a SampledSensorLoop 单元测试（Windows 离线,临时库+Fake 驱动,可注入 sleep）。"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.database.db import Database
from src.sensor import reader as _reader
from src.sensor.alarm import ALERT_TYPE, HighTempAlarm, SensorPersistError
from src.sensor.config import SensorConfig
from src.sensor.loop import SampledSensorLoop
from src.sensor.sensor_errors import SensorReadError


def _cfg(high=1) -> SensorConfig:
    return SensorConfig(
        sensor_id="dht11-01",
        temperature_threshold=40.0,
        sample_period_seconds=1.0,
        confirm_high_count=high,
        confirm_recovery_count=1,
        cooldown_seconds=0.0,
        offline_after_failures=2,
    )


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "loop_test.db"))
    d._tmpdir = tmp  # 保留引用, 防止临时目录被 GC 删除
    return d


def _no_sleep(_: float) -> None:
    return None


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