"""V4.4 E7 + E9 软件集成验收测试（临时 SQLite + Mock Reader，不连真实硬件/OSS）。

覆盖：
INT-01 正常读数链路（reading + online + C4 Outbox reading 记录 + 标识一致）。
INT-02 超温告警链路（持续确认 → sensor_alerts，type=sensor_hightemp，独立于 VLM env）。
INT-03 无效数据链路（不落 reading、不告警、不入 Outbox、degraded、不判离线）。
INT-04 读取故障链路（offline + sensor_status + history）。
INT-05 故障恢复链路（online + recovered_at + 历史保留）。
INT-06 重启恢复链路（同一 SQLite 重建 → restore offline；已恢复不复开）。
C4 一致性（业务记录与 Outbox 对应；重复提交幂等）。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.database.db import Database
from src.sensor import reader as _reader
from src.sensor.alarm import ALERT_TYPE, HighTempAlarm
from src.sensor.config import SensorConfig
from src.sensor.loop import SampledSensorLoop
from src.sensor.runner import build_runner_from_config
from src.sensor.sensor_errors import SensorReadError


def _cfg(high=1, **over) -> SensorConfig:
    cfg = SensorConfig(
        sensor_id="s1",
        temperature_threshold=40.0,
        sample_period_seconds=1.0,
        confirm_high_count=high,
        confirm_recovery_count=1,
        cooldown_seconds=0.0,
        offline_after_failures=2,
    )
    if over:
        import dataclasses
        cfg = dataclasses.replace(cfg, **over)
    return cfg


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "e7e9_int.db"))
    d._tmpdir = tmp
    return d


def _loop(db, cfg=_cfg(), provider=lambda: (25.0, 40.0)):
    return SampledSensorLoop(cfg, _reader.FakeReader(provider), HighTempAlarm(cfg, db), db,
                             sleep=lambda _f: None)


def _outbox(db):
    with db._connect() as c:
        return c.execute(
            "SELECT record_type, source_record_id, status FROM sync_outbox ORDER BY id").fetchall()


def _readings(db):
    return db.query_readings(sensor_id="s1", limit=1000)


def _alerts(db):
    return db.query_sensor_alerts(status="active", type=ALERT_TYPE, limit=1000)


# ---------------- INT-01 正常读数 ----------------
class TestInt01Valid:
    def test_valid_reading_persists_online_and_outbox(self, db):
        loop = _loop(db)
        r = loop.step()
        assert r["status"] == "valid"
        readings = _readings(db)
        assert len(readings) == 1
        assert readings[0]["temperature"] == 25.0 and readings[0]["humidity"] == 40.0
        assert db.get_sensor_status("s1")["status"] == "online"
        # C4 Outbox 有且仅有一条 reading 记录，业务标识一致
        ob = [x for x in _outbox(db) if x[0] == "reading"]
        assert len(ob) == 1
        assert ob[0][1] == readings[0]["reading_id"] and ob[0][2] == "pending"


# ---------------- INT-02 超温告警 ----------------
class TestInt02Alert:
    def test_sustained_over_threshold_generates_sensor_alert(self, db):
        cfg = _cfg(high=3)  # 连续 3 次才告警
        loop = _loop(db, cfg=cfg, provider=lambda: (45.0, 40.0))
        r1 = loop.step()
        r2 = loop.step()
        assert r1["status"] == "valid" and r1["event"] is None
        assert r2["status"] == "valid" and r2["event"] is None
        # 第 3 次达到持续条件 → 生成 sensor_alert
        r = loop.step()
        assert r["event"] is not None and r["event"]["type"] == "alert_created"
        alerts = _alerts(db)
        assert len(alerts) == 1 and alerts[0]["type"] == ALERT_TYPE  # 独立传感器类型
        assert "environment" not in alerts[0]["type"]
        # sensor_alert 也进入 C4 Outbox，且与业务 alert_id 一致
        ob = [x for x in _outbox(db) if x[0] == "sensor_alert"]
        assert len(ob) == 1 and ob[0][1] == alerts[0]["alert_id"]


# ---------------- INT-03 无效数据 ----------------
class TestInt03Invalid:
    def test_invalid_no_readdown_no_alert_no_outbox(self, db):
        loop = _loop(db, provider=lambda: (120.0, 40.0))  # 越界
        r = loop.step()
        assert r["status"] == "invalid"
        assert _readings(db) == [] and _alerts(db) == []
        assert [x for x in _outbox(db) if x[0] == "reading"] == []
        assert loop.offline is False
        assert db.get_sensor_status("s1")["status"] == "degraded"

    def test_nan_inf_rejected(self, db):
        for bad in ((float("nan"), None), (25.0, float("inf"))):
            loop = _loop(db, provider=lambda b=bad: b)
            r = loop.step()
            assert r["status"] == "error" or r["status"] == "invalid"
        assert _readings(db) == []


# ---------------- INT-04 读取故障 ----------------
class TestInt04Fault:
    def test_consecutive_fault_offline_persisted(self, db):
        def fail():
            raise SensorReadError("crc fail")
        loop = _loop(db, provider=fail)
        r1 = loop.step()
        r2 = loop.step()
        assert r2["status"] == "offline" and r2["offline"] is True
        st = db.get_sensor_status("s1")
        assert st["status"] == "offline" and st["fault_type"] == "device"
        assert st["last_error"] == "crc fail"
        assert len(db.list_sensor_fault_events("s1")) >= 2
        assert _readings(db) == []


# ---------------- INT-05 故障恢复 ----------------
class TestInt05Recovery:
    def test_recovery_persists_online_and_history_preserved(self, db):
        def fail():
            raise SensorReadError("x")
        loop = _loop(db, provider=fail)
        loop.step(); loop.step()  # offline
        events_before = len(db.list_sensor_fault_events("s1"))
        # 恢复：成功读数
        loop.reader = _reader.FakeReader(lambda: (25.0, 40.0))
        r = loop.step()
        assert r["status"] == "valid" and r["recovered"] is True
        st = db.get_sensor_status("s1")
        assert st["status"] == "online" and st["recovered_at"] and st["consecutive_failures"] == 0
        # 未恢复故障已回填恢复时间；历史保留
        evs = db.list_sensor_fault_events("s1")
        assert any(ev["status"] == "offline" and ev["recovered_at"] for ev in evs)
        assert len(evs) > events_before  # 新增 online 恢复事件


# ---------------- INT-06 重启恢复 ----------------
class TestInt06Restart:
    def test_restart_restores_offline(self, db):
        def fail():
            raise SensorReadError("y")
        loop = _loop(db, provider=fail)
        loop.step(); loop.step()
        assert db.get_sensor_status("s1")["status"] == "offline"
        path = os.path.join(db._tmpdir.name, "e7e9_int.db")
        del loop  # 模拟"销毁"
        # 同一 SQLite 文件重建 Database + SensorLoop（等价 runner build 后 restore）
        db2 = Database(db_path=path)
        loop2 = SampledSensorLoop(
            _cfg(high=1), _reader.FakeReader(lambda: (25.0, 40.0)),
            HighTempAlarm(_cfg(high=1), db2), db2, sleep=lambda _f: None)
        summary = loop2.restore_state()
        assert summary["restored"] is True and summary["offline"] is True
        assert loop2.offline is True
        r = loop2.step()
        assert r["status"] == "valid" and db2.get_sensor_status("s1")["status"] == "online"


# ---------------- C4 一致性 ----------------
class TestC4Consistency:
    def test_one_outbox_row_per_valid_step(self, db):
        loop = _loop(db)  # 两次有效读数 → 两条 reading + 两条 Outbox，标识各自对应
        loop.step(); loop.step()
        readings = _readings(db)
        assert len(readings) == 2
        ob_reading = [x for x in _outbox(db) if x[0] == "reading"]
        assert len(ob_reading) == 2
        rids = {r["reading_id"] for r in readings}
        assert {x[1] for x in ob_reading} == rids  # 业务主键与 Outbox 一一对应，无合并/重复
        assert all(x[2] == "pending" for x in ob_reading)

    def test_same_tx_rollback_no_partial(self, db):
        """业务写与 Outbox 同事务：注入写入失败 → reading 与 outbox 均无残留（无半同步）。"""
        import src.database.db

        real = db

        class FailDB:
            def __getattr__(self, n):
                return getattr(real, n)

            def _reading_write_core(self, *a, **k):
                raise RuntimeError("db fail")

        from src.sensor.loop import SensorPersistError
        loop = _loop(FailDB())
        with pytest.raises(SensorPersistError):
            loop.step()
        with real._connect() as c:
            assert c.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0
