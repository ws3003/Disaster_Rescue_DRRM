"""V4.3 语音智能分流测试: scene/risk/agent_reasoning/None(action) / 资源查询。

验证:
  - scene   → scene_context(现场查询)
  - risk    → risk_analysis(风险分析, 不建任务)
  - agent   → agent_reasoning(建议/原因, 不创建任务, 无 task_id)
  - None    → command_router(动作/资源/天气走既有命令路由)
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 离线测试: 用 mock TTS, 避免 edge-tts 网络请求
os.environ.setdefault("TTS_PROVIDER", "mock")
os.environ.setdefault("LLM_PROVIDER", "mock")

from src.assistant.voice_decision_router import VoiceDecisionRouter  # noqa: E402
from src.assistant import command_router  # noqa: E402


class RouterTest(unittest.TestCase):
    def _route(self, text):
        return VoiceDecisionRouter.route(text)

    def test_scene(self):
        for q in ("现场有没有伤员", "最近有没有救援任务", "当前多少人", "现场怎么样"):
            self.assertEqual(self._route(q), "scene", q)

    def test_risk(self):
        for q in ("现在危险吗", "风险怎么样", "最高风险是谁", "情况严重吗"):
            self.assertEqual(self._route(q), "risk", q)

    def test_agent(self):
        for q in ("怎么办", "怎么处理", "下一步怎么办", "为什么报警"):
            self.assertEqual(self._route(q), "agent", q)

    def test_none(self):
        for q in ("天气怎么样", "附近有没有医院", "附近有没有消防站", "发送报警"):
            self.assertIsNone(self._route(q), q)


class CommandRouterTest(unittest.TestCase):
    def test_nearby_resource_keywords(self):
        for q in ("附近有没有医院", "附近有没有消防站", "附近有没有消防",
                  "附近有没有避难所", "附近有没有救援点", "附近有没有救援资源"):
            self.assertEqual(command_router.classify(q)["intent"],
                             "nearby_rescue_resource", q)

    def test_weather_preserved(self):
        self.assertEqual(command_router.classify("天气怎么样")["intent"], "weather")

    def test_send_alert_action_dangerous(self):
        c = command_router.classify("发送报警")
        self.assertEqual(c["intent"], "send_alert")
        self.assertEqual(c["category"], "action")
        self.assertTrue(c["require_confirm"])


class HandleTest(unittest.TestCase):
    def _make(self):
        from src.assistant.voice import VoiceAssistant
        return VoiceAssistant()

    def test_scene_routes_scene_context(self):
        out = self._make().handle(text="现场有没有伤员")
        self.assertEqual(out["intent"], "scene_context")

    def test_risk_routes_risk_analysis(self):
        out = self._make().handle(text="现在危险吗")
        self.assertEqual(out["intent"], "risk_analysis")

    def test_risk_is_readonly_no_save_task(self):
        # risk 查询只读: bridge(Agent/Executor) 不应当被调用 → 不触发 save_task
        v = self._make()
        bridge_calls = []
        v._bridge.handle = lambda intent, confirmed=False: (  # type: ignore[assignment]
            bridge_calls.append(intent) or {"ok": True})
        out = v.handle(text="现在危险吗")
        self.assertEqual(out["intent"], "risk_analysis")
        self.assertEqual(bridge_calls, [])      # 未走 AgentBridge
        self.assertNotIn("task_id", str(out))   # 无任务
        self.assertFalse(out["require_confirm"])
        self.assertTrue(out["confirmed"])

    def test_agent_reasoning_creates_no_task(self):
        # "怎么办" → agent_reasoning; 不创建任务 → 返回中不应有 task_id
        out = self._make().handle(text="怎么办")
        self.assertEqual(out["intent"], "agent_reasoning")
        s = str(out)
        self.assertNotIn("task_id", s)
        self.assertFalse(out["require_confirm"])

    def test_why_alarm_is_reasoning(self):
        out = self._make().handle(text="为什么报警")
        self.assertEqual(out["intent"], "agent_reasoning")

    def test_send_alert_still_action(self):
        # 发送报警 不含 agent 关键词 → None → command_router action, 需二次确认
        out = self._make().handle(text="发送报警")
        self.assertEqual(out["intent"], "send_alert")
        self.assertTrue(out["require_confirm"])


if __name__ == "__main__":
    unittest.main(verbosity=2)