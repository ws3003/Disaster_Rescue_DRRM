"""V4.2 环境救援适宜性评估测试(离线, 全 mock)。

覆盖:
  1. intent: "环境适合救援吗" → env_suitability
  2. 无不利因素 → 较适宜建议
  3. 有天气预警 + 高风险人员 → 提示不利因素
  4. command_router 将其归为 info(不触发 Agent)
"""

import os
import re
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


def _mk_context(db, snap, vision, weather=None, warning=None):
    def _weather_provider(lat, lng):
        return weather or {"ok": False}
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, "
                  "trigger_event TEXT, plan_trace TEXT, status TEXT, "
                  "created TEXT, finished TEXT)")
    Path(vision).write_text("{}", encoding="utf-8")
    return AssistantContext(db_path=db, snapshot_dir=snap, vision_path=vision,
                            weather_provider=_weather_provider,
                            warning_provider=(lambda lat, lng: warning or {"ok": True, "warning": []}))


class EnvSuitabilityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "e.db")
        self.snap = os.path.join(self._tmp.name, "s"); os.makedirs(self.snap)
        self.vision = os.path.join(self._tmp.name, "v.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _wh(self, vision_persons=None, weather=None, warning=None):
        ctx = _mk_context(self.db, self.snap, self.vision, weather=weather, warning=warning)
        # 覆盖读取的 vision 文件
        import json
        Path(self.vision).write_text(
            json.dumps({"ts": "2026", "persons": vision_persons or []}), encoding="utf-8")
        return AssistantResponder(context=ctx)

    # 1. intent
    def test_intent_env_suitability(self):
        self.assertEqual(classify("环境适合救援吗"), "env_suitability")
        self.assertEqual(classify("适不适合救援"), "env_suitability")

    # 2. 无不利 → 较适宜
    def test_suitability_ok_when_benign(self):
        r = self._wh(vision_persons=[],
                     weather={"ok": True, "weather": {"condition": "晴", "temperature": 20}})
        out = r.answer("环境适合救援吗")
        self.assertEqual(out["intent"], "env_suitability")
        self.assertIn("较适宜", out["answer"])

    # 3. 预警+高风险 → 提示不利因素
    def test_hazards_flag(self):
        import json as j
        r = self._wh(
            vision_persons=[{"id": 1, "state": "Fall", "risk": "HIGH"}],
            weather={"ok": True, "weather": {"condition": "大雨"}},
            warning={"ok": True, "warning": [{"type": "暴雨", "level": "黄色"}]})
        out = r.answer("环境适合救援吗")
        self.assertIn("不利因素", out["answer"])
        self.assertIn("天气", out["answer"])

    # 4. command_router: env 属 info
    def test_router_info(self):
        cmd = command_router.classify("环境适合救援吗")
        self.assertEqual(cmd["category"], "info")
        self.assertEqual(cmd["intent"], "env_suitability")


if __name__ == "__main__":
    unittest.main(verbosity=2)