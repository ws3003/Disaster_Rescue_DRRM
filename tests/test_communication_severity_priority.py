"""V4.3 DingTalk 语义测试(新架构: alert_type 驱动, person/environment 独立)。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.communication_tool import CommunicationTool  # noqa: E402


def _md(alert):
    return CommunicationTool._build_dingtalk_markdown(alert)


class SeverityPriorityTest(unittest.TestCase):
    def test_person_alert_priority_not_environment(self):
        # person alert 只显示人员风险(person_priority), 不出现环境字段
        md = _md({"alert_type": "person", "person_id": 1, "state": "Standing",
                  "person_priority": "LOW", "risk_score": 0.3234})
        self.assertIn("人员风险等级:", md)
        self.assertIn("LOW", md)
        self.assertIn("风险分数:", md)
        self.assertIn("0.3234", md)
        self.assertNotIn("环境危险等级", md)
        self.assertNotIn("environment_severity", md)

    def test_person_alert_respects_priority_high(self):
        md = _md({"alert_type": "person", "person_id": 1, "state": "Help-Waving",
                  "person_priority": "HIGH", "risk_score": 0.62})
        seg = md.split("人员风险等级:")[1].split("\n")[1].strip()
        self.assertEqual(seg, "HIGH")

    def test_environment_alert_severity(self):
        # environment alert 只显示环境危险等级
        md = _md({"alert_type": "environment", "environment_severity": "HIGH",
                  "message": "检测到高危险灾害环境"})
        self.assertIn("环境危险等级:", md)
        self.assertIn("HIGH", md)
        self.assertNotIn("人员ID:", md)
        self.assertNotIn("人员风险等级:", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)