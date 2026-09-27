"""P0-16 Vision Tool 单元测试 (本地 temp 路径, 不触碰 data/)。

覆盖: update_snapshot(person_events) 往返 / 多人 / recent JSONL /
get_scene 时间窗口 / 无数据兜底 / 坏 JSONL 不崩溃 / ToolRegistry 注册 /
Executor→VisionTool 调用链。
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agent.executor import Executor  # noqa: E402
from src.tools.base import ToolRegistry  # noqa: E402
from src.tools.vision_tool import VisionTool  # noqa: E402


class BaseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.snapshot_path = os.path.join(self._tmp.name, "latest.json")
        self.recent_path = os.path.join(self._tmp.name, "recent.jsonl")
        self.tool = VisionTool(snapshot_path=self.snapshot_path,
                               recent_path=self.recent_path)

    def tearDown(self):
        self._tmp.cleanup()

    def events(self, n=3):
        return [
            {"person_id": i,
             "state": "Fall" if i == 1 else ("Sitting" if i == 2 else "Standing"),
             "risk_score": 0.6 + 0.1 * i,
             "priority": "HIGH" if i == 1 else "LOW"}
            for i in range(1, n + 1)
        ]


class UpdateGetRoundtripTest(BaseCase):
    def test_roundtrip_and_null_missing_fields(self):
        r = self.tool.run(action="update_snapshot", person_events=self.events(1))
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 1)

        g = self.tool.run(action="get_snapshot", person_id=1)
        self.assertTrue(g["ok"])
        self.assertEqual(g["person_id"], 1)
        self.assertEqual(g["state"], "Fall")
        self.assertEqual(g["risk_score"], 0.6 + 0.1 * 1)
        self.assertEqual(g["priority"], "HIGH")
        # 缺失字段必须为 null, 非伪造
        self.assertIsNone(g["state_conf"])
        self.assertIsNone(g["bbox"])
        self.assertIsNone(g["frame_idx"])
        self.assertIsNone(g["annotated_frame_b64"])

    def test_multiple_persons(self):
        self.tool.run(action="update_snapshot", person_events=self.events(3))
        g1 = self.tool.run(action="get_snapshot", person_id=1)
        g3 = self.tool.run(action="get_snapshot", person_id=3)
        self.assertTrue(g1["ok"])
        self.assertTrue(g3["ok"])
        self.assertEqual(g1["person_id"], 1)
        self.assertEqual(g1["state"], "Fall")
        self.assertEqual(g3["person_id"], 3)
        self.assertEqual(g3["state"], "Standing")

    def test_get_snapshot_missing_person(self):
        self.tool.run(action="update_snapshot", person_events=self.events(2))
        g = self.tool.run(action="get_snapshot", person_id=99)
        self.assertFalse(g["ok"])
        self.assertIn("not found", g["error"])


class RecentAndSceneTest(BaseCase):
    def test_recent_jsonl_appended(self):
        self.tool.run(action="update_snapshot", person_events=self.events(3))
        self.tool.run(action="update_snapshot", person_events=self.events(2))
        self.assertTrue(os.path.exists(self.recent_path))
        lines = [json.loads(l) for l in open(self.recent_path, encoding="utf-8")
                 if l.strip()]
        self.assertEqual(len(lines), 2)

    def test_get_scene_window_filter(self):
        now = datetime.now()
        for off, n in ((60, 1), (5, 2)):
            ts = (now - timedelta(seconds=off)).isoformat(timespec="seconds")
            self.tool._append_recent(
                {"timestamp": ts, "persons": [{"person_id": n, "state": "x"}]})
        scene = self.tool.run(action="get_scene", window_sec=10)
        self.assertTrue(scene["ok"])
        self.assertEqual(scene["window_sec"], 10)
        # 只有 5 秒前那帧在窗口内
        self.assertEqual(len(scene["frames"]), 1)
        self.assertEqual(scene["frames"][0]["persons"][0]["person_id"], 2)

    def test_bad_jsonl_line_does_not_crash(self):
        now = datetime.now()
        def line_ok(off, pid):
            return json.dumps(
                {"timestamp": (now - timedelta(seconds=off)).isoformat(timespec="seconds"),
                 "persons": [{"person_id": pid, "state": "Fall"}]})
        with open(self.recent_path, "w", encoding="utf-8") as fh:
            fh.write(line_ok(2, 1) + "\n")
            fh.write("{this is : not json}\n")
            fh.write(line_ok(3, 2) + "\n")
        scene = self.tool.run(action="get_scene", window_sec=10)
        self.assertTrue(scene["ok"])
        self.assertEqual(len(scene["frames"]), 2)  # 坏行被跳过


class NoDataTest(BaseCase):
    def test_no_snapshot_no_exception(self):
        g = self.tool.run(action="get_snapshot")   # 无 latest 文件
        self.assertTrue(g["ok"])
        self.assertIsNone(g["snapshot"])

    def test_miss_require_input(self):
        r = self.tool.run(action="update_snapshot")
        self.assertFalse(r["ok"])


class RegistrationAndChainTest(BaseCase):
    def test_registry_has_vision_tool(self):
        reg = ToolRegistry()
        reg.register(self.tool)
        self.assertTrue(reg.has("vision_tool"))

    def test_executor_can_call_vision_get_snapshot(self):
        # 先写一份 snapshot
        self.tool.run(action="update_snapshot", person_events=self.events(1))
        reg = ToolRegistry()
        reg.register(VisionTool(snapshot_path=self.snapshot_path,
                                recent_path=self.recent_path))
        db = os.path.join(self._tmp.name, "drrm.db")
        ex = Executor(db_path=db, registry=reg)
        plan = {
            "task_id": "T-VISION-1",
            "trigger_type": "VISION",
            "trigger_event": "VISION",
            "actions": [{"tool": "vision_tool", "action": "get_snapshot",
                         "input": {"person_id": 1}}],
            "created": "2026-01-01T00:00:00",
        }
        res = ex.execute(plan)
        first = res.get("results", [{}])[0]
        self.assertTrue(first.get("ok"))
        self.assertEqual(first.get("action"), "get_snapshot")
        self.assertEqual(first.get("person_id"), 1)
        self.assertEqual(first.get("state"), "Fall")


if __name__ == "__main__":
    unittest.main(verbosity=2)