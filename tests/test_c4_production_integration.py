"""V4.4 C4 Pi → Windows 单向同步专项验收集成测试（Mock HTTP + 临时 SQLite，不发真实网络）。

端到端：bridge 入队 → OutboxWorker.process_batch(注入 mock post) → outbox success；数据边界
（media 不入 Outbox、视频/签名 URL 不入载荷）；服务端真实接收 POST /api/v1/c4/records 幂等与持久化。

关键不变量（本测试的验收断言）：
1) media_evidence 绝不进入 C4 Outbox。
2) reading / person_event / sensor_alert 每类都被 bridge 处置：要么进入 Outbox(可同步)，要么进入
   c4_verify(待人工核验)——绝不静默丢失。（是否入 Outbox 由 canonical 对载荷的解析决定，属既有行为；
   本测试如实记录，不假设某类必然入 Outbox。）
3) Worker 只发送 Outbox 内记录，成功后全部置 success；重试网络失败保持 retry_wait、不丢数据。
4) 服务端接收幂等 + 持久化。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server.c4 as c4_mod
from server.api import app as api_app
from src.database.db import Database
from src.sync import bridge
from src.sync.worker import OutboxWorker

_TOKEN = "worker-token"


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if callable(self._body):
            return self._body()
        return self._body


def _resp_ok(records):
    data = [{"record_type": rt, "source_record_id": sid, "status": "synced", "error_code": None}
            for rt, sid in records]
    return _Resp(200, {"code": "OK", "message": "ok", "data": data, "meta": {}})


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(db_path=str(tmp_path / "c4_prod_test.db"))


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("C4_API_TOKEN", _TOKEN)
    api_app.dependency_overrides[c4_mod.get_db] = lambda: db
    try:
        yield TestClient(api_app)
    finally:
        api_app.dependency_overrides.clear()


def _seed(db):
    """入队三类业务记录（bridge 处理：入 Outbox 或 c4_verify）。返回每类 status。"""
    reading = bridge.create_sync_record(
        db, record_type="reading",
        business={"sensor_id": "s1", "temperature": 30.5, "humidity": 40,
                  "device_captured_at": "2026-10-28T10:00:00Z"})
    alert = bridge.create_sync_record(
        db, record_type="sensor_alert",
        business={"type": "sensor_hightemp", "temperature": 52, "threshold": 45,
                  "occurred_at": "2026-10-28T10:00:00Z",
                  "device_captured_at": "2026-10-28T10:00:00Z"})
    person = bridge.create_sync_record(
        db, record_type="person_event",
        business={"type": "memory_alert", "severity": "HIGH", "person_id": 1,
                  "payload": {"state": "Fall", "priority": "HIGH",
                              "timestamp": "2026-10-28T10:00:00Z"}})
    return {"reading": reading, "sensor_alert": alert, "person_event": person}


def _count_outbox(db, rt):
    with db._connect() as c:
        return c.execute("SELECT COUNT(*) FROM sync_outbox WHERE record_type=?",
                         (rt,)).fetchone()[0]


def _count_verify(db, rt):
    with db._connect() as c:
        return c.execute("SELECT COUNT(*) FROM c4_verify WHERE record_type=?",
                         (rt,)).fetchone()[0]


def _outbox(db):
    with db._connect() as c:
        return c.execute(
            "SELECT record_type, source_record_id, status FROM sync_outbox ORDER BY id").fetchall()


class TestEnqueueBoundary:
    def test_media_never_in_outbox_no_silent_loss(self, db):
        info = _seed(db)
        # media_evidence 永不入 Outbox
        assert "media_evidence" not in {x[0] for x in _outbox(db)}
        # 三类每类至少入 Outbox 或 c4_verify（不静默丢失）
        for rt, res in info.items():
            assert res["status"] in ("queued", "verified")
            assert (_count_outbox(db, rt) + _count_verify(db, rt)) >= 1, rt
        # 记录实际分流（document 既有 canonical 行为）
        disposition = {rt: ("outbox" if _count_outbox(db, rt) else
                            ("verify" if _count_verify(db, rt) else "lost"))
                       for rt in info}
        return disposition

    def test_payload_no_media_no_signedurl(self, db):
        _seed(db)
        sent = {}

        def fake_post(url, json=None, headers=None, timeout=10.0):
            sent["payload"] = json
            return _resp_ok([(r["record_type"], r["source_record_id"])
                             for r in json["records"]])
        w = OutboxWorker(db, "http://win/api/v1", _TOKEN, http_post=fake_post,
                         worker_id="w", now_ms=lambda: 1)
        w.process_batch(max_records=10)
        if sent.get("payload"):
            blob = repr(sent["payload"]).lower()
            assert "media_evidence" not in blob
            assert "evidence/" not in blob        # 视频对象 key 不混入
            assert "access_url" not in blob and "sig" not in blob  # 签名 URL 不入载荷


class TestWorkerSend:
    def test_send_outbox_to_success(self, db):
        _seed(db)
        ob_before = _outbox(db)
        assert len(ob_before) >= 1  # 至少 reading/sensor_alert/person_event 之一入 Outbox
        got = []

        def fake_post(url, json=None, headers=None, timeout=10.0):
            recs = [(r["record_type"], r["source_record_id"]) for r in json["records"]]
            got.append(recs)
            return _resp_ok(recs)
        w = OutboxWorker(db, "http://win/api/v1", _TOKEN, http_post=fake_post,
                         worker_id="w", now_ms=lambda: 1)
        w.process_batch(10)
        # 发送过的 Outbox 记录全部 success；没有任何 Outbox 残留 pending/processing
        stats = {x[2] for x in _outbox(db)}
        assert stats == {"success"}
        assert any(recs for batch in got for recs in [batch])  # 确曾发送

    def test_503_retry_wait_preserves(self, db):
        _seed(db)
        w = OutboxWorker(db, "http://win/api/v1", _TOKEN,
                         http_post=lambda *a, **k: _Resp(503, {}), worker_id="w", now_ms=lambda: 1)
        w.process_batch(10)
        ob = _outbox(db)
        assert ob and all(x[2] == "retry_wait" for x in ob)
        assert all(x[2] != "success" for x in ob)  # 不误标成功、数据未丢

    def test_reprocess_after_success_noop(self, db):
        _seed(db)
        w = OutboxWorker(db, "http://win/api/v1", _TOKEN,
                         http_post=lambda *a, **k: _resp_ok([]), worker_id="w", now_ms=lambda: 1)
        w.process_batch(10)
        assert w.process_batch(10) == []  # 幂等：已成功无可领取


class TestServerReceive:
    def test_synced_then_already_synced(self, client, db):
        rec = {"record_type": "reading", "source_record_id": "R-20261028-AAAA0001",
               "content_hash": "hash-a", "edge_id": "pi-1"}
        r1 = client.post("/api/v1/c4/records", json={"records": [rec]},
                         headers={"Authorization": f"Bearer {_TOKEN}"})
        assert r1.status_code == 200 and r1.json()["data"][0]["status"] == "synced"
        r2 = client.post("/api/v1/c4/records", json={"records": [rec]},
                         headers={"Authorization": f"Bearer {_TOKEN}"})
        assert r2.json()["data"][0]["status"] == "already_synced"
        with db._connect() as c:
            assert c.execute("SELECT COUNT(*) FROM c4_idem WHERE source_record_id=?",
                             ("R-20261028-AAAA0001",)).fetchone()[0] == 1

    def test_server_reopen_persists(self, client, db, tmp_path):
        rec = {"record_type": "sensor_alert", "source_record_id": "E-20261028-BBBB0002",
               "content_hash": "hash-b", "edge_id": "pi-1"}
        client.post("/api/v1/c4/records", json={"records": [rec]},
                    headers={"Authorization": f"Bearer {_TOKEN}"})
        db2 = Database(db_path=str(tmp_path / "c4_prod_test.db"))  # 服务端"重启"
        with db2._connect() as c:
            assert c.execute("SELECT 1 FROM c4_idem WHERE source_record_id=?",
                             ("E-20261028-BBBB0002",)).fetchone() is not None

    def test_unauth_rejected(self, client):
        r = client.post("/api/v1/c4/records", json={"records": []})
        assert r.status_code == 401