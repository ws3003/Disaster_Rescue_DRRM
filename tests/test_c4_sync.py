"""V4.4 C4-01 阶段 1 测试: Outbox / 服务端幂等 / 冲突审计 / 共享写入 / fencing。

覆盖:
- 首次业务写 + 幂等登记 + Outbox 入队 同事务原子提交。
- 业务写失败整笔回滚。
- 同键同内容 -> already_synced; 同键异内容 -> content_mismatch + 冲突审计保留首次。
- 唯一约束冲突后的内容核验。
- Outbox 原子领取(fencing gen +1)、旧 gen 回写/续租拒绝、租约过期回收、NULL 规则。
- 既有 save_* 语义经 _core 委托保持 (回归由 test_e9_* 保证; 此处另验事件写)。

隔离策略: 全部 tempfile 临时 SQLite, 不触碰真实库。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.database.db import Database


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    db = Database(db_path=os.path.join(tmp.name, "c4_sync_test.db"))
    yield db


def _enqueue(db, key="E-1000-00000001", h="hash-a", business=None, **kw):
    return db.enqueue_sync("sensor_alert", key, h, business=business, **kw)


_SALERT = {"type": "sensor_hightemp", "temperature": 52.0, "threshold": 45.0}


class TestEnqueueFirstSync:
    def test_first_sync_atomic_business_idem_outbox(self, db):
        res, sid = _enqueue(db, business=_SALERT)
        assert res == "queued" and sid == "E-1000-00000001"
        # 业务记录 (sensor_alerts) 与幂等登记与 outbox 均落库
        assert db.get_sensor_alert("E-1000-00000001") is not None
        got = db.get_outbox(1)
        assert got is not None and got["status"] == "pending" and got["gen"] == 0
        assert got["record_type"] == "sensor_alert" and got["source_record_id"] == "E-1000-00000001"

    def test_enqueue_without_business_registers_idem_and_outbox(self, db):
        db.save_sensor_alert(type="sensor_hightemp", temperature=52.0, threshold=45.0)
        res, sid = _enqueue(db, business=None)
        assert res == "queued"
        assert db.get_outbox(1)["source_record_id"] == "E-1000-00000001"

    def test_business_write_failure_rolls_back_everything(self, db, monkeypatch):
        # 业务写失败 -> 整事务回滚, 不留幂等/outbox 半成品
        def boom(*a, **k):
            raise RuntimeError("business insert failed")

        monkeypatch.setattr(db, "_write_business_in_tx", boom)
        with pytest.raises(RuntimeError):
            _enqueue(db, business=_SALERT)
        assert db.list_outbox() == []
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM c4_idem").fetchone()[0] == 0

    def test_invalid_record_type_rejected(self, db):
        with pytest.raises(ValueError):
            db.enqueue_sync("bogus", "x", "h")


class TestIdempotencyConflict:
    def test_same_key_same_content_already(self, db):
        _enqueue(db)
        res, _ = _enqueue(db)  # 重放同内容
        assert res == "already_synced"
        rows = db.list_outbox()
        assert len(rows) == 1  # 不新增入队

    def test_same_key_diff_content_conflict_keeps_first(self, db):
        _enqueue(db, h="hash-a", business=_SALERT)
        res, _ = db.enqueue_sync("sensor_alert", "E-1000-00000001", "hash-b", business=_SALERT)
        assert res == "content_mismatch"
        # 首次业务与幂等登记保留原内容
        alert = db.get_sensor_alert("E-1000-00000001")
        assert alert is not None and alert["type"] == "sensor_hightemp"
        with db._connect() as conn:
            h = conn.execute(
                "SELECT content_hash FROM c4_idem WHERE record_type='sensor_alert' AND source_record_id='E-1000-00000001'"
            ).fetchone()[0]
            assert h == "hash-a"  # 首收哈希不动
            n = conn.execute("SELECT COUNT(*) FROM c4_content_conflict").fetchone()[0]
            assert n == 1

    def test_concurrent_dispatcher_unhandled_not_mistaken_as_already(self, db):
        # 唯一冲突后必须核验内容: 手工预置登记, 确保不是"所有冲突都视为 already"
        with db.with_transaction() as tx:
            tx.execute(
                """
                INSERT INTO c4_idem (record_type, source_record_id, content_hash, received_at)
                VALUES ('sensor_alert', 'E-KEY', 'existing-hash', 1)
                """
            )
        # 同内容 -> already
        assert db.enqueue_sync("sensor_alert", "E-KEY", "existing-hash")[0] == "already_synced"
        # 异内容 -> content_mismatch(核验后判异, 非一律成功)
        res, _ = db.enqueue_sync("sensor_alert", "E-KEY", "other-hash")
        assert res == "content_mismatch"
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM c4_content_conflict").fetchone()[0] == 1


class TestOutboxFencing:
    def _seed_pending(self, db, keys=("K1",)):
        for k in keys:
            db.enqueue_sync("sensor_alert", k, f"h-{k}")

    def test_claim_increments_gen_and_marks_processing(self, db):
        self._seed_pending(db, ("K1",))
        oid = db.outbox_claim("w1")
        assert oid is not None
        got = db.get_outbox(oid)
        assert got["status"] == "processing" and got["gen"] == 1 and got["worker_id"] == "w1"

    def test_claim_then_no_second_same_row(self, db):
        self._seed_pending(db, ("K1",))
        oid1 = db.outbox_claim("w1")
        # 同一行已被 w1 领取 -> 当前 processing 未过期, 不应再被领取
        assert db.outbox_claim("w2") is None
        assert db.get_outbox(oid1)["worker_id"] == "w1"

    def test_old_gen_mark_rejected(self, db):
        self._seed_pending(db, ("K1",))
        oid = db.outbox_claim("w1")
        got = db.get_outbox(oid)
        assert got["gen"] == 1
        # 用错误 gen 回写 -> 拒绝 (rowcount 0)
        assert db.outbox_mark_success(oid, "w1", got["gen"] - 1) is False
        assert db.get_outbox(oid)["status"] == "processing"
        # 正确 gen -> 成功 (终态)
        assert db.outbox_mark_success(oid, "w1", got["gen"]) is True
        assert db.get_outbox(oid)["status"] == "success"
        # success 回退被拒
        assert db.outbox_requeue(oid) is False

    def test_old_gen_renew_rejected(self, db):
        self._seed_pending(db, ("K1",))
        oid = db.outbox_claim("w1")
        got = db.get_outbox(oid)
        assert db.outbox_renew(oid, "w1", got["gen"] - 1) is False
        assert db.outbox_renew(oid, "w1", got["gen"]) is True

    def test_lease_expired_reclaim_and_old_gen_invalid(self, db):
        # 预置一条 processing 且租约已过期
        with db.with_transaction() as tx:
            tx.execute(
                """
                INSERT INTO sync_outbox (record_type, source_record_id, status, gen, worker_id, lease_until, created_at)
                VALUES ('sensor_alert', 'EXPIRED', 'processing', 3, 'wA', 1, 1)
                """
            )
        oid = db.outbox_claim("wB", now_ms=10_000)
        assert oid is not None
        got = db.get_outbox(oid)
        assert got["gen"] == 4  # 重新领取 gen+1, 旧 worker token 失效
        # 旧 worker (gen 3) 的回写被拒
        assert db.outbox_mark_success(oid, "wA", 3) is False
        assert db.outbox_mark_success(oid, "wB", 4) is True

    def test_processing_lease_null_not_reclaimable(self, db):
        with db.with_transaction() as tx:
            tx.execute(
                """
                INSERT INTO sync_outbox (record_type, source_record_id, status, gen, worker_id, lease_until, created_at)
                VALUES ('sensor_alert', 'NONULL', 'processing', 1, 'wA', NULL, 1)
                """
            )
        # lease NULL 属异常, 不得当作过期租约回收
        assert db.outbox_claim("wB", now_ms=99_000) is None

    def test_retry_wait_null_next_not_retried(self, db):
        with db.with_transaction() as tx:
            tx.execute(
                """
                INSERT INTO sync_outbox (record_type, source_record_id, status, retry_count, next_attempt_at, created_at)
                VALUES ('sensor_alert', 'RWNULL', 'retry_wait', 1, NULL, 1)
                """
            )
        # retry_wait 且 next_attempt_at NULL = 异常, 不静默立即重试
        assert db.outbox_claim("w1", now_ms=50_000) is None

    def test_fail_retry_and_dead_letter(self, db):
        self._seed_pending(db, ("K1",))
        oid = db.outbox_claim("w1")
        got = db.get_outbox(oid)
        # 可重试失败 -> retry_wait, retry_count+1
        assert db.outbox_mark_retry_wait(oid, "w1", got["gen"], next_attempt_at=99_000, last_error="net") is True
        got2 = db.get_outbox(oid)
        assert got2["status"] == "retry_wait" and got2["retry_count"] == 1
        # 领取回 retry 到期(不合规不在此测) -> 直接转永久 dead_letter: 先再领取需满足到期, 改用手工置 processing
        with db._connect() as conn:
            conn.execute(
                "UPDATE sync_outbox SET status='processing', lease_until=99999999999999 WHERE id=?",
                (oid,),
            )
        got3 = db.get_outbox(oid)
        # recclaim gen changed to 0; 直接用当前 gen 标记死信
        assert db.outbox_mark_dead_letter(oid, "w1", got3["gen"], last_error="perm") is True
        assert db.get_outbox(oid)["status"] == "dead_letter"
    # 人工重入: 保留失败历史
    def test_requeue_preserves_history(self, db):
        with db.with_transaction() as tx:
            tx.execute(
                """
                INSERT INTO sync_outbox (record_type, source_record_id, status, retry_count, last_error, created_at)
                VALUES ('sensor_alert', 'DL', 'dead_letter', 5, 'orig-error', 1)
                """
            )
        assert db.outbox_requeue(1, reason="fix") is True
        got = db.get_outbox(1)
        assert got["status"] == "pending" and got["retry_count"] == 0 and got["next_attempt_at"] is None
        assert "orig-error" in got["last_error"] and "requeued@" in got["last_error"]


class TestSaveEventStillWorks:
    def test_save_event_via_core_delegation(self, db):
        eid = db.save_event(type="memory_alert", severity="HIGH", person_id=1,
                            payload={"state": "Fall", "risk_score": 0.92})
        got = db.get_event(eid)
        assert got["type"] == "memory_alert" and got["severity"] == "HIGH"
        assert got["payload"]["risk_score"] == 0.92


class TestIsolation:
    def test_uses_temp_database(self, db):
        assert os.path.basename(str(db.db_path)) == "c4_sync_test.db"
        assert str(db.db_path) != "data/drrm.db"