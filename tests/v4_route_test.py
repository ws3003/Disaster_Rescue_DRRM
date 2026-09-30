"""V4.2 救援路线优先(rescue_route_priority)测试(全 mock)。"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.assistant import classify  # noqa: E402
from src.assistant import command_router  # noqa: E402
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


class RouteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "r.db")
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

    def _res(self):
        ctx = AssistantContext(db_path=self.db, snapshot_dir=self.snap,
                               vision_path=self.vision, map_provider=_FakeMap(),
                               location={"lat": 32.0603, "lng": 118.7969})
        return AssistantResponder(context=ctx)

    def test_intent(self):
        self.assertEqual(classify("最近医院路线"), "rescue_route_priority")
        self.assertEqual(classify("去医院怎么走"), "rescue_route_priority")
        self.assertEqual(classify("救援路线"), "rescue_route_priority")

    def test_router_info(self):
        cmd = command_router.classify("最近医院路线")
        self.assertEqual(cmd["intent"], "rescue_route_priority")
        self.assertEqual(cmd["category"], "info")

    def test_answer_route(self):
        out = self._res().answer("最近医院路线")
        self.assertEqual(out["intent"], "rescue_route_priority")
        a = out["answer"]
        self.assertIn("医院", a)
        self.assertIn("距离", a)
        self.assertIn("分钟", a)
        self.assertIn("信息辅助", a)
        self.assertIn("高风险", a)

    def test_no_agent_action(self):
        # 信息辅助: 不应产生 task / communication / send_alert
        res = self._res().answer("救援路线")
        self.assertNotIn("send_alert", str(res.get("sources", [])))


if __name__ == "__main__":
    unittest.main(verbosity=2)