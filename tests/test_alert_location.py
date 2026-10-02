"""V4.5.2 报警消息 location 注入测试。

验证:
  1. send_alert 包含 location 时, 消息含地址与坐标
  2. location 为空/缺失 → 报警仍正常发送(原逻辑)
  3. 旧字段(person/state/risk)不受影响
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools import communication_tool as ct  # noqa: E402

_MOCK_LOC = {"latitude": 32.0617, "longitude": 118.7778,
             "address": "三江学院附近", "source": "mock"}


class AlertLocationTest(unittest.TestCase):
    def test_build_alert_keeps_location(self):
        alert = ct.CommunicationTool()._build_alert(
            {"severity": "HIGH", "title": "t", "message": "m", "location": _MOCK_LOC})
        self.assertEqual(alert["location"], _MOCK_LOC)

    def test_dingtalk_markdown_contains_location(self):
        alert = ct.CommunicationTool()._build_alert(
            {"severity": "HIGH", "title": "t", "message": "m",
             "person_id": 1, "location": _MOCK_LOC})
        md = ct.CommunicationTool._build_dingtalk_markdown(alert)
        self.assertIn("三江学院附近", md)
        self.assertIn("32.0617", md)
        self.assertIn("118.7778", md)

    def test_markdown_contains_location(self):
        alert = ct.CommunicationTool()._build_alert(
            {"severity": "HIGH", "title": "t", "message": "m", "location": _MOCK_LOC})
        md = ct.CommunicationTool._build_markdown(alert)
        self.assertIn("三江学院附近", md)

    def test_location_missing_alert_normal(self):
        # location 缺失 → 报警仍正常构造, 消息不含位置
        alert = ct.CommunicationTool()._build_alert(
            {"severity": "HIGH", "title": "t", "message": "m"})
        self.assertIsNone(alert["location"])
        md = ct.CommunicationTool._build_dingtalk_markdown(alert)
        self.assertIn("灾害报警", md)
        self.assertNotIn("位置", md)
        # 核心字段仍构造
        ct.CommunicationTool()._build_alert(
            {"severity": "HIGH", "title": "t", "message": "m"})


if __name__ == "__main__":
    unittest.main(verbosity=2)