"""V4.3 报警融合门测试: person_priority 与 environment_severity 双维。"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.agent import Agent  # noqa: E402


def _send_alert(action="send_alert", priority="LOW", severity="LOW",
                person_id=1, **kw):
    return {"tool": "communication_tool",
            "args": {"action": action, "person_id": person_id,
                     "priority": priority, "severity": severity, "message": "m", **kw}}


class _MyAgent(Agent):
    def __init__(self):
        pass


def _run(person_priority, env):
    a = _MyAgent()
    actions = [_send_alert(priority=person_priority)]
    a._apply_alarm_fusion(actions, {"environment_severity": env})
    return actions


def _is_send(actions):
    a = actions[0]
    return (a.get("tool") == "communication_tool"
            and (a.get("args") or {}).get("action") == "send_alert")


class AlarmFusionTest(unittest.TestCase):
    def test_case1_low_low_downgrade(self):
        r = _run("LOW", "LOW")
        self.assertFalse(_is_send(r))
        self.assertTrue(r[0]["args"]["action"] == "save_event")
        self.assertFalse(r[0]["args"]["uncertain"])

    def test_case2_low_high_send(self):
        self.assertTrue(_is_send(_run("LOW", "HIGH")))

    def test_case3_high_low_send(self):
        self.assertTrue(_is_send(_run("HIGH", "LOW")))

    def test_case4_high_high_send(self):
        self.assertTrue(_is_send(_run("HIGH", "HIGH")))

    def test_case5_low_unknown_downgrade_uncertain(self):
        r = _run("LOW", "unknown")
        self.assertFalse(_is_send(r))
        self.assertTrue(r[0]["args"]["action"] == "save_event")
        self.assertTrue(r[0]["args"]["uncertain"])
        self.assertTrue(r[0]["tool"] == "database_tool")

    def test_case6_low_medium_downgrade(self):
        r = _run("LOW", "MEDIUM")
        self.assertFalse(_is_send(r))
        self.assertFalse(r[0]["args"]["uncertain"])


if __name__ == "__main__":
    unittest.main(verbosity=2)