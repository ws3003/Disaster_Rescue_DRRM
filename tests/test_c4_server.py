"""V4.4 C4 服务端接收接口测试 (POST /api/v1/c4/records)。

覆盖(任务 §6.1):
1. 正确鉴权 + 正常接收;
2. 未提供 token;
3. 错误 token;
4. 必填字段缺失 / 非法请求结构;
5. 同键同内容重复提交 -> already_synced (幂等);
6. 同键异内容 -> content_mismatch + 冲突审计;
7. content_mismatch 不被误报为普通成功;
8. 接收端 ingest 异常时事务回滚, 不留下半写入记录;
9. 整批预校验: 任一条非法 -> 整体 400, 不产生部分写入;
10. API envelope / HTTP 状态码与既定契约一致。

隔离: 独立临时 SQLite (依赖覆盖 get_db), 内置最小 FastAPI app 挂载 c4.router,
使用真实 TestClient 走 HTTP 层, 不触碰 data/drrm.db。
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import c4
from src.database.db import Database

TOKEN = "c4-test-token"


@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    monkeypatch.setenv("C4_API_TOKEN", TOKEN)


def _make_app(db: Database) -> TestClient:
    app = FastAPI()
    app.include_router(c4.router)
    app.dependency_overrides[c4.get_db] = lambda: db
    return TestClient(app)


@pytest.fixture()
def client() -> TestClient:
    tmp = tempfile.TemporaryDirectory()
    db = Database(db_path=os.path.join(tmp.name, "c4_server_test.db"))
    yield _make_app(db)


def _rec(rt="sensor_alert", sid="E-1000-00000001", ch="hash-1"):
    return {"record_type": rt, "source_record_id": sid, "content_hash": ch}


def _idem_count(db: Database, sid="E-1000-00000001") -> int:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM c4_idem WHERE source_record_id=?",
            (sid,),
        ).fetchone()
    return int(row["n"])


class TestAuth:
    def test_correct_token_normal_receive(self, client):
        r = client.post("/api/v1/c4/records", json={"records": [_rec()]},
                        headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "OK" and body["message"] == "ok"
        assert body["data"][0]["status"] == "synced"
        assert body["meta"]["succeeded"] == 1

    def test_missing_token_401(self, client):
        r = client.post("/api/v1/c4/records", json={"records": [_rec()]})
        assert r.status_code == 401
        body = r.json()
        assert body["code"] == "UNAUTHORIZED" and body["data"] is None and body["meta"] == {}

    @pytest.mark.parametrize("bad", [
        "Bearer wrong",
        f"bearer {TOKEN}",
        f"Basic {TOKEN}",
        "Token abc",
        "",
    ])
    def test_wrong_token_401(self, client, bad):
        r = client.post("/api/v1/c4/records", json={"records": [_rec()]},
                        headers={"Authorization": bad})
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"

    def test_server_unconfigured_token_500(self, client, monkeypatch):
        monkeypatch.delenv("C4_API_TOKEN", raising=False)
        r = client.post("/api/v1/c4/records", json={"records": [_rec()]},
                        headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 500 and r.json()["code"] == "INTERNAL_ERROR"


class TestValidation:
    def test_records_missing_or_not_list_400(self, client):
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert client.post("/api/v1/c4/records", json={}, headers=auth).status_code == 400
        assert client.post("/api/v1/c4/records", json={"records": {}}, headers=auth).status_code == 400
        assert client.post("/api/v1/c4/records", json={"records": []}, headers=auth).status_code == 400

    def test_missing_required_field_400(self, client):
        auth = {"Authorization": f"Bearer {TOKEN}"}
        cases = [
            {"record_type": "sensor_alert", "source_record_id": "E-1"},      # 缺 content_hash
            {"record_type": "sensor_alert", "content_hash": "h"},            # 缺 source_record_id
            {"source_record_id": "E-1", "content_hash": "h"},                # 缺 record_type
            {"record_type": "bogus", "source_record_id": "E-1", "content_hash": "h"},  # 非法类型
            "not-a-dict",                                                    # 非 dict 记录
        ]
        for rec in cases:
            r = client.post("/api/v1/c4/records", json={"records": [rec]}, headers=auth)
            assert r.status_code == 400 and r.json()["code"] == "INVALID_PARAMETER", rec
        assert _idem_count(client.app.dependency_overrides[c4.get_db]()) == 0

    def test_batch_one_invalid_rejects_whole(self, client):
        db = client.app.dependency_overrides[c4.get_db]()
        r = client.post(
            "/api/v1/c4/records",
            json={"records": [_rec("sensor_alert", "A", "h1"), _rec("sensor_alert", "B", "")]},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert r.status_code == 400
        # 整批预校验: 非法请求不产生任何半写入
        assert _idem_count(db) == 0

    def test_non_list_payload_400(self, client):
        r = client.post("/api/v1/c4/records", json={"records": "nope"},
                        headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 400


class TestIdempotencyConflict:
    def test_same_key_same_content_already_synced(self, client):
        db = client.app.dependency_overrides[c4.get_db]()
        auth = {"Authorization": f"Bearer {TOKEN}"}
        r1 = client.post("/api/v1/c4/records", json={"records": [_rec()]}, headers=auth)
        assert r1.json()["data"][0]["status"] == "synced"
        r2 = client.post("/api/v1/c4/records", json={"records": [_rec()]}, headers=auth)
        assert r2.status_code == 200
        assert r2.json()["data"][0]["status"] == "already_synced"
        assert _idem_count(db) == 1  # 不重复登记

    def test_same_key_diff_content_conflict(self, client):
        db = client.app.dependency_overrides[c4.get_db]()
        auth = {"Authorization": f"Bearer {TOKEN}"}
        r1 = client.post("/api/v1/c4/records", json={"records": [_rec(ch="hash-first")]}, headers=auth)
        assert r1.json()["data"][0]["status"] == "synced"
        r2 = client.post("/api/v1/c4/records", json={"records": [_rec(ch="hash-different")]}, headers=auth)
        assert r2.status_code == 200
        item = r2.json()["data"][0]
        assert item["status"] == "content_mismatch"
        # 首次哈希保留, 冲突审计追加
        with db._connect() as conn:
            h = conn.execute(
                "SELECT content_hash FROM c4_idem WHERE source_record_id='E-1000-00000001'"
            ).fetchone()[0]
            assert h == "hash-first"
            n = conn.execute("SELECT COUNT(*) AS n FROM c4_content_conflict").fetchone()[0]
            assert n == 1

    def test_conflict_not_reported_as_success(self, client):
        auth = {"Authorization": f"Bearer {TOKEN}"}
        client.post("/api/v1/c4/records", json={"records": [_rec(ch="h-a")]}, headers=auth)
        r = client.post("/api/v1/c4/records", json={"records": [_rec(ch="h-b")]}, headers=auth)
        body = r.json()
        assert body["data"][0]["status"] == "content_mismatch"
        assert body["meta"]["succeeded"] == 0
        assert body["meta"]["conflicted"] == 1


class TestRollback:
    def test_ingest_exception_rolls_back_no_partial(self, client, monkeypatch):
        db = client.app.dependency_overrides[c4.get_db]()

        def _broken_ingest(inst_db, rec):
            # 在真实事务内先写入再抛错 -> with_transaction 应回滚, 不留半成品
            with inst_db.with_transaction() as tx:
                tx.execute(
                    "INSERT INTO c4_idem (record_type, source_record_id, content_hash, received_at) "
                    "VALUES ('sensor_alert', 'E-INJECT', 'h', 1)"
                )
                raise RuntimeError("inject failure")

        monkeypatch.setattr(c4, "_ingest", _broken_ingest)
        r = client.post("/api/v1/c4/records", json={"records": [_rec("sensor_alert", "E-INJECT", "h")]},
                        headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 500 and r.json()["code"] == "INTERNAL_ERROR"
        # 异常回滚: 不留半写入登记
        assert _idem_count(db, sid="E-INJECT") == 0
        with db._connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM c4_content_conflict").fetchone()[0]
            assert n == 0


class TestEnvelope:
    def test_contract_envelope_fields(self, client):
        r = client.post("/api/v1/c4/records", json={"records": [_rec()]},
                        headers={"Authorization": f"Bearer {TOKEN}"})
        body = r.json()
        assert set(body.keys()) == {"code", "message", "data", "meta"}
        item = body["data"][0]
        assert set(item.keys()) == {"record_type", "source_record_id", "status", "error_code"}
        assert item["record_type"] == "sensor_alert"
        assert item["error_code"] is None

    def test_http_status_ok_200(self, client):
        r = client.post("/api/v1/c4/records", json={"records": [_rec()]},
                        headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200