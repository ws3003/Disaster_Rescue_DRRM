"""Task3+Task4 阶段1：C4 media_evidence 接收/生命周期/幂等/回退 专项测试。

隔离：tempfile 临时 SQLite（服务端库）；直调 server/c4._ingest；不触真实库/OSS/Pi。
覆盖：首收、NULL 初始、upload 缺失/显式NULL、状态机转换、success 终态保护、
object_key/uploaded_at 缺失保留、idem 幂等与"非法不推进 hash"、media_id/ref_id/枚举校验。
"""
from __future__ import annotations

import os
import tempfile

import pytest

from server import c4
from src.database.db import Database


@pytest.fixture()
def db():
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "c4_media_test.db"))
    yield d
    try:
        tmp.cleanup()
    except Exception:  # noqa: BLE001
        pass


def _mk(media_id="M1", status="pending", upload=None, ok=None, ref="a1",
        mtype="video", source="sensor", upload_null=False, **kw) -> dict:
    base = {
        "media_id": media_id, "ref_id": ref, "source": source,
        "media_type": mtype, "status": status,
        "captured_at": "2026-10-24T12:00:00+08:00",
    }
    if upload is not None:
        base["upload_status"] = upload
    elif upload_null:
        base["upload_status"] = None          # 显式 NULL（区别于字段缺失）
    if ok is not None:
        base["object_key"] = ok
    base.update(kw)
    h, _ = c4._hash_of_business("media_evidence", base)
    return {"record_type": "media_evidence", "source_record_id": media_id,
            "content_hash": h, "edge_id": "e1", "business": base}


def _ingest_body(db, body):
    c4._validate_record(body)          # 与 receive 同前置校验
    return c4._ingest(db, body)


def _row(db, mid):
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM media_evidence WHERE media_id=?", (mid,)).fetchone()
        return dict(r) if r else None


def _idem(db, mid):
    with db._connect() as conn:
        r = conn.execute(
            "SELECT content_hash FROM c4_idem WHERE record_type='media_evidence' AND source_record_id=?",
            (mid,)).fetchone()
        return r[0] if r else None


# ---- 首收 / NULL 初值 / 可播放 ----
def test_first_pending_null(db):
    r = _ingest_body(db, _mk())
    assert r == "synced"
    row = _row(db, "M1")
    assert row["status"] == "pending" and row["upload_status"] is None
    assert row["ref_id"] == "a1" and row["local_path"] == "" and row["source"] == "sensor"
    assert _idem(db, "M1") is not None

def test_first_missing_upload_means_NOT_pending(db):
    # 缺 upload_status 首收 → NULL（未上传），不当成 pending
    r = _ingest_body(db, _mk(ok=None, upload=None))
    assert _row(db, "M1")["upload_status"] is None

def test_first_success_playable_ok(db):
    r = _ingest_body(db, _mk(status="success", upload="success", ok="evidence/M1.mp4"))
    assert r == "synced"
    assert _row(db, "M1")["status"] == "success" and _row(db, "M1")["object_key"] == "evidence/M1.mp4"

def test_first_success_not_playable_rejected(db):
    # status=success 但 upload 未 success / 缺 object_key → 400
    with pytest.raises(Exception):
        _ingest_body(db, _mk(status="success", upload=None, ok=None))
    with pytest.raises(Exception):
        _ingest_body(db, _mk(status="success", upload="pending", ok="k"))

# ---- 转换 ----
def test_pending_to_processing_to_success(db):
    _ingest_body(db, _mk(upload=None))                        # pending / upload NULL
    _ingest_body(db, _mk(status="processing", upload="uploading"))
    r = _ingest_body(db, _mk(status="success", upload="success", ok="evidence/M1.mp4"))
    assert r == "synced"
    row = _row(db, "M1")
    assert row["status"] == "success" and row["upload_status"] == "success"

def test_keep_and_retry_success(db):
    _ingest_body(db, _mk(upload="failed", status="failed-awaiting-retry"))
    r = _ingest_body(db, _mk(status="success", upload="success", ok="evidence/M1.mp4"))
    assert r == "synced"
    row = _row(db, "M1")
    assert row["status"] == "success" and row["upload_status"] == "success"

# ---- success 终态保护 ----
def test_success_not_reverted_by_old_pending(db):
    _ingest_body(db, _mk(status="success", upload="success", ok="k1"))
    r = _ingest_body(db, _mk(status="pending", upload=None, ok=None))
    assert r == "content_mismatch"
    row = _row(db, "M1")
    assert row["status"] == "success" and row["object_key"] == "k1"
    assert _idem(db, "M1")                         # idem 仍为该消息前 hash

def test_success_not_overwritten_by_upload_null_or_pending(db):
    _ingest_body(db, _mk(status="success", upload="success", ok="k1"))
    assert _ingest_body(db, _mk(status="success", upload="pending", ok=None)) == "content_mismatch"
    assert _ingest_body(db, _mk(status="success", upload="failed", ok=None)) == "content_mismatch"
    row = _row(db, "M1")
    assert row["upload_status"] == "success" and row["object_key"] == "k1"

# ---- 缺失保留 vs 显式 NULL ----
def test_missing_object_key_preserved(db):
    _ingest_body(db, _mk(status="success", upload="success", ok="k1"))
    # 新消息 status=success 但缺 object_key（upload 保持 success）→ object_key 不得被清空
    r = _ingest_body(db, _mk(status="success", upload="success"))
    assert r == "synced"
    row = _row(db, "M1")
    assert row["object_key"] == "k1"          # 缺失 → 保留原值
    assert row["status"] == "success" and row["upload_status"] == "success"

def test_explicit_null_upload_after_pending_ok(db):
    _ingest_body(db, _mk(upload=None))                    # NULL 初始
    r = _ingest_body(db, _mk(status="pending", upload_null=True, ok=None))
    assert r == "synced"                                   # 显式 NULL（仍 pending）合法
    assert _row(db, "M1")["upload_status"] is None

def test_explicit_null_upload_after_success_rejected(db):
    _ingest_body(db, _mk(status="success", upload="success", ok="k1"))
    assert _ingest_body(db, _mk(status="success", upload_null=True, ok=None)) == "content_mismatch"

# ---- idem / 重复 / 不推进 hash ----
def test_duplicate_same_hash(db):
    _ingest_body(db, _mk())
    r = _ingest_body(db, _mk())
    assert r == "already_synced"
    assert _idem(db, "M1") is not None

def test_illegal_update_does_not_advance_idem_hash(db):
    _ingest_body(db, _mk(status="success", upload="success", ok="k1"))
    h0 = _idem(db, "M1")
    _ingest_body(db, _mk(status="pending", upload=None, ok=None))  # 旧 pending
    assert _idem(db, "M1") == h0                                  # hash 未推进

# ---- 校验：media_id 一致 / ref_id 不可变 / 枚举 / source ----
def test_media_id_mismatch_400(db):
    body = _mk(); body["business"]["media_id"] = "M-other"
    with pytest.raises(Exception):
        _ingest_body(db, body)

def test_source_must_be_supported(db):
    with pytest.raises(Exception):
        _ingest_body(db, _mk(source="unsupported"))


def test_person_source_is_preserved(db):
    assert _ingest_body(db, _mk(source="person")) == "synced"
    assert _row(db, "M1")["source"] == "person"

def test_bad_media_type(db):
    with pytest.raises(Exception):
        _ingest_body(db, _mk(mtype="audio"))

def test_ref_change_after_insert_rejected(db):
    _ingest_body(db, _mk(ref="a1"))
    assert _ingest_body(db, _mk(status="processing", upload="uploading", ref="a2")) == "content_mismatch"
    assert _row(db, "M1")["ref_id"] == "a1"

def test_missing_required_400(db):
    body = _mk(); body["business"] = {k: v for k, v in body["business"].items() if k != "ref_id"}
    body["content_hash"] = c4._hash_of_business("media_evidence", body["business"])[0]
    with pytest.raises(Exception):
        _ingest_body(db, body)

# ---- idem 已存在但 media 缺失：不自动补建 ----
def test_idem_exists_no_media_no_autocreate(db):
    # 直接插 idem（模拟旧客户端仅登记）
    with db.with_transaction() as tx:
        tx.execute(
            "INSERT INTO c4_idem (record_type, source_record_id, content_hash, received_at, edge_id) "
            "VALUES ('media_evidence', 'Mx', 'h0', 0, 'e')")
    # media 行缺失时，生命周期更新到达 → content_mismatch，不 INSERT media
    r = _ingest_body(db, _mk(media_id="Mx", status="success", upload="success", ok="k"))
    assert r == "content_mismatch"
    assert _row(db, "Mx") is None

# ---- 并发/事务（顺序交错） ----
def test_sequential_duplicate_keeps_one_row(db):
    _ingest_body(db, _mk(ok=None))
    _ingest_body(db, _mk(status="success", upload="success", ok="k1"))
    r = _ingest_body(db, _mk(status="success", upload="success", ok="k1"))
    assert r == "already_synced"
    with db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM media_evidence WHERE media_id='M1'").fetchone()[0]
        assert n == 1
    assert _idem(db, "M1") is not None

def test_reingest_after_invalid_not_created(db):
    # 非法转换被拒后不产生新行（若 DB 已有一行则保持）
    _ingest_body(db, _mk())
    _ingest_body(db, _mk(status="success", upload="pending", ok="k"))  # 非法（success 需 upload=success）
    row = _row(db, "M1")
    assert row["status"] == "pending" and row["upload_status"] is None
