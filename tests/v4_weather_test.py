"""V4.1 Phase 2 天气能力测试 (mock, 离线)。

验证:
  1. 天气意图识别
  2. weather_tool mock 返回
  3. assistant 回答包含天气
  4. assistant 不创建 task
  5. assistant 不调用 communication_tool
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
from src.tools.weather_tool import WeatherTool  # noqa: E402


class WeatherTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "w.db")
        self.snap_dir = os.path.join(self._tmp.name, "snapshots")
        os.makedirs(self.snap_dir, exist_ok=True)
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS tasks ("
                      "task_id TEXT PRIMARY KEY, trigger_event TEXT, plan_trace TEXT, "
                      "status TEXT, created TEXT, finished TEXT)")
        Path(self.snap_dir, "w_20260929.jpg").write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    # 1. 意图识别
    def test_weather_intent(self):
        self.assertEqual(classify("天气怎么样"), "weather_query")
        self.assertEqual(classify("现在下雨吗"), "weather_query")
        self.assertEqual(classify("温度多少"), "weather_query")

    # 2. weather_tool mock 返回
    def test_weather_tool_mock(self):
        t = WeatherTool()
        r = t.run(action="get_current", lat=31.2, lng=121.5)
        self.assertTrue(r["ok"])
        w = r["weather"]
        for k in ("condition", "temperature", "wind", "humidity", "timestamp"):
            self.assertIn(k, w)
        # 未接入真实 API, provider 为 mock
        self.assertEqual(r.get("provider"), "mock")

    # 3. assistant 回答包含天气 (有 mock 提供器时)
    def test_assistant_answer_contains_weather(self):
        ctx = self._mk_ctx(weather=True)
        r = AssistantResponder(context=ctx)
        out = r.answer("天气怎么样")
        self.assertEqual(out["intent"], "weather_query")
        self.assertTrue(any(k in out["answer"] for k in ("天气", "温度", "晴")))

    def test_assistant_no_weather_says_unavailable(self):
        ctx = self._mk_ctx(weather=False)
        r = AssistantResponder(context=ctx)
        out = r.answer("天气怎么样")
        self.assertIn("天气信息暂不可用", out["answer"])

    # 4. 不创建 task
    def test_assistant_does_not_generate_task(self):
        before = self._count_tasks()
        r = AssistantResponder(context=self._mk_ctx(weather=True))
        for q in ("天气怎么样", "现在下雨吗"):
            r.answer(q)
        self.assertEqual(before, self._count_tasks())

    # 5. 不调用 communication_tool / rescue_tool
    def test_assistant_does_not_call_tools(self):
        with patch("src.tools.communication_tool.CommunicationTool.run") as mcomm, \
             patch("src.tools.rescue_tool.RescueTool.run") as mrescue:
            r = AssistantResponder(context=self._mk_ctx(weather=True))
            r.answer("天气怎么样")
            mcomm.assert_not_called()
            mrescue.assert_not_called()

    # ---- helpers -------------------------------------------------
    def _mk_ctx(self, weather):
        from src.assistant.context import AssistantContext
        prov = (WeatherTool().get_current) if weather else None
        return AssistantContext(
            db_path=self.db,
            snapshot_dir=self.snap_dir,
            weather_provider=prov,
            location={"lat": 31.2, "lng": 121.5},
        )

    def _count_tasks(self):
        with sqlite3.connect(self.db) as c:
            return c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


if __name__ == "__main__":
    unittest.main(verbosity=2)