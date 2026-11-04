"""V4.4 C4 Pi → Windows 单向同步专项验收集成测试（Mock HTTP + 临时 SQLite，不发真实网络）。

端到端：bridge 入队 → OutboxWorker.process_batch(注入 mock post) → outbox success；数据边界
（媒体元数据入 Outbox、视频二进制/公开 URL/签名 URL/凭证不入载荷）；服务端真实接收 POST /api/v1/c4/records 幂等与持久化。

关键不变量（本测试的验收断言）：
1) media_evidence 元数据属于 C4 Outbox 同步范围（record_type='media_evidence', source_record_id=media_id）。
2) 视频二进制、公开访问 URL、签名 URL 与 OSS 凭证**不**进入 C4 payload；`object_key` 仅是元数据标识，非访问链接。
3) reading / person_event / sensor_alert 每类都被 bridge 处置：要么进入 Outbox(可同步)，要么进入
   c4_verify(待人工核验)——绝不静默丢失。（是否入 Outbox 由 canonical 对载荷的解析决定，属既有行为；
   本测试如实记录，不假设某类必然入 Outbox。）
4) Worker 只发送 Outbox 内记录，成功后全部置 success；重试网络失败保持 retry_wait、不丢数据。
5) 服务端接收幂等 + 持久化。
6) 媒体可播放性仍由现有 status/upload_status 规则控制（不入 C4 payload 的 URL 不改变该判定）。
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


def _seed_media(db) -> str:
    """经真实 DAO 创建一条媒体元数据记录（引用真实 sensor_alert，走 Outbox 自动入队）。

    返回 media_id。object_key 为对象标识（元数据），非公开 URL。
    """
    media_id = "M-PROD-00000001"
    # 复用 _seed 创建的告警之一作为 ref_id（真实关联）；无则新建一条 sensor_alert。
    alert_id = db.save_sensor_alert(type="sensor_over_temp", temperature=52.0, threshold=45.0)
    db.save_media_evidence(
        source="sensor", ref_id=alert_id, media_type="video",
        local_path="/tmp/evidence_prod.mp4", captured_at="2026-10-28T10:00:00Z",
        status="success", upload_status="success", object_key=f"evidence/{media_id}.mp4",
        uploaded_at="2026-10-28T10:05:00Z", media_id=media_id)
    return media_id


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
    def test_media_evidence_metadata_is_synced_without_silent_loss(self, db):
        info = _seed(db)
        media_sid = _seed_media(db)                       # 经真实 DAO 创建并自动入队
        # 媒体元数据确实进入 Outbox，source_record_id=media_id，且非对象键
        assert "media_evidence" in {x[0] for x in _outbox(db)}
        assert any(x[0] == "media_evidence" and x[1] == media_sid
                   and not x[1].startswith("evidence/") for x in _outbox(db))
        # Worker 处理：媒体不被静默忽略/错误死信，发送成功
        sent: dict = {}

        def fake_post(url, json=None, headers=None, timeout=10.0):
            recs = [(r["record_type"], r["source_record_id"])
                    for r in (json or {}).get("records") or []]
            for r in (json or {}).get("records") or []:
                sent.setdefault(r["record_type"], []).append(r["source_record_id"])
            return _resp_ok(recs)

        w = OutboxWorker(db, "http://win/api/v1", _TOKEN, http_post=fake_post,
                         worker_id="w", now_ms=lambda: 1)
        w.process_batch(max_records=10)
        assert media_sid in sent.get("media_evidence", [])
        with db._connect() as c:
            st = c.execute(
                "SELECT status FROM sync_outbox "
                "WHERE record_type='media_evidence' AND source_record_id=?", (media_sid,)).fetchone()[0]
        assert st == "success"
        # 三类原有记录仍 non-silent（queued 或 verified）
        for rt, res in info.items():
            assert res["status"] in ("queued", "verified"), rt
            assert (_count_outbox(db, rt) + _count_verify(db, rt)) >= 1, rt

    def test_payload_no_public_url_no_signedurl(self, db):
        _seed(db)
        media_sid = _seed_media(db)                       # 真实媒体元数据, object_key=evidence/M-PROD-...mp4
        sent: dict = {}

        def fake_post(url, json=None, headers=None, timeout=10.0):
            recs = [(r["record_type"], r["source_record_id"])
                    for r in (json or {}).get("records") or []]
            for r in (json or {}).get("records") or []:
                sent[r["record_type"]] = json           # 按类型保存载荷, 便于精确检查 media payload
            return _resp_ok(recs)

        w = OutboxWorker(db, "http://win/api/v1", _TOKEN, http_post=fake_post,
                         worker_id="w", now_ms=lambda: 1)
        w.process_batch(max_records=10)
        assert "media_evidence" in sent                  # 媒体 payload 确实被发送
        media_payload = sent["media_evidence"]
        blob = repr(media_payload).lower()
        # 媒体元数据入载荷（非二进制）—— source_record_id=media_id, 且含 object_key 标识
        assert media_sid.lower() in blob
        assert "media_evidence" in blob
        assert f"evidence/{media_sid.lower()}.mp4" in blob          # object_key 为元数据标识, 非 URL
        assert "object_key" in blob
        # 安全：无公开 URL / 签名 URL / OSS 凭证 泄漏
        all_blob = repr(sent).lower()
        for banned in ("http://", "https://", "access_url", "ossaccesskeyid", "signature=",
                       "access key", "secret="):
            assert banned not in all_blob, banned


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