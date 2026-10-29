"""V4.4 E7a 高温告警状态机单元测试（临时库 + 显式测试参数）。"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.database.db import Database
from src.sensor.alarm import ALERT_TYPE, HighTempAlarm, SensorPersistError
from src.sensor.config import SensorConfig

TS0 = "2026-10-26T10:00:00+08:00"
TS1 = "2026-10-26T10:00:30+08:00"
TS2 = "2026-10-26T10:02:00+08:00"


def _cfg(high=2, recovery=2, cooldown=0.0) -> SensorConfig:
    return SensorConfig(
        sensor_id="dht11-01",
        temperature_threshold=40.0,
        sample_period_seconds=1.0,
        confirm_high_count=high,
        confirm_recovery_count=recovery,
        cooldown_seconds=cooldown,
    )


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "alarm_test.db"))
    d._tmpdir = tmp  # 保留引用, 防止临时目录被 GC 删除
    return d


def _active_count(db):
    return len(db.query_sensor_alerts(status="active", type=ALERT_TYPE, limit=1000))


class TestAlarm:
    def test_below_threshold_no_alert(self, db):
        a = HighTempAlarm(_cfg(), db)
        assert a.process(20.0, 30.0, TS0) is None
        assert a.state == "normal"

    def test_confirm_high_creates(self, db):
        a = HighTempAlarm(_cfg(high=2), db)
        assert a.process(45.0, 30.0, TS0) is None          # count1
        ev = a.process(45.0, 30.0, TS1)                     # count2
        assert ev is not None and ev["type"] == "alert_created"
        assert a.state == "active"
        assert _active_count(db) == 1

    def test_no_duplicate_active(self, db):
        a = HighTempAlarm(_cfg(high=1), db)
        a.process(45.0, 30.0, TS0)
        assert _active_count(db) == 1
        ev = a.process(45.0, 30.0, TS1)      # 已活动: 不重复建/不重复通知
        assert ev is None and _active_count(db) == 1

    def test_high_interrupted_by_normal(self, db):
        a = HighTempAlarm(_cfg(high=2), db)
        a.process(45.0, 30.0, TS0)             # count1
        a.process(20.0, 30.0, TS1)             # normal 打断
        ev = a.process(45.0, 30.0, TS2)        # 重新计1(尚未到2)
        assert ev is None and a.state == "confirm_high"

    def test_recovery_resolves(self, db):
        a = HighTempAlarm(_cfg(high=1, recovery=2), db)
        a.process(45.0, 30.0, TS0)             # active
        a.process(20.0, 30.0, TS0)             # 恢复1
        ev = a.process(20.0, 30.0, TS1)        # 恢复2 -> 解除
        assert ev is not None and ev["type"] == "alert_resolved"
        row = db.query_sensor_alerts(status="resolved", type=ALERT_TYPE, limit=1000)
        assert len(row) == 1

    def test_read_fault_resets(self, db):
        a = HighTempAlarm(_cfg(high=2), db)
        a.process(45.0, 30.0, TS0)             # count1
        a.on_read_fault()                       # 读取失败: 重置计数
        ev = a.process(45.0, 30.0, TS1)
        assert ev is None and a.state == "confirm_high" and a.high_count == 1

    def test_cooldown_suppresses(self, db):
        a = HighTempAlarm(_cfg(high=1, recovery=2, cooldown=60.0), db)
        a.process(45.0, 30.0, TS0)             # active
        a.process(20.0, 30.0, TS0)
        a.process(20.0, 30.0, TS0)             # resolved (恢复2==2)
        ev = a.process(45.0, 30.0, TS1)        # 冷却期内(30s<60) → 不新建
        assert ev is None and _active_count(db) == 0
        ev = a.process(45.0, 30.0, TS2)        # 冷却已过(120s) → 新建
        assert ev is not None and ev["type"] == "alert_created"

    def test_db_write_failure_no_advance_no_dup(self, db):
        real = db
        state = {"fail": True}

        class FailingDB:
            def __getattr__(self, name):
                return getattr(real, name)

            def _sensor_alert_write_core(self, *a, **k):
                # bridge 经由 _sensor_alert_write_core 落库; 在此注入持久化失败
                if state["fail"]:
                    raise RuntimeError("disk full")
                return real._sensor_alert_write_core(*a, **k)

        a = HighTempAlarm(_cfg(high=1), FailingDB())
        with pytest.raises(RuntimeError):
            a.process(45.0, 30.0, TS0)   # 写库失败: 不推进活动态, 不静默
        assert a.state != "active" and a.active_id is None
        state["fail"] = False
        ev = a.process(45.0, 30.0, TS1)  # 重试成功: 创建一次, 不重复
        assert ev["type"] == "alert_created"
        assert _active_count(real) == 1