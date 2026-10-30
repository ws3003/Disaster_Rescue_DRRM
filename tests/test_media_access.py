"""V4.4 P0-2B D 组：媒体访问 API（接口 A: 告警媒体列表 / 接口 B: 短期签名 URL）测试。

隔离策略：临时 SQLite；认证依赖 override（已登录哨兵用户）；签名 URL 用假 Storage mock。
不连接真实 OSS、不读取真实凭据、不发网络请求、不污染全局环境变量。

覆盖（errors 均为四字段 envelope {code,message,data,meta}）：
- 认证：未登录 / 无效会话 → 401（验证 AuthError 全局处理器，而非 500）。
- 接口 A：person/sensor 存在性、空列表、分页、has_more、不泄露 object_key/local_path/Bucket/URL。
- 接口 B：404 / 409（双 success 校验）/ 成功 / 拒绝客户端自定义 object_key / 503 / 500 / 不回写 DB。
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import server.api as api_mod
import server.media as media_mod
import server.s4 as s4_mod
from server.auth import get_current_user
from src.database.db import Database
from src.storage import StorageSignError


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture()
def db():
    tmp = tempfile.TemporaryDirectory()
    db = Database(db_path=os.path.join(tmp.name, "media_access_test.db"))
    yield db
    try:
        tmp.cleanup()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture()
def client(db):
    api_mod.app.dependency_overrides[s4_mod.get_db] = lambda: db
    # 默认"已登录"：D0-01 无逐用户逻辑，用一个哨兵用户即可。
    api_mod.app.dependency_overrides[get_current_user] = lambda: {"user_id": "U-TEST"}
    try:
        yield TestClient(api_mod.app)
    finally:
        api_mod.app.dependency_overrides.clear()


class _FakeStorage:
    """签名假件：记录 (object_key, ttl)；可配置抛异常。"""

    def __init__(self, error: Exception | None = None):
        self.calls: list = []
        self.error = error

    def sign_get_url(self, object_key: str, ttl: int = 300) -> str:
        self.calls.append((object_key, ttl))
        if self.error is not None:
            raise self.error
        return f"https://evb/{object_key}?sig&exp={ttl}"


def _monkey_storage(monkeypatch, error=None):
    fake = _FakeStorage(error=error)
    monkeypatch.setattr(media_mod, "create_storage", lambda: fake)
    return fake


def _make_sensor_alert(db) -> str:
    return db.save_sensor_alert(type="sensor_over_temp", temperature=52.0, threshold=45.0)


def _make_person_event(db) -> str:
    return db.save_event(type="high_risk_alert", severity="HIGH", person_id=1, payload={"state": "Fall"})


def _make_media(db, alert_id, *, status="success", upload_status="success",
                object_key="evidence/m-1.mp4"):
    return db.save_media_evidence(
        source="sensor", ref_id=alert_id, media_type="video", local_path="/tmp/m-1.mp4",
        captured_at="2026-10-24T12:00:00+08:00", status=status,
        upload_status=upload_status, object_key=object_key,
        uploaded_at="2026-10-24T12:02:00+08:00",
    )


# --------------------------------------------------------------------------
# 认证（AuthError 全局处理器 → 401，非 500）
# --------------------------------------------------------------------------

class TestAuth:
    def test_list_media_unauthenticated_401(self, client, db):
        api_mod.app.dependency_overrides.pop(get_current_user, None)  # 走真实 get_current_user
        r = client.get("/api/v1/alerts/sensor:E-1234-ABCDEF01/media")
        assert r.status_code == 401
        body = r.json()
        assert body["code"] == "UNAUTHORIZED" and set(body) == {"code", "message", "data", "meta"}
        api_mod.app.dependency_overrides[get_current_user] = lambda: {"user_id": "U-TEST"}

    def test_access_media_invalid_session_401(self, client, db):
        api_mod.app.dependency_overrides.pop(get_current_user, None)  # 走真实 get_current_user
        r = client.post("/api/v1/media/M-19990101-DEAD0001/access",
                        headers={"Authorization": "Bearer bad-token"})
        assert r.status_code == 401
        assert r.json()["code"] == "UNAUTHORIZED"
        api_mod.app.dependency_overrides[get_current_user] = lambda: {"user_id": "U-TEST"}


# --------------------------------------------------------------------------
# 接口 A：告警关联媒体列表
# --------------------------------------------------------------------------

class TestAlertMediaList:
    def test_person_alert_exists_empty(self, client, db):
        eid = _make_person_event(db)
        r = client.get(f"/api/v1/alerts/person:{eid}/media")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "OK" and body["data"] == [] and body["meta"]["total"] == 0

    def test_person_alert_missing_404(self, client, db):
        r = client.get("/api/v1/alerts/person:E-19990101-DEAD0001/media")
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"

    def test_sensor_alert_exists_returns_media(self, client, db):
        aid = _make_sensor_alert(db)
        mid = _make_media(db, aid)
        r = client.get(f"/api/v1/alerts/sensor:{aid}/media")
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 1 and data[0]["media_id"] == mid
        assert data[0]["media_type"] == "video"
        # 不泄露敏感/定位字段
        row = data[0]
        assert set(row) == {"media_id", "media_type", "status", "upload_status",
                            "captured_at", "uploaded_at"}

    def test_sensor_alert_missing_404(self, client, db):
        r = client.get("/api/v1/alerts/sensor:E-19990101-DEAD0002/media")
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"

    def test_invalid_alert_key_400(self, client, db):
        r = client.get("/api/v1/alerts/garbage/media")
        assert r.status_code == 400 and r.json()["code"] == "INVALID_PARAMETER"

    def test_pagination_and_has_more(self, client, db):
        aid = _make_sensor_alert(db)
        for i in range(3):
            _make_media(db, aid, object_key=f"evidence/m{i}.mp4")
        r = client.get(f"/api/v1/alerts/sensor:{aid}/media", params={"page": "1", "page_size": "2"})
        assert r.status_code == 200
        meta = r.json()["meta"]
        assert meta["total"] == 3 and meta["page"] == 1 and meta["page_size"] == 2
        assert meta["has_more"] is True
        assert len(r.json()["data"]) == 2

    def test_no_object_key_localpath_leak(self, client, db):
        aid = _make_sensor_alert(db)
        _make_media(db, aid)
        r = client.get(f"/api/v1/alerts/sensor:{aid}/media")
        text = r.text
        assert "object_key" not in text and "local_path" not in text
        assert "https://" not in text  # 不返回任何长期 URL

    def test_error_envelope(self, client, db):
        r = client.get("/api/v1/alerts/sensor:E-19990101-DEAD0009/media")
        assert set(r.json()) == {"code", "message", "data", "meta"}


# --------------------------------------------------------------------------
# 接口 B：媒体访问签名 URL
# --------------------------------------------------------------------------

class TestMediaAccess:
    def test_success_returns_signed_url(self, client, db, monkeypatch):
        aid = _make_sensor_alert(db)
        mid = _make_media(db, aid)
        fake = _monkey_storage(monkeypatch)
        r = client.post(f"/api/v1/media/{mid}/access")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "OK"
        assert body["data"]["access_url"].startswith("https://evb/")
        # 使用数据库 object_key + 默认 TTL 300，且为 GET(由 storage 保证，此处验证参数)
        assert fake.calls == [("evidence/m-1.mp4", 300)]
        assert body["data"]["expires_at"].endswith("+00:00")
        assert "T" in body["data"]["expires_at"]

    def test_media_missing_404(self, client, db, monkeypatch):
        _monkey_storage(monkeypatch)
        r = client.post("/api/v1/media/M-19990101-DEAD0001/access")
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"

    @pytest.mark.parametrize("status,upload_status", [
        ("pending", "success"), ("success", "failed"), ("failed-awaiting-retry", "success"),
        ("success", None), ("processing", "pending"),
    ])
    def test_not_fully_success_409_and_no_sign(self, client, db, monkeypatch, status, upload_status):
        aid = _make_sensor_alert(db)
        mid = _make_media(db, aid, status=status, upload_status=upload_status)
        fake = _monkey_storage(monkeypatch)
        r = client.post(f"/api/v1/media/{mid}/access")
        assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
        assert fake.calls == []  # 双 success 未同时满足时绝不调用签名

    def test_status_success_upload_success_both_required(self, client, db, monkeypatch):
        aid = _make_sensor_alert(db)
        mid = _make_media(db, aid, status="success", upload_status="success")
        fake = _monkey_storage(monkeypatch)
        r = client.post(f"/api/v1/media/{mid}/access")
        assert r.status_code == 200
        assert fake.calls == [("evidence/m-1.mp4", 300)]

    def test_client_cannot_override_object_key(self, client, db, monkeypatch):
        aid = _make_sensor_alert(db)
        mid = _make_media(db, aid, object_key="evidence/real-key.mp4")
        fake = _monkey_storage(monkeypatch)
        # 请求体/请求参数带任意定位字段，应被忽略，签名只来自 DB object_key
        r = client.post(f"/api/v1/media/{mid}/access?object_key=hacked&bucket=B&endpoint=E"
                        "&access_url=U")
        assert r.status_code == 200
        assert all(k == "evidence/real-key.mp4" for (k, _) in fake.calls)

    def test_storage_sign_error_503(self, client, db, monkeypatch):
        aid = _make_sensor_alert(db)
        mid = _make_media(db, aid)
        _monkey_storage(monkeypatch, error=StorageSignError("down"))
        r = client.post(f"/api/v1/media/{mid}/access")
        assert r.status_code == 503 and r.json()["code"] == "SERVICE_UNAVAILABLE"

    def test_evidence_bucket_not_configured_503(self, client, db, monkeypatch):
        from src.storage import EvidenceBucketNotConfiguredError
        aid = _make_sensor_alert(db)
        mid = _make_media(db, aid)
        _monkey_storage(monkeypatch, error=EvidenceBucketNotConfiguredError("no bucket"))
        r = client.post(f"/api/v1/media/{mid}/access")
        assert r.status_code == 503 and r.json()["code"] == "SERVICE_UNAVAILABLE"

    def test_value_error_500_not_503(self, client, db, monkeypatch):
        aid = _make_sensor_alert(db)
        mid = _make_media(db, aid, object_key=None)
        _monkey_storage(monkeypatch)
        # object_key 缺失在路由层已判 500（不落 OSS）；此处另验证 ValueError→500
        r = client.post(f"/api/v1/media/{mid}/access")
        assert r.status_code == 500 and r.json()["code"] == "INTERNAL_ERROR"

    def test_unexpected_exception_500(self, client, db, monkeypatch):
        aid = _make_sensor_alert(db)
        mid = _make_media(db, aid)
        _monkey_storage(monkeypatch, error=RuntimeError("boom"))
        r = client.post(f"/api/v1/media/{mid}/access")
        assert r.status_code == 500 and r.json()["code"] == "INTERNAL_ERROR"

    def test_signed_url_not_persisted(self, client, db, monkeypatch):
        aid = _make_sensor_alert(db)
        mid = _make_media(db, aid)
        _monkey_storage(monkeypatch)
        client.post(f"/api/v1/media/{mid}/access")
        rec = db.get_media_evidence(mid)
        # object_key 保持原值，未把签名 URL 写入数据库
        assert rec["object_key"] == "evidence/m-1.mp4"
        assert "https://evb/" not in rec["object_key"]