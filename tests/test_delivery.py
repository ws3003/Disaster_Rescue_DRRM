"""V4.4 P1-B 投递登记表 (deliveries) DAO 测试。

隔离策略: 全部使用 tempfile 临时 SQLite, 不触碰 data/drrm.db / src/data/drrm.db。

职责边界 (已确认设计):
- 幂等复合键 (source, alert_id, task_kind)。
- 状态仅 pending/success, 仅 pending -> success, success 为终态。
- task_kind 为开放 TEXT (不设枚举白名单)。
- 重复登记不覆盖 status/created_at。
- success 仅表示本期记录被标记成功, 不代表远端收到/处理。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading

import pytest

from src.database.db import Database

CREATED = "2026-10-27T12:00:00+08:00"


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    db = Database(db_path=os.path.join(tmp.name, "delivery_test.db"))
    yield db


def _save(db, *, source="sensor", alert_id="E-20261027-A2B3C4D5", task_kind="sync", **over):
    kw = dict(source=source, alert_id=alert_id, task_kind=task_kind)
    kw.update(over)
    return db.save_delivery(**kw)


class TestSaveGet:
    def test_first_create(self, db):
        key = _save(db)
        assert key == ("sensor", "E-20261027-A2B3C4D5", "sync")
        got = db.get_delivery("sensor", "E-20261027-A2B3C4D5", "sync")
        assert got is not None
        assert got["status"] == "pending"
        assert got["created_at"]  # 带时区默认时间

    def test_explicit_created_at(self, db):
        _save(db, created_at=CREATED)
        got = db.get_delivery("sensor", "E-20261027-A2B3C4D5", "sync")
        assert got["created_at"] == CREATED

    def test_bad_created_at_rejected(self, db):
        with pytest.raises(ValueError):
            _save(db, created_at="2026-10-27T12:00:00")  # 无时区


class TestIdempotency:
    def test_idempotent_recreate(self, db):
        _save(db)
        _save(db)  # 重复登记
        rows = db.list_deliveries()
        assert len(rows) == 1

    def test_recreate_not_overwrite_status(self, db):
        _save(db)
        db.update_delivery_status("sensor", "E-20261027-A2B3C4D5", "sync", status="success")
        _save(db)  # 重放
        got = db.get_delivery("sensor", "E-20261027-A2B3C4D5", "sync")
        assert got["status"] == "success"

    def test_recreate_not_overwrite_created_at(self, db):
        _save(db, created_at=CREATED)
        _save(db, created_at="2026-10-27T13:00:00+08:00")  # 重放用不同时间
        got = db.get_delivery("sensor", "E-20261027-A2B3C4D5", "sync")
        assert got["created_at"] == CREATED

    def test_recreate_returns_same_key(self, db):
        k1 = _save(db)
        k2 = _save(db)
        assert k1 == k2


class TestDistinctKeys:
    def test_different_source_coexist(self, db):
        _save(db, source="sensor")
        _save(db, source="person", alert_id="E-11112222-09ABFECD")
        rows = db.list_deliveries()
        assert len(rows) == 2
        assert {r["source"] for r in rows} == {"person", "sensor"}

    def test_different_task_kind_coexist(self, db):
        _save(db, task_kind="sync")
        _save(db, task_kind="media")
        rows = db.list_deliveries()
        assert len(rows) == 2


class TestConstraints:
    def test_primary_key_uniqueness_raw(self, db):
        _save(db)
        with db._connect() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO deliveries (source, alert_id, task_kind, status, created_at)
                    VALUES ('sensor', 'E-20261027-A2B3C4D5', 'sync', 'pending', ?)
                    """,
                    (CREATED,),
                )

    def test_invalid_source_sql_rejected(self, db):
        with db._connect() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO deliveries (source, alert_id, task_kind, created_at)
                    VALUES ('foobar', 'E-20261027-A2B3C4D5', 'sync', ?)
                    """,
                    (CREATED,),
                )

    def test_invalid_status_sql_rejected(self, db):
        with db._connect() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO deliveries (source, alert_id, task_kind, status, created_at)
                    VALUES ('sensor', 'E-20261027-A2B3C4D5', 'sync', 'done', ?)
                    """,
                    (CREATED,),
                )

    def test_missing_not_null_rejected_raw(self, db):
        with db._connect() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO deliveries (source, alert_id, task_kind, status, created_at)
                    VALUES ('sensor', 'E-20261027-A2B3C4D5', NULL, 'pending', ?)
                    """,
                    (CREATED,),
                )


class TestValidation:
    def test_invalid_source_dao_rejected(self, db):
        with pytest.raises(ValueError):
            _save(db, source="foobar")

    def test_empty_alert_id_rejected(self, db):
        with pytest.raises(ValueError):
            _save(db, alert_id="")

    def test_empty_task_kind_rejected(self, db):
        with pytest.raises(ValueError):
            _save(db, task_kind="")

    def test_task_kind_open_no_whitelist(self, db):
        # task_kind 开放 TEXT: 任意非空字符串可登记, 不做枚举白名单
        _save(db, task_kind="custom-xyz")
        got = db.get_delivery("sensor", "E-20261027-A2B3C4D5", "custom-xyz")
        assert got is not None

    def test_list_invalid_status_rejected(self, db):
        with pytest.raises(ValueError):
            db.list_deliveries(status="done")


class TestStatusMachine:
    def test_pending_to_success(self, db):
        _save(db)
        assert db.update_delivery_status(
            "sensor", "E-20261027-A2B3C4D5", "sync", status="success"
        ) is True
        assert db.get_delivery("sensor", "E-20261027-A2B3C4D5", "sync")["status"] == "success"

    def test_success_to_pending_forbidden(self, db):
        _save(db)
        db.update_delivery_status("sensor", "E-20261027-A2B3C4D5", "sync", status="success")
        assert db.update_delivery_status(
            "sensor", "E-20261027-A2B3C4D5", "sync", status="pending"
        ) is False
        assert db.get_delivery("sensor", "E-20261027-A2B3C4D5", "sync")["status"] == "success"

    def test_duplicate_success_not_new_delivery(self, db):
        _save(db)
        db.update_delivery_status("sensor", "E-20261027-A2B3C4D5", "sync", status="success")
        assert db.update_delivery_status(
            "sensor", "E-20261027-A2B3C4D5", "sync", status="success"
        ) is False
        rows = db.list_deliveries()
        assert len(rows) == 1

    def test_unknown_status_rejected(self, db):
        _save(db)
        with pytest.raises(ValueError):
            db.update_delivery_status(
                "sensor", "E-20261027-A2B3C4D5", "sync", status="failed"
            )

    def test_update_missing_record_false(self, db):
        assert db.update_delivery_status(
            "sensor", "E-00000000-00000000", "sync", status="success"
        ) is False

    def test_update_does_not_clobber_other_fields(self, db):
        _save(db, created_at=CREATED)
        db.update_delivery_status("sensor", "E-20261027-A2B3C4D5", "sync", status="success")
        got = db.get_delivery("sensor", "E-20261027-A2B3C4D5", "sync")
        assert got["created_at"] == CREATED and got["alert_id"] == "E-20261027-A2B3C4D5"


class TestIntegrityGuard:
    def test_non_idempotent_integrity_raises(self, db, monkeypatch):
        # 已存在同键记录, 但模拟"回查未命中"(如 IntegrityError 来自非幂等约束):
        # 不得误判为幂等冲突, 必须重抛原异常。
        _save(db)
        monkeypatch.setattr(db, "get_delivery", lambda *a, **k: None)
        with pytest.raises(sqlite3.IntegrityError):
            _save(db)

    def test_update_missing_with_invalid_status_still_raises(self, db):
        # 非法状态优先于记录存在性: 记录不存在 + 非法 status -> ValueError
        with pytest.raises(ValueError):
            db.update_delivery_status("sensor", "E-00000000-00000000", "sync", status="nope")


class TestInitCompat:
    def test_old_db_gets_deliveries_preserves_data(self, tmp_path):
        # 模拟 P1-B 之前的旧库 (仅含 V4.3 核心表 + 一条数据), 不预建 deliveries。
        old_db = os.path.join(tmp_path, "old.db")
        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE persons (
                person_id INTEGER PRIMARY KEY,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                latest_state TEXT, latest_risk REAL
            );
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY, ts TEXT NOT NULL,
                type TEXT NOT NULL, severity TEXT, person_id INTEGER, payload TEXT
            );
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, trigger_event TEXT, plan_trace TEXT,
                status TEXT DEFAULT 'running', created TEXT, finished TEXT
            );
            INSERT INTO persons (person_id, first_seen, last_seen) VALUES (1, 'a', 'b');
            """
        )
        conn.commit()
        conn.close()

        # 实例化 Database 跑完整 schema.sql (IF NOT EXISTS): 应补建新表且保留旧数据。
        db = Database(db_path=old_db)
        assert db.get_person(1) is not None  # 旧数据保留
        # 新表可查询/写入
        assert db.list_deliveries() == []
        db.save_delivery(source="person", alert_id="E-11112222-09ABFECD", task_kind="sync")
        assert db.get_delivery("person", "E-11112222-09ABFECD", "sync") is not None


class TestConcurrency:
    def test_two_instances_same_key_idempotent(self, tmp_path):
        # 双 Database 实例对同一隔离临时文件并发登记同一复合键: 至多一条, 均不抛错优先。
        db_file = os.path.join(tmp_path, "concurrent.db")
        a = Database(db_path=db_file)
        b = Database(db_path=db_file)
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def _go(d: Database):
            try:
                barrier.wait(timeout=5)
                results.append(d.save_delivery(
                    source="sensor", alert_id="E-20261027-0A0A0A0A", task_kind="sync"
                ))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        ta = threading.Thread(target=_go, args=(a,))
        tb = threading.Thread(target=_go, args=(b,))
        ta.start(); tb.start()
        ta.join(timeout=10); tb.join(timeout=10)

        assert not errors, errors
        assert len(results) == 2
        dups = {r for r in results}
        assert len(dups) == 1
        # 库中至多一条同复合键
        rows = a.list_deliveries()
        assert len([r for r in rows
                    if r["source"] == "sensor" and r["alert_id"] == "E-20261027-0A0A0A0A"
                    and r["task_kind"] == "sync"]) == 1


class TestIsolation:
    def test_uses_temp_database(self, db):
        # 隔离: 数据库连接必须是临时文件名, 而非默认 data/drrm.db
        assert os.path.basename(str(db.db_path)) == "delivery_test.db"
        assert str(db.db_path) != "data/drrm.db"