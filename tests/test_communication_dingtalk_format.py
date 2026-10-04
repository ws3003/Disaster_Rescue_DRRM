"""V4.3 DingTalk 报警格式测试: alert_type 驱动(人员/环境 模板分离)。"""
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
    def test_person_alert_no_environment_fields(self):
        md = _md({"alert_type": "person", "person_id": 1, "state": "Fall",
                  "person_priority": "HIGH", "risk_score": 0.6})
        self.assertIn("人员ID:", md)
        self.assertIn("状态:", md)
        self.assertIn("人员风险等级:", md)
        self.assertIn("风险分数:", md)
        # 禁环境字段
        for banned in ("环境危险等级", "环境等级", "environment_severity", "环境高风险报警"):
            self.assertNotIn(banned, md)

    def test_environment_alert_no_person_fields(self):
        md = _md({"alert_type": "environment", "environment_severity": "HIGH",
                  "message": "检测到高危险灾害环境"})
        self.assertIn("环境高风险报警", md)
        self.assertIn("环境危险等级:", md)
        self.assertIn("检测到高危险灾害环境", md)
        # 禁人员字段
        for banned in ("人员ID:", "状态:", "人员风险等级:", "风险分数:"):
            self.assertNotIn(banned, md)

    def test_person_alert_defaults_when_unkown_type(self):
        # 未知/缺省 alert_type → 按人员模板(person_id 存在时)
        md = _md({"person_id": 1, "state": "Fall", "person_priority": "HIGH"})
        self.assertIn("人员ID:", md)
        self.assertIn("人员风险等级:", md)

    def test_environment_alert_via_build_alert_env_template(self):
        # 经 _build_alert 透传 alert_type: environment → 环境模板, 不落人员字段
        tool = CommunicationTool()
        alert = tool._build_alert(
            {"action": "send_alert", "alert_type": "environment",
             "environment_severity": "HIGH",
             "title": "t", "message": "检测到高危险灾害环境"})
        self.assertEqual(alert.get("alert_type"), "environment")
        md = CommunicationTool._build_dingtalk_markdown(alert)
        self.assertIn("环境高风险报警", md)
        self.assertIn("环境危险等级:", md)
        self.assertNotIn("人员ID:", md)
        self.assertNotIn("人员风险等级:", md)

    def test_person_alert_via_build_alert_keeps_person_fields(self):
        tool = CommunicationTool()
        alert = tool._build_alert(
            {"action": "send_alert", "alert_type": "person",
             "person_id": 1, "state": "Fall", "person_priority": "HIGH",
             "risk_score": 0.634, "title": "t", "message": "m"})
        self.assertEqual(alert.get("alert_type"), "person")
        md = CommunicationTool._build_dingtalk_markdown(alert)
        self.assertIn("人员ID:", md)
        self.assertIn("人员风险等级:", md)
        self.assertNotIn("环境危险等级", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)