"""V4.5.1 LocationTool 接入 Agent Context 测试。

验证:
  1. Agent context(含 ingest / _build_context) 包含 location
  2. 原 scene_context 仍存在
  3. image_url/task_id 不丢失
  4. location 异常 → location={}, Agent 不失败
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.agent import Agent  # noqa: E402

_MOCK_LOC = {"latitude": 32.0617, "longitude": 118.7778,
             "address": "三江学院附近", "source": "mock"}
_SC = {"scene_context": {"persons": [], "risk_summary": {}, "events": [], "tasks": []}}


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
    return {"event_summary": "t", "risk_explanations": [],
            "rescue_suggestions": [], "task_proposals": []}


def _make():
    a = _MyAgent()
    a.planner = _FakePlanner()
    a.memory = mock.MagicMock()
    a.executor = mock.MagicMock()
    return a


class _Ctx:
    SC = "src.agent.context_provider.ContextProvider.get_scene_context"
    LOC = "src.agent.context_provider.ContextProvider.get_location"


class LocationContextTest(unittest.TestCase):
    def _patch(self, loc=None, sc=None, loc_err=False, sc_err=False):
        stack = []
        stack.append(mock.patch(_Ctx.LOC,
                                side_effect=RuntimeError("loc boom") if loc_err else
                                (lambda: loc or _MOCK_LOC)))
        stack.append(mock.patch(_Ctx.SC,
                                side_effect=RuntimeError("sc boom") if sc_err else
                                (lambda: sc if sc is not None else _SC)))
        for p in stack:
            p.start()
            self.addCleanup(p.stop)

    def test_ingest_contains_location_and_preserves_fields(self):
        a = _make()
        self._patch()
        a.ingest_llm_output(_llm_output(),
                            context={"image_url": "http://x", "task_id": "T1"})
        ctx = a.planner.called_context
        self.assertEqual(ctx["location"], _MOCK_LOC)
        self.assertEqual(ctx["image_url"], "http://x")
        self.assertEqual(ctx["task_id"], "T1")
        self.assertIn("scene_context", ctx)  # 原 scene_context 仍存在

    def test_build_context_contains_location(self):
        a = _MyAgent()  # 规则路径 _build_context
        self._patch()
        ctx = a._build_context(trigger={"ts": "x"}, context=None)
        self.assertEqual(ctx["location"], _MOCK_LOC)
        self.assertIn("scene_context", ctx)
        self.assertIn("trigger", ctx)

    def test_location_exception_fallback_empty(self):
        a = _make()
        self._patch(loc_err=True)
        a.ingest_llm_output(_llm_output(),
                            context={"image_url": "http://x", "task_id": "T1"})
        ctx = a.planner.called_context
        self.assertEqual(ctx["location"], {})     # 兜底
        self.assertIn("scene_context", ctx)       # scene_context 不受影响
        self.assertIsNotNone(ctx)                 # Agent 未失败


if __name__ == "__main__":
    unittest.main(verbosity=2)