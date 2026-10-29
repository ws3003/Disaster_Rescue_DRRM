"""V4.4 E9-03 媒体取证 DAO 测试。

隔离策略: 全部使用 tempfile 临时 SQLite, 不触碰 data/drrm.db / src/data/drrm.db。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from src.database.db import Database

CAPTURED = "2026-10-24T12:00:00+08:00"
CREATED = "2026-10-24T12:01:00+08:00"


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    db = Database(db_path=os.path.join(tmp.name, "e9_media_test.db"))
    db.save_sensor_alert(
        type="sensor_over_temp",
        temperature=52.0,
        humidity=30.0,
        threshold=45.0,
        device_captured_at=CAPTURED,
    )
    yield db


def _alert_id(db: Database) -> str:
    return db.query_sensor_alerts(limit=1)[0]["alert_id"]


def _save(db, **over):
    kw = dict(
        source="sensor",
        ref_id=_alert_id(db),
        media_type="video",
        local_path="/media/a.mp4",
        captured_at=CAPTURED,
        created_at=CREATED,
    )
    kw.update(over)
    return db.save_media_evidence(**kw), kw


class TestSaveGet:
    def test_valid_save_and_get(self, db):
        mid, kw = _save(db)
        got = db.get_media_evidence(mid)
        assert got is not None and got["media_id"] == mid
        assert got["source"] == "sensor" and got["media_type"] == "video"
        assert got["ref_id"] == kw["ref_id"] and got["local_path"] == kw["local_path"]
        assert got["status"] == "pending" and got["retry_count"] == 0
        assert got["captured_at"] == CAPTURED and got["created_at"] == CREATED
        assert got["uploaded_at"] is None

    def test_invalid_enum_dao_rejected(self, db):
        with pytest.raises(ValueError):
            _save(db, source="foobar")
        with pytest.raises(ValueError):
            _save(db, media_type="audio")
        with pytest.raises(ValueError):
            _save(db, status="resolved")
        with pytest.raises(ValueError):
            _save(db, capture_result="meh")
        with pytest.raises(ValueError):
            _save(db, upload_status="unknown")
        with pytest.raises(ValueError):
            _save(db, retry_count=-1)

    def test_ref_not_exist_rejected(self, db):
        with pytest.raises(ValueError):
            _save(db, ref_id="E-20261024-FFFFFFFF")


class TestList:
    def test_list_by_ref(self, db):
        _save(db)
        _save(db, local_path="/media/b.mp4")
        items = db.list_media_evidence_by_ref("sensor", _alert_id(db))
        assert len(items) == 2
        assert [i["media_id"] for i in items] == sorted(
            i["media_id"] for i in items
        )

    def test_list_invalid_source(self, db):
        with pytest.raises(ValueError):
            db.list_media_evidence_by_ref("person", "x")


class TestIdempotencyConflict:
    def test_idempotent_same_key(self, db):
        mid, kw = _save(db, media_id="M-20261024-AAAA0001")
        again = db.save_media_evidence(
            source="sensor", ref_id=kw["ref_id"], media_type="video",
            local_path=kw["local_path"], captured_at=CAPTURED, created_at=CREATED,
            media_id=mid,
        )
        assert again == mid
        # 幂等不应覆盖状态: 先把状态推离 pending 再重放
        db.update_media_evidence_status(mid, status="processing")
        again2 = db.save_media_evidence(
            source="sensor", ref_id=kw["ref_id"], media_type="video",
            local_path=kw["local_path"], captured_at=CAPTURED, created_at=CREATED,
            media_id=mid,
        )
        assert again2 == mid
        assert db.get_media_evidence(mid)["status"] == "processing"

    def test_conflict_same_id_different_key(self, db):
        _save(db, media_id="M-20261024-BBBB0001")
        # 同 id、不同 local_path(关键字段) -> 冲突, 不覆盖
        with pytest.raises(ValueError):
            db.save_media_evidence(
                source="sensor", ref_id=_alert_id(db), media_type="video",
                local_path="/media/other.mp4", captured_at=CAPTURED, created_at=CREATED,
                media_id="M-20261024-BBBB0001",
            )
        got = db.get_media_evidence("M-20261024-BBBB0001")
        assert got["local_path"] == "/media/a.mp4"


class TestStatus:
    def test_valid_transitions(self, db):
        mid, _ = _save(db)
        assert db.update_media_evidence_status(mid, status="processing") is True
        assert db.update_media_evidence_status(mid, status="success") is True
        assert db.get_media_evidence(mid)["status"] == "success"

    def test_failed_then_processing(self, db):
        mid, _ = _save(db)
        db.update_media_evidence_status(mid, status="processing")
        assert db.update_media_evidence_status(mid, status="failed-awaiting-retry") is True
        assert db.update_media_evidence_status(mid, status="processing") is True
        assert db.get_media_evidence(mid)["status"] == "processing"

    def test_invalid_transitions_rejected(self, db):
        mid, _ = _save(db)
        # pending -> success 直跳
        assert db.update_media_evidence_status(mid, status="success") is False
        # pending -> failed-awaiting-retry 直跳
        assert db.update_media_evidence_status(mid, status="failed-awaiting-retry") is False
        # success 不可回退
        db.update_media_evidence_status(mid, status="processing")
        db.update_media_evidence_status(mid, status="success")
        assert db.update_media_evidence_status(mid, status="processing") is False
        assert db.update_media_evidence_status(mid, status="failed-awaiting-retry") is False
        assert db.get_media_evidence(mid)["status"] == "success"

    def test_nonexistent_media(self, db):
        assert db.get_media_evidence("M-00010100-00000000") is None
        assert db.update_media_evidence_status("M-00010100-00000000", status="processing") is False

    def test_aux_update_atomic_on_invalid_value(self, db):
        mid, _ = _save(db)
        db.update_media_evidence_status(mid, status="processing")
        # 非法 uploaded_at -> ValueError, 记录不得改变
        with pytest.raises(ValueError):
            db.update_media_evidence_status(mid, status="success", uploaded_at="not-a-date")
        got = db.get_media_evidence(mid)
        assert got["status"] == "processing" and got["uploaded_at"] is None
        # 合法辅助字段
        assert db.update_media_evidence_status(
            mid, status="success", uploaded_at=CREATED, object_key="obj/1"
        ) is True
        got = db.get_media_evidence(mid)
        assert got["object_key"] == "obj/1" and got["uploaded_at"] == CREATED

    def test_aux_failure_leaves_status_unchanged(self, db):
        mid, _ = _save(db)
        db.update_media_evidence_status(mid, status="processing")
        with pytest.raises(ValueError):
            db.update_media_evidence_status(mid, status="success", capture_result="bogus")
        assert db.get_media_evidence(mid)["status"] == "processing"


class TestTimeAndPath:
    def test_no_tz_rejected(self, db):
        with pytest.raises(ValueError):
            _save(db, captured_at="2026-10-24T12:00:00")

    def test_bad_date_rejected(self, db):
        with pytest.raises(ValueError):
            _save(db, captured_at="2026-02-30T12:00:00+08:00")

    def test_empty_path_rejected(self, db):
        with pytest.raises(ValueError):
            _save(db, local_path="")

    def test_iso_z_accepted(self, db):
        _save(db, captured_at="2026-10-24T12:00:00Z")


class TestIsolation:
    def test_uses_temp_database(self, db):
        # 隔离: 数据库连接必须是临时文件名, 而非默认 data/drrm.db
        assert os.path.basename(str(db.db_path)) == "e9_media_test.db"
        assert str(db.db_path) != "data/drrm.db"

    def test_check_constraint_via_raw_sql(self, db):
        # 直接 SQL 写入非法枚举应被 CHECK 拒绝
        mid, _ = _save(db)
        with db._connect() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO media_evidence (
                        media_id, source, ref_id, media_type, local_path, status,
                        captured_at, created_at
                    ) VALUES (?, 'bad', ?, 'video', '/x', 'pending', ?, ?)
                    """,
                    ("M-CK-0001", _alert_id(db), CREATED, CREATED),
                )