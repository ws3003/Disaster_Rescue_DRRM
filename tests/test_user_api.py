"""WP-2.2 微信 Session 用户态查询接口（/api/v1/user/*）测试。

隔离：tempfile 临时 SQLite；认证依赖 = 真实 get_current_user（经 override s4.get_db
指向临时库）；不经真实微信/OSS/DB。
覆盖：路由注册、401（无/无效/过期/撤销/有效）、有效访问、单租户共享、S4 兼容、
与原 S4 共享查询结果的业务一致性。
"""
from __future__ import annotations

import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import server.api as api_mod
import server.s4 as s4_mod
from server import auth as auth_mod
from src.database.db import Database

USER_PATHS = [
    "/api/v1/user/system/status",
    "/api/v1/user/alerts",
    "/api/v1/user/alert",     # 占位，实际经 alert_key 路径（见 register 断言）
    "/api/v1/user/environment/readings",
]


@pytest.fixture()
def db():
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "user_api_test.db"))
    _seed(d)
    yield d
    try:
        tmp.cleanup()
    except Exception:  # noqa: BLE001
        pass


def _seed(d: Database):
    d.save_reading(sensor_id="s1", temperature=51.0, humidity=31.0,
                   device_captured_at="2026-10-24T10:00:00+08:00")
    d.save_reading(sensor_id="s1", temperature=52.0, humidity=32.0,
                   device_captured_at="2026-10-24T10:01:00+08:00")
    d.save_event(type="high_risk_alert", severity="high", person_id=1,
                 payload={"title": "受困", "state": "Lying", "priority": "HIGH"})
    d.save_sensor_alert(type="sensor_over_temp", temperature=52.0, threshold=45.0)


@pytest.fixture()
def client(db):
    overrides = api_mod.app.dependency_overrides
    had = s4_mod.get_db in overrides
    original = overrides.get(s4_mod.get_db)
    overrides[s4_mod.get_db] = lambda: db
    try:
        yield TestClient(api_mod.app)
    finally:
        if had:
            overrides[s4_mod.get_db] = original
        else:
            overrides.pop(s4_mod.get_db, None)


def _user(db, openid: str, user_id: str, *, expires_s: int = 86400):
    uid = db.create_user(openid, user_id=user_id)
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_s)).isoformat(
        timespec="seconds")
    db.create_session(uid, auth_mod._sha256(token), expires_at=expires_at)
    return token


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


class TestRegistration:
    def test_user_routes_registered(self, db):
        paths = set(api_mod.app.openapi().get("paths", {}) or {})
        for p in ["/api/v1/user/system/status", "/api/v1/user/alerts",
                  "/api/v1/user/alerts/{alert_key}",
                  "/api/v1/user/alerts/{alert_key}/related",
                  "/api/v1/user/environment/readings"]:
            assert p in paths, p


class TestAuth:
    def test_missing_header_401(self, client, db):
        assert client.get("/api/v1/user/alerts").status_code == 401

    def test_invalid_token_401(self, client, db):
        assert client.get("/api/v1/user/alerts",
                          headers=_bearer("not-a-real-token")).status_code == 401

    def test_expired_token_401(self, client, db):
        tok = _user(db, "openid-exp", "U-EXP", expires_s=-3600)
        assert client.get("/api/v1/user/alerts", headers=_bearer(tok)).status_code == 401

    def test_revoked_token_401(self, client, db):
        uid = db.create_user("openid-rv", user_id="U-RV")
        tok = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(seconds=86400)).isoformat(timespec="seconds")
        db.create_session(uid, auth_mod._sha256(tok), expires_at=exp)
        db.revoke_session(_last_session(db, uid))
        assert client.get("/api/v1/user/alerts", headers=_bearer(tok)).status_code == 401

    def test_valid_token_200_with_data(self, client, db):
        tok = _user(db, "openid-ok", "U-OK")
        r = client.get("/api/v1/user/alerts", headers=_bearer(tok))
        assert r.status_code == 200
        assert "data" in r.json()


def _last_session(db, uid):
    with db._connect() as c:
        row = c.execute(
            "SELECT session_id FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
            (uid,)).fetchone()
    return row[0] if row else None


class TestBusinessReuse:
    def test_system_status_reused(self, client, db):
        tok = _user(db, "openid-s", "U-S")
        r = client.get("/api/v1/user/system/status", headers=_bearer(tok))
        assert r.status_code == 200
        assert r.json()["data"]["device"]["status"] == "unknown"  # 与 S4 占位一致

    def test_alerts_list_different_users_share(self, client, db):
        ta = _user(db, "openid-a", "U-A")
        tb = _user(db, "openid-b", "U-B")
        ra = client.get("/api/v1/user/alerts", headers=_bearer(ta)).json()
        rb = client.get("/api/v1/user/alerts", headers=_bearer(tb)).json()
        ka = [x["alert_key"] for x in ra["data"]]
        kb = [x["alert_key"] for x in rb["data"]]
        assert ka and ka == kb  # 单租户共享：两用户同一数据集

    def test_user_query_equals_s4_query(self, client, db, monkeypatch):
        monkeypatch.setenv("S4_API_TOKEN", "tok")
        tok = _user(db, "openid-c", "U-C")
        uh = client.get("/api/v1/user/alerts", headers=_bearer(tok)).json()
        sh = client.get("/api/v1/alerts", headers=_bearer("tok")).json()  # S4 服务 Bearer
        assert [x["alert_key"] for x in sh["data"]] == [x["alert_key"] for x in uh["data"]]

    def test_alert_detail_and_readings(self, client, db):
        tok = _user(db, "openid-d", "U-D")
        alerts = client.get("/api/v1/user/alerts", headers=_bearer(tok)).json()["data"]
        if alerts:
            ak = alerts[0]["alert_key"]
            det = client.get(f"/api/v1/user/alerts/{ak}", headers=_bearer(tok))
            assert det.status_code == 200 and "data" in det.json()
        rd = client.get("/api/v1/user/environment/readings", headers=_bearer(tok))
        assert rd.status_code == 200
        assert rd.json()["data"] and "reading_id" in rd.json()["data"][0]

    def test_s4_route_no_session_but_service_token(self, client, db, monkeypatch):
        monkeypatch.setenv("S4_API_TOKEN", "s4tok")
        # S4 原路由：不需要微信 Session，使用 S4_API_TOKEN
        assert client.get("/api/v1/alerts", headers=_bearer("s4tok")).status_code == 200