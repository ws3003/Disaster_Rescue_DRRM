"""V4.2 /api/v1/assistant/ask API 验证测试(离线)。

通过 patch server.api._build_responder_with_weather 注入确定性 responder(隔离数据),
验证 HTTP 契约: ok=True, intent=rescue_route_priority, answer 含 医院/路线。
不修改任何业务代码。
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import api  # noqa: E402
from src.assistant.responder import AssistantResponder  # noqa: E402
from src.assistant.context import AssistantContext  # noqa: E402


class _FakeMap:
    def nearby(self, keyword=None, location=None):
        return {"ok": True, "poi": [{"name": "南京市中心医院", "type": "医院",
                                     "distance": "800米", "address": "x"}]}

    def geocode(self, place=None):
        return {"ok": True, "location": {"lat": 32.0603, "lng": 118.7969, "place": place}}

    def rescue_route(self, origin=None, destination=None):
        return {"ok": True, "route": {"distance_km": 0.64, "duration_min": 3,
                                      "summary": "mock route"}}


class AssistantAskApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "a.db")
        self.snap = os.path.join(self._tmp.name, "s"); os.makedirs(self.snap)
        self.vision = os.path.join(self._tmp.name, "v.json")
        Path(self.vision).write_text(
            '{"ts":"2026","persons":[{"id":1,"state":"Fall","risk":"HIGH","risk_score":0.92}]}',
            encoding="utf-8")
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, "
                      "trigger_event TEXT, plan_trace TEXT, status TEXT, "
                      "created TEXT, finished TEXT)")

    def tearDown(self):
        self._tmp.cleanup()

    def _ask(self, question):
        ctx = AssistantContext(db_path=self.db, snapshot_dir=self.snap,
                               vision_path=self.vision, map_provider=_FakeMap(),
                               location={"lat": 32.0603, "lng": 118.7969})
        responder = AssistantResponder(context=ctx)
        # 让 HTTP 端点使用该确定性 responder(仅测试隔离, 不改业务代码)
        with patch("server.api._build_responder_with_weather", return_value=responder):
            c = TestClient(api.app)
            return c.post("/api/v1/assistant/ask",
                          json={"question": question}).json()

    def test_ask_rescue_route(self):
        body = self._ask("最近医院路线怎么走")
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("intent"), "rescue_route_priority")
        self.assertIn("医院", body.get("answer", ""))
        self.assertIn("路线", body.get("answer", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)