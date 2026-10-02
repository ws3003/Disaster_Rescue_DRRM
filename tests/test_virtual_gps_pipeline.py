"""V4.7 虚拟 GPS 端到端验证(无硬件): GPS→LocationTool→Amap→Context→Agent→Communication。

FakeGPS 模拟 上海人民广场(31.2304, 121.4737)。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.location_tool import LocationTool  # noqa: E402
from src.tools import communication_tool as ct  # noqa: E402
from src.agent.agent import Agent  # noqa: E402

SH = {"latitude": 31.2304, "longitude": 121.4737}


class FakeGPSProvider:
    """虚拟 GPS: 返回上海人民广场坐标或 {} """

    def __init__(self, ok=True):
        self.ok = ok

    def get_location(self):
        return dict(SH) if self.ok else {}


def _gps_patch(ok=True, amap="上海市黄浦区人民广场", amap_err=False):
    stack = [
        mock.patch("src.tools.gps_provider.GPSProvider.get_location",
                   side_effect=lambda: FakeGPSProvider(ok=ok).get_location()),
    ]
    if amap_err:
        stack.append(mock.patch("src.tools.amap_provider.AmapProvider.regeo",
                                side_effect=RuntimeError("amap down")))
    else:
        stack.append(mock.patch("src.tools.amap_provider.AmapProvider.regeo",
                                return_value=amap))
    for p in stack:
        p.start()
    return stack


class VirtualGpsTest(unittest.TestCase):
    def tearDown(self):
        # 只显式 start 过的 patch 在此处由调用栈 manage; 这里为空.
        pass

    def test_case1_gps_success(self):
        with mock.patch.dict(os.environ, {"AMAP_API_KEY": "sk-test"}):
            stack = _gps_patch(ok=True, amap="上海市黄浦区人民广场")
            try:
                loc = LocationTool().get_current_location()
            finally:
                for p in stack:
                    p.stop()
        self.assertEqual(loc["source"], "gps")
        self.assertEqual(loc["latitude"], 31.2304)
        self.assertEqual(loc["longitude"], 121.4737)
        self.assertIn("上海", loc["address"])

    def test_case2_gps_fail_mock_fallback(self):
        with mock.patch.dict(os.environ, {"AMAP_API_KEY": ""}):
            stack = _gps_patch(ok=False, amap=None)  # GPS 失败, amap 空
            try:
                loc = LocationTool().get_current_location()
            finally:
                for p in stack:
                    p.stop()
        self.assertEqual(loc["source"], "mock")
        self.assertIn("三江学院附近", loc["address"])

    def test_case3_gps_present_amap_fail(self):
        with mock.patch.dict(os.environ, {"AMAP_API_KEY": "sk-test"}):
            stack = _gps_patch(ok=True, amap_err=True)  # GPS ok, Amap 异常
            try:
                loc = LocationTool().get_current_location()
            finally:
                for p in stack:
                    p.stop()
        self.assertEqual(loc["source"], "gps")
        self.assertEqual(loc["latitude"], 31.2304)
        self.assertEqual(loc["longitude"], 121.4737)


class VirtualGpsAlertPipelineTest(unittest.TestCase):
    """Case4: context.location → Agent → communication_tool → 报警消息含位置。"""

    def _make_agent(self):
        class _FakePlanner:
            def plan_from_llm_output(self, llm_output, context=None):
                return {
                    "ok": True, "status": "planned", "task_id": "T1",
                    "actions": [{
                        "tool": "communication_tool",
                        "args": {"action": "send_alert", "person_id": 1,
                                 "message": "fall", "severity": "HIGH"}},
                        {"tool": "database_tool", "action": "save_event", "args": {}},
                    ]}

        class _MyAgent(Agent):
            def __init__(self):
                pass

        a = _MyAgent()
        a.planner = _FakePlanner()
        a.memory = mock.MagicMock()
        a.executor = mock.MagicMock()
        return a

    def _sc_patch(self):
        return mock.patch("src.agent.context_provider.ContextProvider.get_scene_context",
                          return_value={"scene_context": {"persons": [],
                                                          "risk_summary": {},
                                                          "events": [], "tasks": []}})

    def test_agent_injects_location_to_communication(self):
        loc = {**SH, "address": "上海人民广场", "source": "gps"}
        a = self._make_agent()
        with self._sc_patch():
            a.ingest_llm_output(
                {"event_summary": "t", "risk_explanations": [],
                 "rescue_suggestions": [], "task_proposals": []},
                context={"image_url": "http://x", "task_id": "T1", "location": loc})
        a.executor.execute_agent_task.assert_called_once()
        exec_task = a.executor.execute_agent_task.call_args[0][0]
        send_args = None
        for act in exec_task["actions"]:
            args = act.get("args") or {}
            if act.get("tool") == "communication_tool" and args.get("action") == "send_alert":
                send_args = args
        self.assertIsNotNone(send_args)
        self.assertEqual(send_args["location"], loc)

        # CommunicationTool 渲染地址
        tool = ct.CommunicationTool()
        alert = tool._build_alert({"severity": "HIGH", "title": "t", "message": "m",
                                   "person_id": 1, "location": loc})
        md = ct.CommunicationTool._build_dingtalk_markdown(alert)
        self.assertIn("上海人民广场", md)
        self.assertIn("31.2304", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)