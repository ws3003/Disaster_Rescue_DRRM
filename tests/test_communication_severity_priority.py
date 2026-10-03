"""V4.3 severity(环境) 与 priority(人员) 拆分展示测试。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.communication_tool import CommunicationTool  # noqa: E402


def _dingtalk(alert):
    return CommunicationTool._build_dingtalk_markdown(alert)


class SeverityPriorityTest(unittest.TestCase):
    def test_split_display(self):
        alert = {
            "timestamp": "2026-01-01T00:00:00",
            "severity": "HIGH",
            "state": "Standing",
            "priority": "LOW",
            "person_id": 1,
            "extra": {"risk_score": 0.3234},
        }
        md = _dingtalk(alert)
        self.assertIn("环境危险等级:", md)
        self.assertIn("HIGH", md)
        self.assertIn("人员风险等级:", md)
        self.assertIn("LOW", md)
        self.assertIn("状态:", md)
        self.assertIn("Standing", md)
        self.assertIn("风险分数:", md)
        self.assertIn("0.3234", md)

    def test_missing_priority_shows_dash_not_high(self):
        # priority 缺失 → 人员风险等级显示 "-", 绝不用 severity 顶替
        alert = {"severity": "HIGH", "state": "Standing",
                 "extra": {"risk_score": 0.3234}}
        md = _dingtalk(alert)
        self.assertIn("人员风险等级:", md)
        self.assertIn("-", md)
        # 人员风险等级行不应把 HIGH 当人员档
        segment = md.split("人员风险等级:")[1].split("风险分数:")[0]
        self.assertNotIn("HIGH", segment)


if __name__ == "__main__":
    unittest.main(verbosity=2)