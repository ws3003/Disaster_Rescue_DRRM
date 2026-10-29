"""V4.4 P1-A `Database.with_transaction()` 单元测试。

隔离：全部使用 tempfile 临时 SQLite；不触碰 data/drrm.db。
通过 monkeypatch `_connect` 注入可控连接包装器(TrackedConn) 验证 commit/rollback/close
调用次数并注入异常，以验证异常传播链与清理行为。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from src.database.db import Database


class TrackedConn:
    """包装真实 sqlite3.Connection；跟踪调用并可注入异常。"""

    def __init__(self, real: sqlite3.Connection):
        self._real = real
        self.commit_called = 0
        self.rollback_called = 0
        self.close_called = 0
        self.fail_commit = False
        self.fail_rollback = False
        self.fail_close = False

    def execute(self, *args, **kwargs):
        return self._real.execute(*args, **kwargs)

    def commit(self):
        self.commit_called += 1
        if self.fail_commit:
            raise sqlite3.OperationalError("commit failed (injected)")
        self._real.commit()

    def rollback(self):
        self.rollback_called += 1
        if self.fail_rollback:
            raise sqlite3.OperationalError("rollback failed (injected)")
        self._real.rollback()

    def close(self):
        self.close_called += 1
        if self.fail_close:
            raise sqlite3.OperationalError("close failed (injected)")
        self._real.close()


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=os.path.join(str(tmp_path), "tx_test.db"))
    return d


def _tracked(db: Database) -> TrackedConn:
    # 包装到 schematized 的 db.db_path(同一临时库), 复用其建表后的 schema
    real = sqlite3.connect(str(db.db_path))
    real.row_factory = sqlite3.Row
    return TrackedConn(real)


def _row_count(path: str, table: str) -> int:
    with sqlite3.connect(path) as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestBasic:
    def test_multi_write_commits(self, db, tmp_path, monkeypatch):
        tr = _tracked(db)
        monkeypatch.setattr(db, "_connect", lambda: tr)
        with db.with_transaction() as conn:
            conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r1','s1',1.0,'2026-10-26T10:00:00+08:00')")
            conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r2','s1',2.0,'2026-10-26T10:00:00+08:00')")
        assert tr.commit_called == 1 and tr.rollback_called == 0 and tr.close_called == 1
        assert _row_count(str(db.db_path), "readings") == 2
        assert db._tx is False

    def test_business_exception_rolls_back(self, db, tmp_path, monkeypatch):
        tr = _tracked(db)
        monkeypatch.setattr(db, "_connect", lambda: tr)
        with pytest.raises(ValueError) as ei:
            with db.with_transaction() as conn:
                conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r1','s1',1.0,'2026-10-26T10:00:00+08:00')")
                raise ValueError("boom")
        assert ei.value.args[0] == "boom"
        assert tr.rollback_called == 1 and tr.commit_called == 0 and tr.close_called == 1
        assert ei.value.__cause__ is None  # 无清理异常, 不建链
        assert _row_count(str(db.db_path), "readings") == 0
        assert db._tx is False


class TestCommitFailure:
    def test_commit_exception_propagates_and_rollback_attempted(self, db, tmp_path, monkeypatch):
        tr = _tracked(db)
        monkeypatch.setattr(db, "_connect", lambda: tr)
        tr.fail_commit = True
        with pytest.raises(sqlite3.OperationalError) as ei:
            with db.with_transaction() as conn:
                conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r1','s1',1.0,'2026-10-26T10:00:00+08:00')")
        assert "commit failed" in str(ei.value)
        assert tr.rollback_called == 1 and tr.close_called == 1
        assert db._tx is False

    def test_commit_and_rollback_both_fail(self, db, tmp_path, monkeypatch):
        tr = _tracked(db)
        monkeypatch.setattr(db, "_connect", lambda: tr)
        tr.fail_commit = True
        tr.fail_rollback = True
        with pytest.raises(sqlite3.OperationalError) as ei:
            with db.with_transaction() as conn:
                conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r1','s1',1.0,'2026-10-26T10:00:00+08:00')")
        assert "commit failed" in str(ei.value)
        # 主异常=commit; rollback 异常经异常链保留
        assert isinstance(ei.value.__cause__, sqlite3.OperationalError)
        assert "rollback failed" in str(ei.value.__cause__)
        assert tr.rollback_called == 1 and tr.close_called == 1
        assert db._tx is False


class TestClose:
    def test_business_and_close_both_fail(self, db, tmp_path, monkeypatch):
        tr = _tracked(db)
        monkeypatch.setattr(db, "_connect", lambda: tr)
        tr.fail_close = True
        with pytest.raises(ValueError) as ei:
            with db.with_transaction() as conn:
                conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r1','s1',1.0,'2026-10-26T10:00:00+08:00')")
                raise ValueError("biz")
        assert ei.value.__cause__ is not None
        assert "close failed" in str(ei.value.__cause__)
        assert tr.close_called == 1 and tr.rollback_called == 1
        assert db._tx is False

    def test_normal_commit_then_close_failure_propagates_close(self, db, tmp_path, monkeypatch):
        tr = _tracked(db)
        monkeypatch.setattr(db, "_connect", lambda: tr)
        tr.fail_close = True
        with pytest.raises(sqlite3.OperationalError) as ei:
            with db.with_transaction() as conn:
                conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r1','s1',1.0,'2026-10-26T10:00:00+08:00')")
        assert "close failed" in str(ei.value)
        assert ei.value.__cause__ is None  # 无清理链, 仅传播关闭异常
        assert tr.commit_called == 1 and tr.close_called == 1 and tr.rollback_called == 0
        assert db._tx is False


class TestMultipleCleanupAndReset:
    def test_business_rollback_close_all_fail_still_continues_and_resets(self, db, tmp_path, monkeypatch):
        tr = _tracked(db)
        monkeypatch.setattr(db, "_connect", lambda: tr)
        tr.fail_rollback = True
        tr.fail_close = True
        with pytest.raises(ValueError) as ei:
            with db.with_transaction() as conn:
                conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r1','s1',1.0,'2026-10-26T10:00:00+08:00')")
                raise ValueError("biz")
        # 主异常=业务; rollback 异常入链(先发生); close 已尝试(close_called)即使它也失败
        assert "biz" in str(ei.value)
        assert isinstance(ei.value.__cause__, sqlite3.OperationalError)
        assert "rollback failed" in str(ei.value.__cause__)
        assert tr.rollback_called == 1 and tr.close_called == 1
        assert db._tx is False

    def test_tx_reset_all_paths(self, db, tmp_path, monkeypatch):
        # 正常
        tr = _tracked(db); monkeypatch.setattr(db, "_connect", lambda: tr)
        with db.with_transaction() as conn:
            conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r1','s1',1.0,'2026-10-26T10:00:00+08:00')")
        assert db._tx is False
        # 业务异常
        with pytest.raises(ValueError):
            with db.with_transaction() as conn:
                raise ValueError("x")
        assert db._tx is False


class TestNested:
    def test_nested_rejected_and_outer_continues(self, db, tmp_path, monkeypatch):
        tr = _tracked(db)
        monkeypatch.setattr(db, "_connect", lambda: tr)
        with db.with_transaction() as conn:
            conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r1','s1',1.0,'2026-10-26T10:00:00+08:00')")
            with pytest.raises(RuntimeError):
                with db.with_transaction():
                    pass  # 内层拒绝
            # 内层拒绝后, 外层仍可继续写
            conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r2','s1',2.0,'2026-10-26T10:00:00+08:00')")
        assert tr.commit_called == 1 and tr.close_called == 1
        assert _row_count(str(db.db_path), "readings") == 2
        assert db._tx is False


class TestSingleConnection:
    def test_only_one_connection_opened(self, db, monkeypatch):
        calls = []
        tr = _tracked(db)

        def fake_connect():
            calls.append(1)
            return tr

        monkeypatch.setattr(db, "_connect", fake_connect)
        with db.with_transaction() as conn:
            conn.execute("INSERT INTO readings (reading_id, sensor_id, temperature, device_captured_at) VALUES ('r1','s1',1.0,'2026-10-26T10:00:00+08:00')")
        assert len(calls) == 1  # 仅开连接那一次
        assert db._tx is False