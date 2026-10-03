"""V4.3 DingTalk 报警格式测试: 人员报警/环境报警分流。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.communication_tool import CommunicationTool  # noqa: E402


def _md(alert):
    return CommunicationTool._build_dingtalk_markdown(alert)


class DingtalkFormatTest(unittest.TestCase):
    def test_person_alert_shows_fields(self):
        md = _md({"timestamp": "t", "severity": "HIGH", "person_id": 1,
                  "priority": "HIGH", "state": "Fall",
                  "extra": {"risk_score": 0.92}})
        self.assertIn("环境危险等级:", md)
        self.assertIn("HIGH", md)
        self.assertIn("人员ID:", md)
        self.assertIn("人员风险等级:", md)
        # 断言人员风险等级间距 HIGH
        seg = md.split("人员风险等级:")[1].split("\n")[1].strip()
        self.assertEqual(seg, "HIGH")
        self.assertIn("Fall", md)
        self.assertIn("0.92", md)

    def test_env_alert_hides_person_fields(self):
        md = _md({"timestamp": "t", "severity": "HIGH",
                  "message": "检测到高危险灾害环境", "person_id": None})
        self.assertIn("环境高风险报警", md)
        self.assertIn("环境危险等级:", md)
        self.assertNotIn("人员ID:", md)
        self.assertNotIn("人员风险等级:", md)
        self.assertNotIn("风险分数:", md)
        self.assertIn("检测到高危险灾害环境", md)

    def test_missing_fields_no_crash(self):
        md = _md({})
        self.assertIsInstance(md, str)
        self.assertIn("环境危险等级:", md)  # severity 兜底 HIGH


if __name__ == "__main__":
    unittest.main(verbosity=2)