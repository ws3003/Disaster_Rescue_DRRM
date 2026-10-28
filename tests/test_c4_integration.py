"""V4.4 C4 生产接入 (A1) 集成测试: bridge 单事务分流 + 事务/幂等/冲突/回滚 + 调用点兼容。

覆盖(授权 §六 1-15), 隔离临时 SQLite, 不碰真实库/硬件/云。
说明:
- 业务精度仍未注册; 非整 float 在 precision 未确认时 -> c4_verify(不隐式舍入, 不入 Outbox)。
- 用 Decimal/整数在显式版本下走"可哈希 -> Outbox"分支验证。
- alarm(sensor_alert 调用点)暂未接线(阻断项, 见报告), 其 bridge 能力用直接调用验证。
"""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal

import pytest

from src.database.db import Database
from src.sync import bridge as c4_bridge


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    db = Database(db_path=os.path.join(tmp.name, "c4_integration_test.db"))
    yield db


_EVENT_OK = {"type": "memory_alert", "severity": "HIGH", "person_id": 1,
             "payload": {"state": "Fall", "risk_score": Decimal("0.5"), "priority": "HIGH",
                         "timestamp": "2026-10-28T10:00:00Z"}}
_READING_OK = {"sensor_id": "s1", "temperature": Decimal("30.5"), "humidity": Decimal("40"),
               "device_captured_at": "2026-10-28T10:00:00Z"}
_SALERT_OK = {"type": "sensor_hightemp", "temperature": Decimal("52"), "threshold": Decimal("45"),
              "occurred_at": "2026-10-28T10:00:00Z", "device_captured_at": "2026-10-28T10:00:00Z"}


# ---- 1-3: 正常业务写入 + Outbox 同事务提交 ----
class TestQueuedPath:
    def test_event_queued(self, db):
        r = c4_bridge.create_sync_record(db, record_type="person_event", business=_EVENT_OK)
        assert r["status"] == "queued"
        assert db.get_event(r["source_record_id"]) is not None
        ob = db.list_outbox()
        assert len(ob) == 1 and ob[0]["record_type"] == "person_event"

    def test_reading_queued(self, db):
        r = c4_bridge.create_sync_record(db, record_type="reading", business=_READING_OK)
        assert r["status"] == "queued"
        assert db.get_reading(r["source_record_id"]) is not None
        assert len(db.list_outbox()) == 1

    def test_sensor_alert_queued(self, db):
        # alarm 调用点未接线; 直接经 bridge 验证 sensor_alert 能力
        r = c4_bridge.create_sync_record(db, record_type="sensor_alert", business=_SALERT_OK)
        assert r["status"] == "queued"
        assert db.get_sensor_alert(r["source_record_id"]) is not None
        assert len(db.list_outbox()) == 1


# ---- 4-5: 规范化失败 -> 业务+verify 同事务, 不建 Outbox ----
class TestVerifyPath:
    def test_non_integral_float_goes_verify_not_outbox(self, db):
        biz = {"type": "memory_alert", "severity": "HIGH", "person_id": 1,
               "payload": {"state": "Fall", "risk_score": 0.92, "priority": "HIGH",
                           "timestamp": "2026-10-28T10:00:00Z"}}  # risk 为 float
        r = c4_bridge.create_sync_record(db, record_type="person_event", business=biz)
        assert r["status"] == "verified"
        assert "unparsable" in r["reasons"]
        # 业务事件已持久化 (权威源), 但无 Outbox (不隐式舍入)
        assert db.get_event(r["source_record_id"]) is not None
        assert db.list_outbox() == []
        assert db.get_verify("person_event", r["source_record_id"]) is not None

    def test_verify_never_in_outbox(self, db):
        biz = {"type": "memory_alert", "severity": "HIGH", "person_id": 2,
               "payload": {"state": "Fall", "risk_score": 0.92, "priority": "HIGH"}}  # 无 timestamp + float
        r = c4_bridge.create_sync_record(db, record_type="person_event", business=biz)
        assert r["status"] == "verified" and "missing_required" in r["reasons"]
        assert db.list_outbox() == []


# ---- 6-8,15: 回滚(无半成品) ----
class TestAtomicRollback:
    def test_business_write_failure_rolls_back(self, db, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("core failed")

        monkeypatch.setattr(c4_bridge, "_write_core", boom)
        with pytest.raises(RuntimeError):
            c4_bridge.create_sync_record(db, record_type="person_event", business=_EVENT_OK)
        assert db.list_outbox() == []
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM c4_verify").fetchone()[0] == 0

    def test_outbox_write_failure_rolls_back_business(self, db, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("outbox fail")

        monkeypatch.setattr(db, "_enqueue_sync_in_tx", boom)
        with pytest.raises(RuntimeError):
            c4_bridge.create_sync_record(db, record_type="person_event", business=_EVENT_OK)
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM c4_idem").fetchone()[0] == 0

    def test_verify_write_failure_rolls_back_business(self, db, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("verify fail")

        monkeypatch.setattr(db, "_add_verify_in_tx", boom)
        biz_float = {"type": "t", "severity": "H", "person_id": 1,
                     "payload": {"state": "F", "risk_score": 0.9, "priority": "H",
                                 "timestamp": "2026-10-28T10:00:00Z"}}
        with pytest.raises(RuntimeError):
            c4_bridge.create_sync_record(db, record_type="person_event", business=biz_float)
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM c4_verify").fetchone()[0] == 0


# ---- 9-11: 幂等/冲突 ----
class TestIdempotencyConflict:
    def test_re_enqueue_same_key_hash_no_dup(self, db):
        r = c4_bridge.create_sync_record(db, record_type="person_event", business=_EVENT_OK)
        sid = r["source_record_id"]
        from src.database import canonical
        h, _ = canonical.compute_content_hash("person_event",
                                              {"payload": _EVENT_OK["payload"],
                                               "person_id": _EVENT_OK["person_id"]},
                                              version=c4_bridge.SYNC_NORM_VERSION)
        out, _ = db.enqueue_sync("person_event", sid, h)  # 同键同 hash 重入
        assert out == "already_synced"
        assert len(db.list_outbox()) == 1  # 不重复入队

    def test_content_conflict_preserved(self, db):
        r = c4_bridge.create_sync_record(db, record_type="person_event", business=_EVENT_OK)
        sid = r["source_record_id"]
        out, _ = db.enqueue_sync("person_event", sid, "DIFFERENT-HASH")
        assert out == "content_mismatch"
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM c4_content_conflict").fetchone()[0] == 1


# ---- 12: 精度未确认不隐式舍入(as above in VerifyPath) ----
# (已由 test_non_integral_float_goes_verify_not_outbox 覆盖)


# ---- 13: 公开 API 兼容 ----
class TestPublicCompat:
    def test_public_save_and_c4_contracts(self, db):
        eid = db.save_event(type="memory_alert", severity="HIGH", person_id=5,
                            payload={"state": "F", "risk_score": 0.9})
        assert db.get_event(eid) is not None
        rid = db.save_reading(sensor_id="s", temperature=30.0)
        assert db.get_reading(rid) is not None
        aid = db.save_sensor_alert(type="sensor_hightemp", temperature=52.0, threshold=45.0)
        assert db.get_sensor_alert(aid) is not None
        # enqueue_sync / add_verify 返回语义
        res = db.enqueue_sync("person_event", "E-C-1", "h1")
        assert isinstance(res, tuple) and res[0] in ("queued",)
        assert db.add_verify("person_event", "E-C-1", {"unparsable"}) is True