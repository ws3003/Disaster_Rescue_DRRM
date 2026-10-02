"""V4.5.2 Phase1 Location Alert 自动透传测试。

验证:
  Test1: context 有 location → send_alert action.args 含 location(image_url/task_id 保留)
  Test2: 无 location → Agent 正常运行, send_alert 无 location 字段(旧行为兼容)
  Test3: location 异常 → Agent 不中断, location 空, 其它流程正常
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.agent import Agent  # noqa: E402

_MOCK_LOC = {"latitude": 32.0617, "longitude": 118.7778, "address": "三江学院附近"}
_SC = {"scene_context": {"persons": [], "risk_summary": {}, "events": [], "tasks": []}}


class _FakePlanner:
    def plan_from_llm_output(self, llm_output, context=None):
        # 对齐真实 Planner 契约: actions 位于顶层, 且 action 名在 args.action 内(与注入守卫一致)。
        return {
            "ok": True,
            "status": "planned",
            "task_id": "T1",
            "actions": [
                {"tool": "communication_tool",
                 "args": {"action": "send_alert", "person_id": 1,
                          "message": "fall", "severity": "HIGH"}},
                {"tool": "database_tool", "action": "save_event", "args": {}},
            ],
        }


class _MyAgent(Agent):
    def __init__(self):
        pass


def _make(planner=None):
    a = _MyAgent()
    a.planner = planner or _FakePlanner()
    a.memory = mock.MagicMock()
    a.executor = mock.MagicMock()
    return a


def _llm_output():
    return {"event_summary": "t", "risk_explanations": [],
            "rescue_suggestions": [], "task_proposals": []}


class _Shared:
    SC = "src.agent.context_provider.ContextProvider.get_scene_context"
    LOC = "src.agent.context_provider.ContextProvider.get_location"


class LocationAlertPipelineTest(unittest.TestCase):
    def _patch(self, loc=_MOCK_LOC, sc=_SC, loc_err=False, sc_err=False):
        stack = []
        stack.append(mock.patch(_Shared.LOC,
                                side_effect=RuntimeError("boom") if loc_err else
                                (lambda: loc)))
        stack.append(mock.patch(_Shared.SC,
                                side_effect=RuntimeError("boom") if sc_err else
                                (lambda: sc)))
        for p in stack:
            p.start()
            self.addCleanup(p.stop)

    def _send_alert_args(self, a):
        a.executor.execute_agent_task.assert_called_once()
        exec_task = a.executor.execute_agent_task.call_args[0][0]
        for act in exec_task["actions"]:
            args = act.get("args") or {}
            if act.get("tool") == "communication_tool" and args.get("action") == "send_alert":
                return args
        return None

    def test1_location_transparent(self):
        a = _make()
        self._patch(loc=_MOCK_LOC)
        a.ingest_llm_output(
            _llm_output(),
            context={"image_url": "http://x", "task_id": "T1", "location": _MOCK_LOC})
        args = self._send_alert_args(a)
        self.assertIsNotNone(args)
        self.assertEqual(args.get("location"), _MOCK_LOC)   # location 透传
        self.assertEqual(args.get("image_path"), "http://x")  # 已规范化 URL http
        # image_path 由 img_url 注入, 非 image_url
        self.assertIn("message", args)

    def test2_no_location(self):
        a = _make()
        self._patch(loc={})   # location 为空
        a.ingest_llm_output(_llm_output(), context={"task_id": "T1"})
        args = self._send_alert_args(a)
        self.assertIsNotNone(args)
        self.assertNotIn("location", args)   # 无 location 字段, 旧行为兼容

    def test3_location_exception(self):
        a = _make()
        self._patch(loc_err=True)             # ContextProvider.get_location 抛异常
        a.ingest_llm_output(
            _llm_output(),
            context={"image_url": "http://x", "task_id": "T1"})
        # Agent 不中断, send_alert 仍执行, 无 location(异常注入在 context 层已兜底 {})
        a.executor.execute_agent_task.assert_called_once()
        args = self._send_alert_args(a)
        self.assertIsNotNone(args)
        self.assertNotIn("location", args)


if __name__ == "__main__":
    unittest.main(verbosity=2)