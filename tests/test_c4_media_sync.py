"""V4.4 Task3+Task4 阶段2.1 —— Edge media_evidence → Outbox → Worker → Server C4 媒体元数据同步。

覆盖（断言数据库最终状态，不只看 HTTP/返回值）：
- 创建 media 自动入队(sync_outbox pending, record_type='media_evidence', source_record_id=media_id)。
- 幂等重存(同显式 media_id)不重开、gen 不增。
- 合法状态转换重开 outbox(gen+1, 清 worker/lease/next_attempt/completed)。
- 非法/同态转换不重开、返回 False。
- Outbox 写失败时媒体 INSERT / UPDATE 整体回滚(触发器强制失败)。
- Worker 读取完整媒体业务快照(canonical 8 字段 + uploaded_at)。
- uploaded_at 经生成传输并在服务端持久化、不入 canonical hash。
- Edge Worker → Server C4 端到端：服务端 media_evidence + c4_idem 落库。
- 上传失败状态(failed-awaiting-retry / upload_status=failed)的媒体仍能同步。
- generation fencing：旧 gen 成功/重试/死信回执不能覆盖新 gen；新 claim 读最新快照。
- 断网重试与恢复。进程重启恢复(新 Worker 实例接续 pending)。
- 回归：person_event/reading/sensor_alert 不受 media 改动影响；sensor_alert resolved 生命周期不被破坏。

隔离：独立临时 edge TEST.db 与 server TEST.db，走 real record DAO + OutboxWorker + server/c4 router；
HTTP 用注入 transport（e2e 转真实 server ASGI，其余用脚本化响应），不发真实网络、不触 data/drrm.db。
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.database import canonical
from src.database.db import Database
from src.sync import bridge
from src.sync.worker import OutboxWorker
from server import c4

_TOKEN = "c4-media-sync-token"
_BASE = "http://server-test"


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body() if callable(self._body) else self._body


def _new_db(tag):
    return Database(db_path=os.path.join(tempfile.mkdtemp(), f"{tag}.db"))


@pytest.fixture()
def edge_db() -> Database:
    db = _new_db("edge_test")
    # 预置一条 sensor_alert 作为 media 的 ref_id 关联(便于 save_media_evidence)。
    db.rid = db.save_sensor_alert(type="temp-high", occurred_at="2026-10-24T12:00:00+08:00")
    yield db


def _ob(db, mid):
    with db._connect() as conn:
        r = conn.execute(
            "SELECT id,status,gen,worker_id,lease_until,next_attempt_at,completed_at,retry_count "
            "FROM sync_outbox WHERE record_type='media_evidence' AND source_record_id=?", (mid,)).fetchone()
    return dict(r) if r else None


def _save_media(db, mid=None, *, status="pending", upload=None, ok=None):
    return db.save_media_evidence(source="sensor", ref_id=db.rid, media_type="video",
                                  local_path="/tmp/m.mp4", captured_at="2026-10-24T12:00:00+08:00",
                                  status=status, media_id=mid, upload_status=upload, object_key=ok)


def _make_server_app(server_db, monkeypatch):
    monkeypatch.setenv("C4_API_TOKEN", _TOKEN)
    app = FastAPI()
    app.include_router(c4.router)
    app.dependency_overrides[c4.get_db] = lambda: server_db
    return TestClient(app)


def _server_transport(client):
    def post(url, json=None, headers=None, timeout=None):
        resp = client.post("/api/v1/c4/records", json=json, headers=headers)
        return _Resp(resp.status_code, resp.json())
    return post


# ---- 创建入队 / 幂等 / 状态分离 ----

def test_create_enqueues_media_pending(edge_db):
    mid = _save_media(edge_db)
    ob = _ob(edge_db, mid)
    assert ob is not None and ob["status"] == "pending" and ob["gen"] == 0
    with edge_db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM media_evidence").fetchone()[0]
    assert n == 1


def test_idempotent_resave_no_gen_change(edge_db):
    _save_media(edge_db, mid="M-fixed")
    _save_media(edge_db, mid="M-fixed")
    with edge_db._connect() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM media_evidence WHERE media_id='M-fixed'").fetchone()[0]
    assert rows == 1
    assert _ob(edge_db, "M-fixed")["gen"] == 0


def test_distinct_media_ids_independent(edge_db):
    _save_media(edge_db, mid="M-1")
    _save_media(edge_db, mid="M-2")
    with edge_db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM sync_outbox WHERE record_type='media_evidence'").fetchone()[0]
    assert n == 2


def test_legal_transition_reopens(edge_db):
    mid = _save_media(edge_db)
    assert edge_db.update_media_evidence_status(mid, status="processing") is True
    ob = _ob(edge_db, mid)
    assert ob["status"] == "pending" and ob["gen"] == 1
    assert ob["worker_id"] is None and ob["lease_until"] is None and ob["next_attempt_at"] is None


def test_illegal_and_self_transition_no_reopen(edge_db):
    mid = _save_media(edge_db, mid="M-fixed")
    assert edge_db.update_media_evidence_status(mid, status="success") is False  # pending->success 非法
    g0 = _ob(edge_db, mid)["gen"]
    assert edge_db.update_media_evidence_status(mid, status="processing") is True   # 合法 -> gen+1
    assert edge_db.update_media_evidence_status(mid, status="processing") is False  # 同态非法
    assert _ob(edge_db, mid)["gen"] == g0 + 1                                        # 未因同态再增


# ---- 事务回滚(用 sqlite 触发器强制 outbox 写失败) ----

def test_outbox_failure_rolls_back_media_insert(edge_db):
    with edge_db._connect() as conn:
        conn.execute("CREATE TRIGGER _fail_ob BEFORE INSERT ON sync_outbox "
                     "BEGIN SELECT RAISE(ABORT,'forced'); END")
        try:
            with pytest.raises(Exception):
                _save_media(edge_db, mid="M-rb-insert")
        finally:
            conn.execute("DROP TRIGGER IF EXISTS _fail_ob")
    with edge_db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM media_evidence").fetchone()[0]
    assert n == 0                     # 媒体 INSERT 已随事务整体回滚


def test_outbox_failure_rolls_back_media_update(edge_db):
    mid = _save_media(edge_db)
    with edge_db._connect() as conn:
        conn.execute("CREATE TRIGGER _fail_ob BEFORE INSERT ON sync_outbox "
                     "BEGIN SELECT RAISE(ABORT,'forced'); END")
        try:
            with pytest.raises(Exception):
                edge_db.update_media_evidence_status(mid, status="processing")
        finally:
            conn.execute("DROP TRIGGER IF EXISTS _fail_ob")
    with edge_db._connect() as conn:
        st = conn.execute("SELECT status FROM media_evidence WHERE media_id=?", (mid,)).fetchone()[0]
    assert st == "pending"            # 媒体状态更新已随事务整体回滚, outbox 未重开


# ---- Worker 快照 与 上传失败仍可同步 ----

def test_worker_reads_full_media_snapshot(edge_db):
    mid = _save_media(edge_db, status="pending")
    biz = OutboxWorker(edge_db, _BASE, _TOKEN)._read_business("media_evidence", mid)
    assert biz is not None and biz["media_id"] == mid and biz["source"] == "sensor"
    assert set(canonical.WHITELIST["media_evidence"]) <= set(biz)      # canonical 8 字段齐备
    assert biz["uploaded_at"] is None                                  # 传输字段 present


def test_upload_failed_status_still_syncable(edge_db):
    mid = _save_media(edge_db, status="failed-awaiting-retry", upload="failed", ok=None)
    assert _ob(edge_db, mid)["status"] == "pending"
    biz = OutboxWorker(edge_db, _BASE, _TOKEN)._read_business("media_evidence", mid)
    assert biz["status"] == "failed-awaiting-retry" and biz["upload_status"] == "failed"


def test_uploaded_at_not_in_hash(edge_db):
    mid = _save_media(edge_db, status="pending")
    edge_db.update_media_evidence_status(mid, status="processing")
    edge_db.update_media_evidence_status(mid, status="success", upload_status="success",
                                         object_key="evidence/m.mp4",
                                         uploaded_at="2026-10-24T12:05:00+08:00")
    w = OutboxWorker(edge_db, _BASE, _TOKEN)
    h1 = w._hash_for("media_evidence", mid)[0]
    biz = w._read_business("media_evidence", mid)
    biz2 = dict(biz); biz2["uploaded_at"] = "2026-10-24T99:99:99+08:00"
    h2 = w._hash_for("media_evidence", mid, biz2)[0]
    assert h1 is not None and h1 == h2     # uploaded_at 变化不影响 canonical hash


# ---- Generation fencing（DB 级） ----

def test_old_gen_cannot_complete_new_gen(edge_db):
    mid = _save_media(edge_db)
    oid = _ob(edge_db, mid)["id"]
    assert edge_db.outbox_claim("W1") == oid
    assert edge_db.get_outbox(oid)["gen"] == 1 and edge_db.get_outbox(oid)["status"] == "processing"
    # 媒体状态变化 → 重开 outbox gen2 pending worker=NULL
    assert edge_db.update_media_evidence_status(mid, status="processing") is True
    cur = _ob(edge_db, mid)
    assert cur["gen"] == 2 and cur["status"] == "pending" and cur["worker_id"] is None
    # 旧 gen=1 的 W1 各回执均不得生效
    assert edge_db.outbox_mark_success(oid, "W1", 1) is False
    assert edge_db.outbox_mark_retry_wait(oid, "W1", 1, next_attempt_at=0, last_error="e") is False
    assert edge_db.outbox_mark_dead_letter(oid, "W1", 1, last_error="e") is False
    assert _ob(edge_db, mid)["gen"] == 2 and _ob(edge_db, mid)["status"] == "pending"  # 新 gen 未丢失
    # 新 Worker 可领取并读最新快照
    assert edge_db.outbox_claim("W2") == oid
    assert edge_db.get_outbox(oid)["gen"] == 3
    biz = OutboxWorker(edge_db, _BASE, _TOKEN)._read_business("media_evidence", mid)
    assert biz["status"] == "processing"


# ---- Edge Worker → Server C4 端到端 ----

def test_e2e_media_sync_to_server(edge_db, monkeypatch):
    server_db = _new_db("server_test")
    client = _make_server_app(server_db, monkeypatch)
    mid = _save_media(edge_db, status="failed-awaiting-retry", upload="failed", ok=None)
    w = OutboxWorker(edge_db, _BASE, _TOKEN, http_post=_server_transport(client))
    results = w.process_batch(max_records=10)
    assert "success" in results
    with server_db._connect() as conn:
        row = conn.execute("SELECT * FROM media_evidence WHERE media_id=?", (mid,)).fetchone()
        idem = conn.execute("SELECT 1 FROM c4_idem WHERE record_type='media_evidence' AND source_record_id=?",
                            (mid,)).fetchone()
    assert row is not None and idem is not None
    assert row["status"] == "failed-awaiting-retry" and row["upload_status"] == "failed"
    assert _ob(edge_db, mid)["status"] == "success"           # 边缘 outbox 推进成功


def test_e2e_uploaded_at_persists_on_server(edge_db, monkeypatch):
    server_db = _new_db("server_test")
    client = _make_server_app(server_db, monkeypatch)
    mid = _save_media(edge_db, status="pending")
    edge_db.update_media_evidence_status(mid, status="processing")
    edge_db.update_media_evidence_status(mid, status="success", upload_status="success",
                                         object_key="evidence/m.mp4",
                                         uploaded_at="2026-10-24T12:05:00+08:00")
    w = OutboxWorker(edge_db, _BASE, _TOKEN, http_post=_server_transport(client))
    assert "success" in w.process_batch(max_records=10)
    with server_db._connect() as conn:
        row = conn.execute("SELECT status,uploaded_at FROM media_evidence WHERE media_id=?", (mid,)).fetchone()
    assert row["status"] == "success" and row["uploaded_at"] == "2026-10-24T12:05:00+08:00"


def test_e2e_network_retry_then_recovery(edge_db, monkeypatch):
    server_db = _new_db("server_test")
    client = _make_server_app(server_db, monkeypatch)
    mid = _save_media(edge_db, status="pending")
    state = {"fail": True}

    def transport(url, json=None, headers=None, timeout=None):
        if state["fail"]:
            state["fail"] = False
            raise ConnectionError("network down")
        return _server_transport(client)(url, json=json, headers=headers)

    w = OutboxWorker(edge_db, _BASE, _TOKEN, http_post=transport)
    w.process_batch(max_records=1)                              # 首次: claim+发送 -> 网络失败 -> retry_wait
    with edge_db._connect() as conn:
        ob = conn.execute(
            "SELECT status,retry_count FROM sync_outbox "
            "WHERE record_type='media_evidence' AND source_record_id=?", (mid,)).fetchone()
    assert ob["status"] == "retry_wait" and ob["retry_count"] >= 1
    with edge_db._connect() as conn:                             # 置为到期, 模拟断网恢复后重试
        conn.execute("UPDATE sync_outbox SET next_attempt_at=0 "
                     "WHERE record_type='media_evidence' AND source_record_id=?", (mid,))
    assert "success" in w.process_batch(max_records=1)
    with server_db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM media_evidence WHERE media_id=?", (mid,)).fetchone()[0]
    assert n == 1


def test_e2e_restart_recovery(edge_db, monkeypatch):
    server_db = _new_db("server_test")
    client = _make_server_app(server_db, monkeypatch)
    mid = _save_media(edge_db, status="pending")
    # 尚未发送: outbox 仍 pending
    assert _ob(edge_db, mid)["status"] == "pending"
    # "重启" = 由新 Worker 实例接续处理持久化的 pending 行
    w = OutboxWorker(edge_db, _BASE, _TOKEN, http_post=_server_transport(client))
    assert "success" in w.process_batch(max_records=5)
    with server_db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM media_evidence WHERE media_id=?", (mid,)).fetchone()[0]
    assert n == 1
    assert _ob(edge_db, mid)["status"] == "success"


# ---- 回归：media 改动不影响既有三类型 ----

def test_existing_sync_types_regression(edge_db):
    reading = bridge.create_sync_record(
        edge_db, record_type="reading",
        business={"sensor_id": "S1", "temperature": "25.5",
                  "device_captured_at": "2026-10-24T12:00:00+08:00"})
    alert = bridge.create_sync_record(
        edge_db, record_type="sensor_alert",
        business={"type": "t", "occurred_at": "2026-10-24T12:00:00+08:00"})
    person = bridge.create_sync_record(
        edge_db, record_type="person_event",
        business={"type": "memory_alert", "severity": "HIGH", "person_id": 1,
                  "payload": {"state": "Fall", "timestamp": "2026-10-24T12:00:00Z"}})
    assert (reading["status"], alert["status"], person["status"]) == ("queued", "queued", "queued")
    # media 与三类共存不串扰
    _save_media(edge_db, mid="M-mix")
    with edge_db._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM sync_outbox "
            "WHERE record_type IN ('person_event','reading','sensor_alert')").fetchone()[0]
    assert n == 3
    assert _ob(edge_db, "M-mix") is not None


def test_sensor_alert_resolved_lifecycle_regression(edge_db):
    a = bridge.create_sync_record(
        edge_db, record_type="sensor_alert",
        business={"type": "t", "occurred_at": "2026-10-24T12:00:00+08:00"})
    aid = a["source_record_id"]
    assert edge_db.update_sensor_alert_status(aid, "resolved") is True
    with edge_db._connect() as conn:
        row = conn.execute(
            "SELECT status,gen FROM sync_outbox WHERE record_type='sensor_alert' AND source_record_id=?",
            (aid,)).fetchone()
    assert row is not None and row["status"] == "pending" and row["gen"] >= 1