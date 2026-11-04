"""Isolated ASGI evidence, not real WeChat/OSS/Pi or production acceptance."""
import faulthandler
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from unittest.mock import patch

OUT = Path(__file__).resolve().parent
ROOT = OUT.parent.parent
REPO = ROOT / "Disaster_Rescue_DRRM_B"
sys.path.insert(0, str(REPO))
faulthandler.dump_traceback_later(40, exit=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from server import auth, media, s4
from src.database.db import Database
from src.tools.database_tool import DatabaseTool


class TestWechat:
    def __init__(self, **kwargs):
        pass

    def code2session(self, code):
        assert code == "SOFTWARE-TEST-NOT-REAL-WECHAT-CODE"
        return type("Result", (), {"openid": "AUDIT-TEST-OPENID", "session_key": "AUDIT-TEST-KEY"})()


runtime = Path(tempfile.mkdtemp(prefix="auth_boundary_TEST_", dir=OUT.parent))
db = Database(db_path=str(runtime / "TEST_ONLY.db"))
alert_id = db.save_sensor_alert(type="TEST_ONLY", temperature=52, threshold=50)
app = FastAPI()
app.include_router(s4.router)
app.include_router(auth.router)
media.install(app)
app.include_router(media.router)
app.dependency_overrides[s4.get_db] = lambda: db
service_token = secrets.token_urlsafe(32)
checks = []


def check(client, label, method, path, token, expected):
    headers = {"Authorization": "Bearer " + token} if token else {}
    response = client.request(method, path, headers=headers)
    body = response.json()
    # Do not persist auth headers, token values, hashes or login response bodies.
    row = {"credential_kind": label, "method": method, "path": path,
           "status": response.status_code, "code": body.get("code"),
           "expected_status": expected, "matches_expected": response.status_code == expected}
    checks.append(row)
    assert row["matches_expected"], row


with patch.dict(os.environ, {"S4_API_TOKEN": service_token}), patch.object(auth, "WechatClient", TestWechat):
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/login", json={"code": "SOFTWARE-TEST-NOT-REAL-WECHAT-CODE"})
        assert login.status_code == 200
        session_token = login.json()["data"]["session_token"]
        assert session_token != service_token
        paths = ["/api/v1/system/status", "/api/v1/alerts", f"/api/v1/alerts/sensor:{alert_id}",
                 f"/api/v1/alerts/sensor:{alert_id}/related", "/api/v1/environment/readings"]
        for label, token, s4_status, user_status in [
            ("session", session_token, 401, 200),
            ("service", service_token, 200, 401),
            ("anonymous", None, 401, 401),
        ]:
            for path in paths:
                check(client, label, "GET", path, token, s4_status)
            check(client, label, "GET", "/api/v1/auth/me", token, user_status)
            check(client, label, "GET", f"/api/v1/alerts/sensor:{alert_id}/media", token, user_status)
            check(client, label, "POST", "/api/v1/media/M-TEST-NONEXISTENT/access", token,
                  404 if label == "session" else 401)
        logout = client.post("/api/v1/auth/logout", headers={"Authorization": "Bearer " + session_token})
        assert logout.status_code == 200
        check(client, "revoked_session", "GET", "/api/v1/auth/me", session_token, 401)

# Static wiring independently exercised through the actual production tool.
# This is not a full Agent/vision runtime test.
tool = DatabaseTool(db_path=str(runtime / "TOOL_TEST_ONLY.db"))
saved = tool.run(action="save_event", type="high_risk_alert", severity="HIGH", person_id=1,
                 payload={"state": "Help-Waving", "priority": "HIGH", "risk_score": 1,
                          "timestamp": "2026-11-03T10:00:00+08:00"})
assert saved["ok"] is True, saved
event_id = saved["event_id"]
outbox = tool.db.list_outbox()
producer_result = {"scope": "DatabaseTool_only_not_full_Agent", "event_saved": tool.db.get_event(event_id) is not None,
                   "outbox_record_types": [r["record_type"] for r in outbox],
                   "outbox_states": [r["status"] for r in outbox]}
assert producer_result["event_saved"]
assert producer_result["outbox_record_types"] == ["person_event"]

result = {"scope": "SOFTWARE_ASGI_TEST_WECHAT_EXCHANGE_STUBBED_NO_REAL_WECHAT_OSS_PI",
          "backend_head": "96ac8ba55b49a31b3296853d46af28df156bb0d8",
          "checks": checks, "checks_passed": len(checks), "producer_probe": producer_result,
          "credentials_persisted": False,
          "warning": "Raw temporary databases are outside this evidence folder and excluded from delivery."}
(OUT / "auth-boundary-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"{len(checks)} auth boundary assertions passed; actual DatabaseTool saved an event and populated person_event Outbox.")
