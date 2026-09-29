"""V4.1 Phase7.2 视况人员上下文测试 (离线)。

验证:
  1. vision_snapshot write/read 原子写入
  2. intent 识别 scene_people
  3. context 读取 vision 人员
  4. responder 人员状态回答(人数/HIGH 提示)
  5. 不创建 task
  6. 不调用 communication_tool / rescue_tool
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

import src.vision_snapshot as vs  # noqa: E402
from src.assistant import classify  # noqa: E402
from src.assistant.responder import AssistantResponder  # noqa: E402


class VisionSnapshotTest(unittest.TestCase):
    def test_write_read_roundtrip(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "vision.json")
        r = vs.write_persons(
            [{"id": 1, "state": "Fall", "priority": "HIGH", "risk_score": 0.92},
             {"id": 2, "state": "Standing", "priority": "LOW"}],
            ts="2026-09-29T12:00:00", path=p)
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 2)
        data = vs.read_latest(p)
        self.assertEqual(len(data["persons"]), 2)
        self.assertEqual(data["persons"][0]["state"], "Fall")

    def test_read_missing_returns_empty(self):
        d = tempfile.mkdtemp()
        self.assertEqual(vs.read_latest(os.path.join(d, "nope.json")), {})


class VisionAssistantTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "v.db")
        self.snap_dir = os.path.join(self._tmp.name, "snapshots")
        self.vision_path = os.path.join(self._tmp.name, "vision.json")
        os.makedirs(self.snap_dir, exist_ok=True)
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS tasks ("
                      "task_id TEXT PRIMARY KEY, trigger_event TEXT, plan_trace TEXT, "
                      "status TEXT, created TEXT, finished TEXT)")
        vs.write_persons(
            [{"id": 1, "state": "Fall", "priority": "HIGH", "risk_score": 0.92},
             {"id": 2, "state": "Standing", "priority": "LOW"}],
            ts="2026-09-29T12:00:00", path=self.vision_path)

    def tearDown(self):
        self._tmp.cleanup()

    def _mk_ctx(self):
        from src.assistant.context import AssistantContext
        return AssistantContext(db_path=self.db, snapshot_dir=self.snap_dir,
                                vision_path=self.vision_path)

    def test_people_intent(self):
        self.assertEqual(classify("现场有几个人"), "scene_people")
        self.assertEqual(classify("谁倒了"), "scene_people")

    def test_context_reads_persons(self):
        ctx = self._mk_ctx()
        out = ctx.collect()
        self.assertEqual(len(out["vision"]["persons"]), 2)

    def test_responder_people_answer(self):
        r = AssistantResponder(context=self._mk_ctx())
        out = r.answer("现场现在有几个人")
        self.assertEqual(out["intent"], "scene_people")
        self.assertIn("2 人", out["answer"])
        self.assertIn("HIGH", out["answer"])

    def test_no_task_generated(self):
        before = self._count_tasks()
        r = AssistantResponder(context=self._mk_ctx())
        r.answer("现场有几个人")
        self.assertEqual(before, self._count_tasks())

    def test_does_not_call_tools(self):
        with patch("src.tools.communication_tool.CommunicationTool.run") as mcomm, \
             patch("src.tools.rescue_tool.RescueTool.run") as mrescue:
            r = AssistantResponder(context=self._mk_ctx())
            r.answer("现场有几个人")
            mcomm.assert_not_called()
            mrescue.assert_not_called()

    def _count_tasks(self):
        with sqlite3.connect(self.db) as c:
            return c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


if __name__ == "__main__":
    unittest.main(verbosity=2)