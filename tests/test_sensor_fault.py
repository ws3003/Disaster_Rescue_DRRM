"""V4.4 E7-07 传感器故障持久化 DAO 与迁移测试（A3）。

- DAO：sensor_status upsert、sensor_fault_events 记录/幂等恢复。
- 迁移：空库初始化、旧库升级（保留旧数据）、重复执行幂等。
不连接真实硬件。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from src.database.db import Database

TS = "2026-10-26T09:00:00+08:00"


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "fault_test.db"))
    d._tmpdir = tmp
    return d


class TestDao:
    def test_status_upsert_and_get(self, db):
        assert db.get_sensor_status("s1") is None
        db.save_sensor_status(sensor_id="s1", status="offline", fault_type="device",
                              last_error="crc", consecutive_failures=3, updated_at=TS)
        st = db.get_sensor_status("s1")
        assert st["status"] == "offline" and st["consecutive_failures"] == 3
        # 幂等 upsert：同 sensor_id 覆盖，不新增重复行
        db.save_sensor_status(sensor_id="s1", status="online", consecutive_failures=0, updated_at=TS)
        rows = db.get_sensor_status("s1")
        assert rows["status"] == "online" and rows["consecutive_failures"] == 0
        assert db.list_sensor_fault_events("s1") == []

    def test_fault_event_record_and_recover(self, db):
        ev = db.record_sensor_fault_event(
            sensor_id="s1", status="offline", fault_type="device", detail="crc", occurred_at=TS)
        got = db.list_sensor_fault_events("s1")
        assert len(got) == 1 and got[0]["status"] == "offline" and got[0]["recovered_at"] is None
        # 幂等恢复：仅未恢复项可回填；再调不覆盖
        assert db.mark_sensor_fault_event_recovered(ev, recovered_at=TS) is True
        assert db.mark_sensor_fault_event_recovered(ev, recovered_at=TS) is False
        assert db.list_sensor_fault_events("s1")[0]["recovered_at"] == TS

    def test_bad_status_rejected(self, db):
        with pytest.raises(ValueError):
            db.save_sensor_status(sensor_id="s1", status="bogus", updated_at=TS)


class TestMigration:
    def _old_db(self, path):
        """构造一个只有 V4.3 四表 + 一行数据的"旧库"，未含 A3 新表。"""
        c = sqlite3.connect(path)
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS persons (
                person_id INTEGER PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                latest_state TEXT, latest_risk REAL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, ts TEXT NOT NULL, type TEXT NOT NULL,
                severity TEXT, person_id INTEGER, payload TEXT
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, trigger_event TEXT, plan_trace TEXT,
                status TEXT DEFAULT 'running', created TEXT, finished TEXT
            );
            CREATE TABLE IF NOT EXISTS agent_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, ts TEXT NOT NULL,
                tool_name TEXT NOT NULL, input TEXT, output TEXT, latency_ms INTEGER
            );
            INSERT INTO persons (person_id, first_seen, last_seen, latest_state, latest_risk)
            VALUES (1, '2026-10-01T00:00:00+08:00', '2026-10-01T00:00:00+08:00', 'Standing', 0.1);
            INSERT INTO events (event_id, ts, type, severity, person_id, payload)
            VALUES ('E-20261001-ABC', '2026-10-01T00:00:00+08:00', 'high_risk_alert', 'HIGH', 1, '{}');
            """
        )
        c.commit()
        c.close()

    def test_empty_db_init_creates_new_tables(self, tmp_path):
        p = os.path.join(tmp_path, "new.db")
        d = Database(db_path=p)
        with d._connect() as c:
            assert c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sensor_status'").fetchone()
            assert c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sensor_fault_events'").fetchone()

    def test_old_db_upgrade_preserves_data(self, tmp_path):
        p = os.path.join(tmp_path, "old.db")
        self._old_db(p)
        d = Database(db_path=p)  # 初始化 → 追加 A3 新表
        with d._connect() as c:
            # 旧表与数据保留
            assert c.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
            # 新表已建
            assert c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sensor_status'").fetchone()
            assert c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sensor_fault_events'").fetchone()

    def test_repeat_init_idempotent(self, tmp_path):
        p = os.path.join(tmp_path, "idem.db")
        d1 = Database(db_path=p)
        d1.save_sensor_status(sensor_id="s1", status="offline", updated_at=TS)
        d2 = Database(db_path=p)  # 再次初始化（IF NOT EXISTS 幂等）
        assert d2.get_sensor_status("s1")["status"] == "offline"  # 数据保留、不重复建表
        with d2._connect() as c:
            assert c.execute("SELECT COUNT(*) FROM sensor_status").fetchone()[0] == 1