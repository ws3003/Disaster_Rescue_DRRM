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

    def test_case7_low_high_no_send_generates_env_alert(self):
        # env HIGH 且计划中无 send_alert(仅 save_event) → Agent 生成环境级 send_alert
        a = _MyAgent()
        actions = [_send_alert(action="save_event", priority="LOW")]  # 非 send_alert
        a._apply_alarm_fusion(actions, {"environment_severity": "HIGH"})
        snd = [x for x in actions
               if x.get("tool") == "communication_tool"
               and (x.get("args") or {}).get("action") == "send_alert"]
        self.assertEqual(len(snd), 1)
        self.assertIsNone(snd[0]["args"]["person_id"])
        self.assertEqual(snd[0]["args"]["severity"], "HIGH")
        self.assertEqual(snd[0]["args"]["title"], "环境高风险报警")

    def test_case8_low_low_no_send_not_generated(self):
        # env LOW 且无 send_alert → 不生成报警
        a = _MyAgent()
        actions = [_send_alert(action="save_event", priority="LOW")]
        a._apply_alarm_fusion(actions, {"environment_severity": "LOW"})
        self.assertFalse(any(
            x.get("tool") == "communication_tool"
            and (x.get("args") or {}).get("action") == "send_alert"
            for x in actions))

    @staticmethod
    def _run_empty_actions(env):
        a = _MyAgent()
        actions = []
        a._apply_alarm_fusion(actions, {"environment_severity": env})
        return actions

    @staticmethod
    def _snd(actions):
        return [x for x in actions
                if x.get("tool") == "communication_tool"
                and (x.get("args") or {}).get("action") == "send_alert"]

    def test_case9_env_high_no_person_no_alarm(self):
        # 无人员信息 + env HIGH → 不生成环境报警
        a = _MyAgent()
        actions = [{"tool": "database_tool", "args": {"action": "save_event"}}]
        a._apply_alarm_fusion(actions, {"environment_severity": "HIGH"})
        self.assertEqual(self._snd(actions), [])

    def test_case10_person_low_env_high_generates_env(self):
        # 有人员(LOW) + env HIGH + 无 person HIGH → 生成环境 send_alert
        a = _MyAgent()
        actions = [{"tool": "database_tool",
                    "args": {"action": "save_event", "person_id": 1, "priority": "LOW"}}]
        a._apply_alarm_fusion(actions, {"environment_severity": "HIGH"})
        snd = self._snd(actions)
        self.assertEqual(len(snd), 1)
        self.assertIsNone(snd[0]["args"]["person_id"])
        self.assertEqual(snd[0]["args"]["severity"], "HIGH")

    def test_case11_person_high_env_high_no_env_duplicate(self):
        # 已存在 person HIGH send_alert + env HIGH → 只保留人员报警, 不生成环境报警
        a = _MyAgent()
        actions = [_send_alert(priority="HIGH")]
        a._apply_alarm_fusion(actions, {"environment_severity": "HIGH"})
        snd = self._snd(actions)
        self.assertEqual(len(snd), 1)
        self.assertEqual(snd[0]["args"]["priority"], "HIGH")
        self.assertEqual(snd[0]["args"]["person_id"], 1)

    def test_case12_person_high_env_low_only_person(self):
        a = _MyAgent()
        actions = [_send_alert(priority="HIGH")]
        a._apply_alarm_fusion(actions, {"environment_severity": "LOW"})
        snd = self._snd(actions)
        self.assertEqual(len(snd), 1)
        self.assertEqual(snd[0]["args"]["priority"], "HIGH")

    def test_case13_env_low_person_high_severity_overridden(self):
        a = _MyAgent()
        actions = [_send_alert(priority="HIGH", severity="HIGH")]
        a._apply_alarm_fusion(actions, {"environment_severity": "LOW"})
        snd = self._snd(actions)
        self.assertEqual(snd[0]["args"]["severity"], "LOW")     # 环境档覆盖
        self.assertEqual(snd[0]["args"]["priority"], "HIGH")    # 人员档保留

    def test_case14_env_medium_person_high_severity_overridden(self):
        a = _MyAgent()
        actions = [_send_alert(priority="HIGH")]
        a._apply_alarm_fusion(actions, {"environment_severity": "MEDIUM"})
        snd = self._snd(actions)
        self.assertEqual(snd[0]["args"]["severity"], "MEDIUM")
        self.assertEqual(snd[0]["args"]["priority"], "HIGH")

    def test_case15_env_high_person_high_no_duplicate_env(self):
        a = _MyAgent()
        actions = [_send_alert(priority="HIGH", severity="HIGH")]
        a._apply_alarm_fusion(actions, {"environment_severity": "HIGH"})
        snd = self._snd(actions)
        self.assertEqual(len(snd), 1)                     # 仅人员报警, 不重复环境
        self.assertEqual(snd[0]["args"]["severity"], "HIGH")
        self.assertEqual(snd[0]["args"]["priority"], "HIGH")


if __name__ == "__main__":
    unittest.main(verbosity=2)