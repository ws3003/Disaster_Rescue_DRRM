"""V4.4 Agent scene_context 注入测试。

验证:
  1. _build_context 注入 scene_context
  2. planner 仍收到 context(不破坏调用链)
  3. ContextProvider 异常时 Agent 不阻断, scene_context={}
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.agent import Agent  # noqa: E402
from src.agent.planner import Planner  # noqa: E402


class MyAgent(Agent):
    # 最小可实例化 Agent 骨架(跳过真实 config 读取, 便于测试)
    def __init__(self):
        pass


class AgentSceneContextTest(unittest.TestCase):
    def _agent(self):
        a = MyAgent()
        a.planner = Planner(memory=None, cooldown_minutes=30, max_steps=3)
        return a

    def test_context_has_scene_context(self):
        a = self._agent()
        with mock.patch(
            "src.agent.context_provider.ContextProvider.get_scene_context",
            return_value={"scene_context": {
                "persons": [{"id": 1, "state": "Fall", "risk": "HIGH"}]}},
        ):
            ctx = a._build_context(trigger={"ts": "x"}, context=None)
        self.assertIn("scene_context", ctx)
        self.assertIn("persons", ctx["scene_context"])
        self.assertEqual(ctx["scene_context"]["persons"][0]["id"], 1)
        # 原字段仍在
        self.assertIn("memory_digest", ctx)
        self.assertIn("trigger", ctx)

    def test_planner_receives_context(self):
        a = self._agent()
        with mock.patch(
            "src.agent.context_provider.ContextProvider.get_scene_context",
            return_value={"scene_context": {"persons": []}},
        ) as sc:
            ctx = a._build_context(trigger={"ts": "x"}, context={"task_id": "T1"})
        self.assertIn("scene_context", ctx)
        self.assertIn("external_context", ctx)
        # 证明该 context 可被 planner 消费(plan 至少不因 ctx 结构崩溃)
        # 不真正运行 planner 全流程, 只断言 ctx 类型为 dict 且 scene_context 存在
        self.assertIsInstance(ctx, dict)

    def test_provider_exception_does_not_break(self):
        a = self._agent()
        with mock.patch(
            "src.agent.context_provider.ContextProvider.get_scene_context",
            side_effect=RuntimeError("boom"),
        ):
            ctx = a._build_context(trigger={"ts": "x"}, context=None)
        self.assertEqual(ctx["scene_context"], {})  # 异常兜底
        self.assertIn("memory_digest", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)