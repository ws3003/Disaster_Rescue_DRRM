"""V4.4 ingest_llm_output 场景上下文注入测试。

验证:
  1. 调用 ingest_llm_output 时, planner 收到的 context 含 scene_context
  2. ContextProvider 异常时 Agent 仍运行, scene_context={}
  3. process_frame 路径不受影响(规则路径仍用 _build_context)
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

_SC = {"scene_context": {"persons": [{"id": 1, "state": "Fall", "risk": "HIGH"}],
                         "risk_summary": {"highest": "HIGH", "count": 1},
                         "events": [], "tasks": []}}


class _FakePlanner:
    """只记录 plan_from_llm_output 收到的 context, 不真正执行。"""

    def __init__(self):
        self.called_context = None

    def plan_from_llm_output(self, llm_output, context=None):
        self.called_context = context
        return {"ok": True, "actions": [], "status": "done"}


class _MyAgent(Agent):
    def __init__(self):
        pass


class AgentLlmSceneContextTest(unittest.TestCase):
    def _make(self):
        a = _MyAgent()
        a.planner = _FakePlanner()
        a.memory = mock.MagicMock()
        a.executor = mock.MagicMock()
        return a

    def _empty_llm_output(self):
        return {"event_summary": "test", "risk_explanations": [],
                "rescue_suggestions": [], "task_proposals": []}

    def test_planner_receives_scene_context(self):
        a = self._make()
        with mock.patch("src.agent.context_provider.ContextProvider.get_scene_context",
                        return_value=_SC):
            a.ingest_llm_output(self._empty_llm_output(),
                                context={"image_url": "http://x", "task_id": "T1"})
        ctx = a.planner.called_context
        self.assertIn("scene_context", ctx)
        self.assertEqual(ctx["scene_context"]["persons"][0]["id"], 1)
        # 原字段保留
        self.assertEqual(ctx["image_url"], "http://x")
        self.assertEqual(ctx["task_id"], "T1")

    def test_provider_exception_fallback_empty(self):
        a = self._make()
        with mock.patch("src.agent.context_provider.ContextProvider.get_scene_context",
                        side_effect=RuntimeError("boom")):
            a.ingest_llm_output(self._empty_llm_output(),
                                context={"image_url": "http://x", "task_id": "T1"})
        ctx = a.planner.called_context
        self.assertEqual(ctx["scene_context"], {})  # 兜底
        # 且不阻断: 仍走完流程
        self.assertIsNotNone(ctx)

    def test_process_frame_uses_build_context(self):
        # process_frame 不经 ingest 注入逻辑; _build_context 仍存在(规则路径)
        a = _MyAgent()
        ctx = a._build_context(trigger={"ts": "x"}, context=None)
        self.assertIn("scene_context", ctx)  # 规则路径注入仍可用
        self.assertIn("memory_digest", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)