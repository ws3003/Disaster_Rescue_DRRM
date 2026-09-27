"""P0-19/21/22 生产输入接入 单元测试。

覆盖: person_events→frame_record / 多人 / 空 / Vision adapter 接入 /
main --agent 参数识别 / Agent 生产输入 smoke(真实链)。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agent import input_adapter  # noqa: E402
from src.tools.vision_adapter import to_snapshot  # noqa: E402
from src.tools.vision_tool import VisionTool  # noqa: E402


class AdapterTest(unittest.TestCase):
    def test_person_events_to_frame_record(self):
        fr = input_adapter.person_events_to_frame_record(
            [{"person_id": 1, "state": "Fall", "risk_score": 0.6077, "priority": "HIGH"}])
        self.assertIn("timestamp", fr)
        self.assertEqual(fr["persons"][0]["person_id"], 1)
        self.assertEqual(fr["persons"][0]["state"], "Fall")
        self.assertEqual(fr["persons"][0]["risk_score"], 0.6077)
        self.assertEqual(fr["persons"][0]["priority"], "HIGH")

    def test_multiple_persons(self):
        events = [{"person_id": i, "state": s, "risk_score": 0.1 * i, "priority": p}
                  for i, s, p in [(1, "Fall", "HIGH"), (2, "Sitting", "LOW"),
                                  (3, "Standing", "LOW")]]
        fr = input_adapter.person_events_to_frame_record(events)
        self.assertEqual(len(fr["persons"]), 3)
        self.assertEqual([p["person_id"] for p in fr["persons"]], [1, 2, 3])

    def test_empty(self):
        fr = input_adapter.person_events_to_frame_record([])
        self.assertEqual(fr["persons"], [])
        self.assertIn("timestamp", fr)
        fr2 = input_adapter.person_events_to_frame_record(None)
        self.assertEqual(fr2["persons"], [])

    def test_missing_person_id_dropped(self):
        fr = input_adapter.person_events_to_frame_record(
            [{"state": "Fall"}, {"person_id": 5, "state": "Sitting"}])
        self.assertEqual(len(fr["persons"]), 1)
        self.assertEqual(fr["persons"][0]["person_id"], 5)


class VisionFeedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.snap = os.path.join(self._tmp.name, "latest_vision.json")
        self.recent = os.path.join(self._tmp.name, "vision_snapshots.jsonl")
        self.tool = VisionTool(snapshot_path=self.snap, recent_path=self.recent)

    def tearDown(self):
        self._tmp.cleanup()

    def test_person_events_to_vision_snapshot_files(self):
        events = [{"person_id": 1, "state": "Fall", "risk_score": 0.6077, "priority": "HIGH"}]
        snapshot = to_snapshot(events)
        r = self.tool.run(action="update_snapshot", snapshot=snapshot)
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.exists(self.snap))
        self.assertTrue(os.path.exists(self.recent))
        latest = json.load(open(self.snap, encoding="utf-8"))
        self.assertEqual(latest["persons"][0]["state"], "Fall")


class MainArgTest(unittest.TestCase):
    def test_agent_flag_recognized_via_help(self):
        out = subprocess.run(
            [sys.executable, os.path.join(ROOT, "src", "main.py"), "--agent", "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=ROOT)
        self.assertEqual(out.returncode, 0)
        self.assertIn("--agent", out.stdout)


class AgentSmokeTest(unittest.TestCase):
    def test_agent_process_frame_runs_real_chain(self):
        from src.agent.agent import Agent
        agent = Agent()
        frame_record = {
            "timestamp": "2026-01-01T00:00:00",
            "persons": [
                {"person_id": 1, "state": "Fall", "risk_score": 0.92, "priority": "HIGH"},
            ],
        }
        results = agent.process_frame(frame_record)
        self.assertIsInstance(results, list)
        self.assertGreaterEqual(len(results), 1)
        self.assertTrue(any(isinstance(r, dict) for r in results))


if __name__ == "__main__":
    unittest.main(verbosity=2)