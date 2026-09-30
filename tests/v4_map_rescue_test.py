"""V4.2 附近救援资源语义回答测试(离线, 全 mock)。

覆盖:
  1. intent: "附近哪里可以救援" → nearby_rescue_resource
  2. responder 结合 vision 高风险人员 + mock POI(医院/消防/避难) 输出
  3. 回答含 救援资源 / 医院 / 高风险人员 / 辅助提示
"""
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
    """mock map provider: 按关键词返回 POI。"""
    def nearby(self, keyword=None, location=None):
        return {"ok": True, "poi": [{"name": f"模拟{keyword}点", "type": keyword,
                                     "distance": "800米", "address": "x"}]}

    def rescue_route(self, **kwargs):
        return {"ok": True, "route": {"distance_km": 1, "duration_min": 1, "summary": "x"}}


class MapRescueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "mr.db")
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
                               vision_path=self.vision, map_provider=_FakeMap())
        return AssistantResponder(context=ctx)

    def test_intent(self):
        self.assertEqual(classify("附近哪里可以救援"), "nearby_rescue_resource")
        self.assertEqual(classify("附近医院在哪里"), "nearby_rescue_resource")
        self.assertEqual(classify("最近避难点"), "nearby_rescue_resource")

    def test_router_info(self):
        cmd = command_router.classify("附近哪里可以救援")
        self.assertEqual(cmd["intent"], "nearby_rescue_resource")
        self.assertEqual(cmd["category"], "info")

    def test_answer_includes_rescue_context(self):
        out = self._res().answer("附近哪里可以救援")
        self.assertEqual(out["intent"], "nearby_rescue_resource")
        a = out["answer"]
        self.assertIn("救援资源", a)
        self.assertIn("医院", a)
        self.assertIn("消防站", a)
        self.assertIn("高风险", a)
        self.assertIn("信息辅助", a)
        return a