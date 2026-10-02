"""V4.4 全链路上下文集成测试(只读生产代码):
VLM/LLM → Agent.ingest_llm_output → (ContextProvider scene_context) → Planner → Executor。

验证:
  case1: ingest 注入 scene_context {persons,risk_summary,events,tasks} 进 planner context
  case2: Provider 异常 → Agent 仍运行, scene_context={}
  case3: 旧接口 ingest(llm_output) 无 context 仍正常(向后兼容)
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.agent import Agent  # noqa: E402

_SC = {
    "scene_context": {
        "persons": [{"id": 1, "state": "Fall", "risk": "HIGH"}],
        "risk_summary": {"highest": "HIGH", "count": 1},
        "events": [],
        "tasks": [],
    }
}


class _FakePlanner:
    def __init__(self):
        self.called_context = None

    def plan_from_llm_output(self, llm_output, context=None):
        self.called_context = context
        return {"ok": True, "actions": [], "status": "done"}


class _MyAgent(Agent):
    def __init__(self):
        pass


def _llm_output():
    return {"event_summary": "test", "risk_explanations": [],
            "rescue_suggestions": [], "task_proposals": []}


def _make():
    a = _MyAgent()
    a.planner = _FakePlanner()
    a.memory = mock.MagicMock()
    a.executor = mock.MagicMock()
    return a


class FullContextChainTest(unittest.TestCase):
    def test_case1_planner_receives_scene_context(self):
        a = _make()
        with mock.patch("src.agent.context_provider.ContextProvider.get_scene_context",
                        return_value=_SC):
            a.ingest_llm_output(_llm_output(),
                                context={"image_url": "http://x", "task_id": "T1"})
        ctx = a.planner.called_context
        self.assertIn("scene_context", ctx)
        sc = ctx["scene_context"]
        for k in ("persons", "risk_summary", "events", "tasks"):
            self.assertIn(k, sc)
        self.assertEqual(sc["persons"][0]["id"], 1)
        # 原字段保留
        self.assertEqual(ctx["image_url"], "http://x")
        self.assertEqual(ctx["task_id"], "T1")

    def test_case2_provider_exception_fallback(self):
        a = _make()
        with mock.patch("src.agent.context_provider.ContextProvider.get_scene_context",
                        side_effect=RuntimeError("boom")):
            a.ingest_llm_output(_llm_output(),
                                context={"image_url": "http://x", "task_id": "T1"})
        ctx = a.planner.called_context
        self.assertEqual(ctx["scene_context"], {})
        self.assertIsNotNone(ctx)  # 不阻断

    def test_case3_old_interface_without_context(self):
        a = _make()
        with mock.patch("src.agent.context_provider.ContextProvider.get_scene_context",
                        return_value=_SC):
            res = a.ingest_llm_output(_llm_output())  # 不传 context(旧接口)
        # 正常运行且返回 list
        self.assertIsInstance(res, list)
        self.assertTrue(all(isinstance(r, dict) for r in res))
        # context 被注入 scene_context
        self.assertIn("scene_context", a.planner.called_context)


if __name__ == "__main__":
    unittest.main(verbosity=2)