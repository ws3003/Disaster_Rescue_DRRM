"""V4.4 微信认证模块 HTTP 层测试（`tests/test_auth.py`）。

- 使用 FastAPI TestClient（HTTP 层），依赖注入结果 get_db 返回临时 SQLite，不写 data/drrm.db。
- 微信 code2session 全部 Mock：不访问真实微信；AppSecret/session_key/明文 Token 不入库不落日志。
- 断言响应信封 {code,message,data,meta}，code 字符串。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

from src.database.db import Database
from server import auth as auth_mod
from server.wechat import CODE2SESSION_URL, WechatClient


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture()
def db():
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "auth_test.db"))
    d._tmpdir = tmp
    return d


@pytest.fixture()
def client(db):
    """TestClient + FastAPI dependency_overrides 注入临时 SQLite get_db；
    teardown 恢复原覆盖状态（只清理本 fixture 设置的 s4.get_db），避免污染共享 app。"""
    from server import s4, api as api_mod
    # 覆盖 get_db 依赖（auth 与 s4 共用 server.s4.get_db 同函数对象，一次覆盖即可）
    overrides = api_mod.app.dependency_overrides
    had_original = s4.get_db in overrides
    original = overrides.get(s4.get_db)
    overrides[s4.get_db] = lambda: db
    test_client = TestClient(api_mod.app)
    try:
        yield test_client
    finally:
        if had_original:
            overrides[s4.get_db] = original
        else:
            overrides.pop(s4.get_db, None)


# 记录微信调用（避免真实请求）
class _FakeWechat:
    def __init__(self, openid="WX-OPENID-1", errcode=0):
        self.openid = openid
        self.errcode = errcode
        self.calls = 0

    def code2session(self, code):
        self.calls += 1
        if self.errcode:
            from server.wechat import WechatAuthError
            raise WechatAuthError("wechat errcode", errcode=self.errcode)
        return type("R", (), {"openid": self.openid, "session_key": "SK-1"})()


def _patch_wechat(monkeypatch, fake):
    monkeypatch.setattr(auth_mod, "WechatClient", lambda **k: fake)


# ----------------------------------------------------------------------
# 登录
# ----------------------------------------------------------------------
class TestLogin:
    def test_login_success(self, client, monkeypatch):
        _patch_wechat(monkeypatch, _FakeWechat())
        r = client.post("/api/v1/auth/login", json={"code": "mock-code"})
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "OK"
        data = body["data"]
        assert data["session_token"] and data["user_id"].startswith("U-")
        assert data["expires_in"] == 86400

    def test_login_missing_code(self, client):
        # body 为空 -> 400 INVALID_PARAMETER
        r = client.post("/api/v1/auth/login", json={})
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_PARAMETER"

    def test_login_empty_code(self, client):
        r = client.post("/api/v1/auth/login", json={"code": "   "})
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_PARAMETER"

    def test_login_wechat_errcode(self, client, monkeypatch):
        _patch_wechat(monkeypatch, _FakeWechat(errcode=40029))
        r = client.post("/api/v1/auth/login", json={"code": "bad"})
        assert r.status_code == 502
        assert r.json()["code"] == "WECHAT_ERROR"

    def test_login_network_error(self, client, monkeypatch):
        def boom(**k):
            raise _WechatNet()
        class _WechatNet(Exception):
            pass
        from server.wechat import WechatAuthError
        fake = type("F", (), {"code2session": lambda self, c: (_ for _ in ()).throw(WechatAuthError("network"))})()
        _patch_wechat(monkeypatch, fake)
        r = client.post("/api/v1/auth/login", json={"code": "x"})
        assert r.status_code == 502
        assert r.json()["code"] == "WECHAT_ERROR"

    def test_login_missing_openid(self, client, monkeypatch):
        from server.wechat import WechatAuthError
        fake = type("F", (), {"code2session": lambda self, c: (_ for _ in ()).throw(WechatAuthError("missing openid"))})()
        _patch_wechat(monkeypatch, fake)
        r = client.post("/api/v1/auth/login", json={"code": "x"})
        assert r.status_code == 502
        assert r.json()["code"] == "WECHAT_ERROR"

    def test_login_repeat_openid_same_user(self, client, monkeypatch):
        _patch_wechat(monkeypatch, _FakeWechat(openid="dup-openid"))
        r1 = client.post("/api/v1/auth/login", json={"code": "c1"})
        r2 = client.post("/api/v1/auth/login", json={"code": "c2"})
        assert r1.json()["data"]["user_id"] == r2.json()["data"]["user_id"]  # 同一内部用户

    def test_db_stores_hash_not_plaintext(self, client, db, monkeypatch):
        _patch_wechat(monkeypatch, _FakeWechat())
        r = client.post("/api/v1/auth/login", json={"code": "mock-code"})
        token = r.json()["data"]["session_token"]
        # 直接用 db fixture（登录经 dependency_overrides 写入同一临时库），不用 auth_mod.get_db()
        with db._connect() as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(sessions)").fetchall()]
            rows = c.execute("SELECT session_token_hash FROM sessions").fetchall()
        assert "session_token" not in cols            # 无明文列
        hashes = [r[0] for r in rows]
        assert hashes and all(len(h) == 64 for h in hashes)   # sha256 长度
        assert all(token not in h for h in hashes)     # 明文不在哈希值里


class TestMe:
    def _login(self, client, monkeypatch):
        _patch_wechat(monkeypatch, _FakeWechat())
        return client.post("/api/v1/auth/login", json={"code": "c"})

    def test_me_ok(self, client, monkeypatch):
        tok = self._login(client, monkeypatch).json()["data"]["session_token"]
        r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["data"]["user_id"].startswith("U-")

    def test_me_missing_token(self, client):
        r = client.get("/api/v1/auth/me")
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"

    def test_me_wrong_token(self, client):
        r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"

    def test_me_revoked_token(self, client, db, monkeypatch):
        tok = self._login(client, monkeypatch).json()["data"]["session_token"]
        # 撤销当前 session（用 db fixture，登录写入同一临时库）
        sess = db.get_session_by_hash(auth_mod._sha256(tok))
        assert sess is not None
        db.revoke_session(sess["session_id"])
        r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"


class TestLogout:
    def _login(self, client, monkeypatch):
        _patch_wechat(monkeypatch, _FakeWechat())
        return client.post("/api/v1/auth/login", json={"code": "c"})

    def test_logout_ok(self, client, monkeypatch):
        tok = self._login(client, monkeypatch).json()["data"]["session_token"]
        r = client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["code"] == "OK"

    def test_logout_then_me_401(self, client, monkeypatch):
        tok = self._login(client, monkeypatch).json()["data"]["session_token"]
        client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {tok}"})
        r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"

    def test_logout_repeat_safe(self, client, monkeypatch):
        tok = self._login(client, monkeypatch).json()["data"]["session_token"]
        h = {"Authorization": f"Bearer {tok}"}
        client.post("/api/v1/auth/logout", headers=h)
        r2 = client.post("/api/v1/auth/logout", headers=h)   # 已撤销，重复登出应安全（幂等，无 500/崩溃）
        assert r2.status_code in (200, 401)                  # 幂等安全即可

    def test_logout_does_not_affect_other_session(self, client, monkeypatch):
        # 同一用户两个 session，撤销一个不影响另一个
        _patch_wechat(monkeypatch, _FakeWechat())
        t1 = client.post("/api/v1/auth/login", json={"code": "c1"}).json()["data"]["session_token"]
        t2 = client.post("/api/v1/auth/login", json={"code": "c2"}).json()["data"]["session_token"]
        client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {t1}"})
        r2 = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {t2}"})
        assert r2.status_code == 200                         # 同用户另一 session 仍有效


class TestSecurity:
    def test_response_no_secret_or_key(self, client, monkeypatch):
        from server.wechat import WechatAuthError
        captured = {}
        class F:
            def code2session(self, code):
                return type("R", (), {"openid": "o", "session_key": "S3CRET-KEY"})()
        _patch_wechat(monkeypatch, F())
        r = client.post("/api/v1/auth/login", json={"code": "c"})
        assert "S3CRET-KEY" not in r.text
        assert "APP_SECRET" not in r.text
        assert "session_key" not in r.text

    def test_error_does_not_leak_wechat_raw(self, client, monkeypatch):
        from server.wechat import WechatAuthError
        class F:
            def code2session(self, code):
                raise WechatAuthError("errcode", errcode=40029)
        _patch_wechat(monkeypatch, F())
        r = client.post("/api/v1/auth/login", json={"code": "x"})
        assert r.json()["message"] != "errcode"           # 不透传微信原始错误