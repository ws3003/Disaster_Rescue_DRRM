"""V4.3 报警融合门测试(新报警矩阵: person/environment 独立)。

|person|priority|env|result|
|无|-|HIGH|0|
|无|-|LOW|0|
|有|LOW|LOW|0|
|有|HIGH|LOW|person|
|有|LOW|HIGH|environment|
|有|HIGH|HIGH|person+environment|
"""
import collections
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.agent import Agent  # noqa: E402


def _person(priority="LOW", person_id=1, state="Fall"):
    return {"tool": "communication_tool",
            "args": {"action": "send_alert", "person_id": person_id,
                     "priority": priority, "state": state, "message": "m"}}


def _noperson():
    return {"tool": "database_tool", "args": {"action": "save_event"}}


class _MyAgent(Agent):
    def __init__(self):
        pass


def _snd(actions, alert_type=None):
    out = [x for x in actions
           if x.get("tool") == "communication_tool"
           and (x.get("args") or {}).get("action") == "send_alert"]
    if alert_type:
        out = [x for x in out if (x.get("args") or {}).get("alert_type") == alert_type]
    return out


class AlarmFusionMatrixTest(unittest.TestCase):
    def _run(self, actions, env):
        a = _MyAgent()
        a._apply_alarm_fusion(actions, {"environment_severity": env})
        return actions

    def test_no_person_env_high_zero(self):
        r = self._run([_noperson()], "HIGH")
        self.assertEqual(_snd(r), [])

    def test_no_person_env_low_zero(self):
        r = self._run([_noperson()], "LOW")
        self.assertEqual(_snd(r), [])

    def test_person_low_env_low_zero(self):
        r = self._run([_person("LOW")], "LOW")
        self.assertEqual(_snd(r), [])

    def test_person_low_env_high_env_only(self):
        r = self._run([_person("LOW")], "HIGH")
        self.assertEqual(len(_snd(r, "environment")), 1)
        self.assertEqual(_snd(r, "person"), [])

    def test_person_high_env_low_person_only(self):
        r = self._run([_person("HIGH")], "LOW")
        self.assertEqual(len(_snd(r, "person")), 1)
        self.assertEqual(_snd(r, "environment"), [])

    def test_person_high_env_high_both(self):
        r = self._run([_person("HIGH")], "HIGH")
        self.assertEqual(len(_snd(r, "person")), 1)
        self.assertEqual(len(_snd(r, "environment")), 1)


class AlarmFusionRulePathTest(unittest.TestCase):
    """process_trigger(规则路径) 也经融合门。"""

    @staticmethod
    def _rule_agent(actions):
        a = _MyAgent()
        plan = {"ok": True, "status": "planned", "task_id": "T1",
                "trigger_type": "HIGH_RISK", "person_id": 1,
                "priority": "HIGH", "risk_score": 0.9,
                "actions": actions, "created": datetime.now().isoformat()}
        a.planner = mock.MagicMock()
        a.planner.plan.return_value = plan
        a.memory = mock.MagicMock()
        a.executor = mock.MagicMock()
        a.stats = collections.defaultdict(int)
        return a

    @staticmethod
    def _trigger():
        return {"trigger_type": "HIGH_RISK", "person_id": 1,
                "state": "Fall", "priority": "HIGH", "risk_score": 0.9}

    def test_rule_person_high_env_low_person_alert(self):
        a = self._rule_agent([_person("HIGH")])
        a.process_trigger(self._trigger(), context={"environment_severity": "LOW"})
        plan = a.executor.execute.call_args[0][0]
        self.assertEqual(len(_snd(plan["actions"], "person")), 1)
        self.assertEqual(_snd(plan["actions"], "environment"), [])


class EnvironmentCooldownTest(unittest.TestCase):
    """环境报警独立 Memory key 冷却; 与人员报警互不干扰。"""

    @staticmethod
    def _agent():
        from src.agent.memory import Memory
        a = _MyAgent()
        a.memory = Memory(cooldown_minutes=30)
        return a

    def test_first_env_high_sends(self):
        a = self._agent()
        actions = [_person("HIGH")]
        a._apply_alarm_fusion(actions, {"environment_severity": "HIGH"})
        self.assertEqual(len(_snd(actions, "environment")), 1)

    def test_cooldown_suppresses_second_env(self):
        a = self._agent()
        acts1 = [_person("HIGH")]
        a._apply_alarm_fusion(acts1, {"environment_severity": "HIGH"})  # 首次 record
        acts2 = [_person("HIGH")]
        a._apply_alarm_fusion(acts2, {"environment_severity": "HIGH"})  # 冷却内
        self.assertEqual(_snd(acts2, "environment"), [])

    def test_env_memory_does_not_block_person(self):
        a = self._agent()
        a._apply_alarm_fusion([_person("HIGH")], {"environment_severity": "HIGH"})  # env 进冷却
        acts2 = [_person("HIGH")]
        a._apply_alarm_fusion(acts2, {"environment_severity": "HIGH"})
        # 第二次 env 被抑制, 但 person 走独立 key(未在此处被 record)仍可生成
        self.assertEqual(_snd(acts2, "environment"), [])
        self.assertEqual(len(_snd(acts2, "person")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)