"""V4.1 Phase 3 地图/GIS 能力测试 (mock, 离线)。

验证:
  1. 地图意图识别 (location_query / route_query)
  2. map_tool mock 返回 (geocode / rescue_route)
  3. assistant 路线回答 / 位置回答
  4. assistant 不创建 task
  5. assistant 不调用 communication_tool
  6. 不回归 v4_pipeline / v4_assistant / v4_weather
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.assistant import classify  # noqa: E402
from src.assistant.responder import AssistantResponder  # noqa: E402
from src.tools.map_tool import MapTool  # noqa: E402


class MapTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "m.db")
        self.snap_dir = os.path.join(self._tmp.name, "snapshots")
        os.makedirs(self.snap_dir, exist_ok=True)
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS tasks ("
                      "task_id TEXT PRIMARY KEY, trigger_event TEXT, plan_trace TEXT, "
                      "status TEXT, created TEXT, finished TEXT)")
        Path(self.snap_dir, "m_20260929.jpg").write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    # 1. 意图识别
    def test_map_intent(self):
        self.assertEqual(classify("现场在哪里"), "location_query")
        self.assertEqual(classify("位置在哪"), "location_query")
        self.assertEqual(classify("路线怎么走"), "route_query")
        self.assertEqual(classify("怎么过去"), "route_query")

    # 2. map_tool mock 返回
    def test_map_tool_mock(self):
        t = MapTool(provider="mock")
        g = t.run(action="geocode", address="上海中心")
        self.assertTrue(g["ok"])
        for k in ("lat", "lng", "place"):
            self.assertIn(k, g["location"])
        r = t.run(action="rescue_route", origin="现场", destination="急救站")
        self.assertTrue(r["ok"])
        for k in ("distance_km", "duration_min", "summary"):
            self.assertIn(k, r["route"])

    # 3. assistant 位置 / 路线回答
    def test_assistant_location_answer(self):
        ctx = self._mk_ctx(map_=True)
        r = AssistantResponder(context=ctx)
        out = r.answer("现场在哪里")
        self.assertEqual(out["intent"], "location_query")
        self.assertIn("上海中心", out["answer"])

    def test_assistant_route_answer(self):
        ctx = self._mk_ctx(map_=True)
        r = AssistantResponder(context=ctx)
        out = r.answer("路线怎么走")
        self.assertEqual(out["intent"], "route_query")
        self.assertIn("公里", out["answer"])
        self.assertIn("分钟", out["answer"])

    def test_assistant_no_map_says_unavailable(self):
        ctx = self._mk_ctx(map_=False)
        r = AssistantResponder(context=ctx)
        loc = r.answer("现场在哪里")
        route = r.answer("路线怎么走")
        self.assertIn("未连接地图服务", loc["answer"])
        self.assertIn("未连接地图服务", route["answer"])

    # 4. 不创建 task
    def test_assistant_does_not_generate_task(self):
        before = self._count_tasks()
        r = AssistantResponder(context=self._mk_ctx(map_=True))
        for q in ("现场在哪里", "路线怎么走"):
            r.answer(q)
        self.assertEqual(before, self._count_tasks())

    # 5. 不调用 communication/rescue
    def test_assistant_does_not_call_tools(self):
        with patch("src.tools.communication_tool.CommunicationTool.run") as mcomm, \
             patch("src.tools.rescue_tool.RescueTool.run") as mrescue:
            r = AssistantResponder(context=self._mk_ctx(map_=True))
            r.answer("路线怎么走")
            mcomm.assert_not_called()
            mrescue.assert_not_called()

    # 6. 回归: 三个既有测试文件由外部 runner 一起执行
    # helpers
    def _mk_ctx(self, map_):
        from src.assistant.context import AssistantContext
        m = MapTool(provider="mock") if map_ else None
        return AssistantContext(
            db_path=self.db,
            snapshot_dir=self.snap_dir,
            map_provider=m,
            place="上海中心",
            route_origin="现场",
            route_destination="最近急救站",
        )

    def _count_tasks(self):
        with sqlite3.connect(self.db) as c:
            return c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


if __name__ == "__main__":
    unittest.main(verbosity=2)