"""E7 两级高温告警测试（新功能；单级兼容已由 test_sensor_alarm 回归保证）。

参数按本轮联调基线（测试用显式值，非现场认证标准）：
一级 40/二级 50/恢复 36；确认 3/确认 3；冷却 600s；湿度不参与。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from src.database.db import Database
from src.sensor.alarm import ALERT_TYPE, HighTempAlarm
from src.sensor.config import SensorConfig


def _cfg() -> SensorConfig:
    return SensorConfig(
        sensor_id="s1", temperature_threshold=40.0, level2_threshold=50.0, recovery_threshold=36.0,
        sample_period_seconds=1.0, confirm_high_count=3, confirm_recovery_count=3, cooldown_seconds=600.0)


_BASE = datetime(2026, 10, 28, 10, 0, 0, tzinfo=timezone.utc)


def ts(sec: int) -> str:
    """从基准时间后偏移 sec 秒，生成合法带时区 ISO。"""
    return (_BASE + timedelta(seconds=sec)).isoformat(timespec="seconds")


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "twolevel_test.db"))
    d._tmpdir = tmp
    return d


def _sensors(db, status=None):
    return db.query_sensor_alerts(status=status, type=ALERT_TYPE, limit=1000)


class TestTwoLevel:
    def test_level1_created_stable_id(self, db):
        a = HighTempAlarm(_cfg(), db)
        assert a.process(45.0, None, ts(0)) is None      # count1
        assert a.process(45.0, None, ts(10)) is None     # count2
        ev = a.process(45.0, None, ts(20))               # count3 -> 一级创建
        assert ev is not None and ev["type"] == "alert_created" and ev["level"] == 1
        assert a.state == "active" and a.active_level == 1
        assert len(_sensors(db, "active")) == 1
        got = db.query_sensor_alerts(limit=1)[0]
        assert got["alert_id"] == ev["alert_id"]

    def test_level2_upgrade_same_alert(self, db):
        a = HighTempAlarm(_cfg(), db)
        created = None
        for i, t in enumerate([45.0, 45.0, 45.0]):
            e = a.process(t, None, ts(i * 10))
            if e: created = e
        # 持续二级 55：第二次(累计第2)仍 None，第三次升级
        assert a.process(55.0, None, ts(30)) is None
        assert a.process(55.0, None, ts(40)) is None
        ev = a.process(55.0, None, ts(50))
        assert ev is not None and ev["type"] == "alert_upgraded" and ev["level"] == 2
        assert ev["alert_id"] == created["alert_id"]          # 同告警 ID 稳定
        assert a.active_level == 2
        assert len(_sensors(db)) == 1                          # 升级不新建重复告警
        assert a.process(55.0, None, ts(60)) is None           # 已二级持续: 去重

    def test_recovery_resolves(self, db):
        a = HighTempAlarm(_cfg(), db)
        for i in range(3):
            a.process(45.0, None, ts(i * 10))
        assert a.process(30.0, None, ts(30)) is None
        assert a.process(30.0, None, ts(40)) is None
        ev = a.process(30.0, None, ts(50))
        assert ev is not None and ev["type"] == "alert_resolved"
        assert len(_sensors(db, "resolved")) == 1

    def test_cooldown_suppresses_then_allows(self, db):
        a = HighTempAlarm(_cfg(), db)
        for i in range(3):
            a.process(45.0, None, ts(i * 10))
        for i in range(3):
            a.process(30.0, None, ts(30 + i * 10))   # 恢复
        # 冷却期内(距 resolve 30s<600s) 再次 3 次一级 -> 不新建
        for i in range(3):
            assert a.process(45.0, None, ts(200 + i * 10)) is None
        assert len(_sensors(db, "active")) == 0
        # 冷却已过(距 resolve 700s>600s) -> 新建
        a2 = a  # 同一实例, _last_resolved_ts 已设
        ev = None
        for i in range(3):
            e = a2.process(45.0, None, ts(800 + i * 10))
            if e: ev = e
        assert ev is not None and ev["type"] == "alert_created"

    def test_event_fields(self, db):
        a = HighTempAlarm(_cfg(), db)
        ev = a.process(45.0, None, ts(0)); ev = a.process(45.0, None, ts(10))
        ev = a.process(45.0, None, ts(20))
        assert {"type", "alert_id", "level", "temperature", "threshold", "occurred_at"} <= set(ev.keys())
        assert ev["level"] == 1 and ev["threshold"] == 40.0

    def test_level1_does_not_imply_level2(self, db):
        # 只到一级（45）不会冒用级别 2
        a = HighTempAlarm(_cfg(), db)
        ev = None
        for i in range(6):   # 持续一级累计多次, 不升级
            e = a.process(45.0, None, ts(i * 10))
            if e: ev = e
        assert ev["level"] == 1
        # 二级升级需要 3 次持续 ≥50
        assert a.process(55.0, None, ts(60)) is None
        assert a.process(55.0, None, ts(70)) is None
        e2 = a.process(55.0, None, ts(80))
        assert e2["type"] == "alert_upgraded"