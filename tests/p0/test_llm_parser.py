"""LLM Parser 单元测试 (A7): 三类输入 + 字段级兜底 + 硬失败。"""

import json
import unittest

from _setup import load_mock  # noqa: F401
from src.llm import parser as llm


class LlmParseInputFormsTest(unittest.TestCase):
    def setUp(self):
        self.valid = {
            "event_summary": "教学楼坍塌",
            "risk_explanations": [{"pid": 3, "reason": "昏迷倒地"}],
            "rescue_suggestions": ["优先破拆 id=3"],
            "task_proposals": [{"action": "alert", "target": 3, "payload": {"severity": "critical"}}],
        }

    def test_python_dict(self):
        result = llm.parse(self.valid)
        self.assertEqual(result.event_summary, "教学楼坍塌")

    def test_json_string(self):
        result = llm.parse(json.dumps(self.valid))
        self.assertEqual(result.task_proposals[0]["action"], "alert")

    def test_markdown_fenced_json(self):
        wrapped = f"```json\n{json.dumps(self.valid)}\n```"
        result = llm.parse(wrapped)
        self.assertEqual(result.risk_explanations[0]["pid"], 3)


class LlmFallbackTest(unittest.TestCase):
    def test_all_missing_uses_defaults(self):
        result = llm.parse({})
        self.assertEqual(result.event_summary, "")
        self.assertEqual(result.risk_explanations, [])
        self.assertEqual(result.rescue_suggestions, [])
        self.assertEqual(result.task_proposals, [])

    def test_task_proposals_empty_by_default(self):
        """空 task_proposals 是合法降级路径: 下游 planner 走 rule-based。"""
        result = llm.parse({"event_summary": "仅规则触发"})
        self.assertEqual(result.task_proposals, [])

    def test_risk_explanation_with_bad_pid_dropped(self):
        result = llm.parse(
            {"risk_explanations": [{"pid": "abc", "reason": "x"}, {"pid": 2, "reason": "y"}]}
        )
        self.assertEqual([r["pid"] for r in result.risk_explanations], [2])

    def test_task_proposal_normalized(self):
        result = llm.parse(
            {"task_proposals": [{"action": "notify", "target": "5", "payload": "bad"}, {"target": 1}]}
        )
        # 字符串数字 target 转为 int
        self.assertEqual(result.task_proposals[0]["target"], 5)
        # 非 dict payload 兜底为空对象
        self.assertEqual(result.task_proposals[0]["payload"], {})
        # 缺 action 兜底为 "log"
        self.assertEqual(result.task_proposals[1]["action"], "log")

    def test_bool_not_treated_as_int_target(self):
        result = llm.parse({"task_proposals": [{"action": "log", "target": True}]})
        self.assertIsNone(result.task_proposals[0]["target"])


class LlmParseErrorTest(unittest.TestCase):
    def test_unparseable_raises(self):
        with self.assertRaises(llm.LLMParseError):
            llm.parse("no json here")

    def test_bare_list_raises(self):
        with self.assertRaises(llm.LLMParseError):
            llm.parse("[1,2]")

    def test_invalid_type_raises(self):
        with self.assertRaises(llm.LLMParseError):
            llm.parse(3.14)


class LlmToDictTest(unittest.TestCase):
    def test_to_dict_contains_all_fields(self):
        d = llm.parse({"event_summary": "x"}).to_dict()
        self.assertEqual(
            set(d.keys()),
            {"event_summary", "risk_explanations", "rescue_suggestions", "task_proposals"},
        )

    def test_mock_roundtrips(self):
        mock = load_mock("llm_output")
        d = llm.parse(mock).to_dict()
        self.assertEqual(d["event_summary"], mock["event_summary"])
        self.assertEqual(len(d["task_proposals"]), len(mock["task_proposals"]))


class LlmValidateTest(unittest.TestCase):
    def test_valid_frame_has_no_issue(self):
        self.assertEqual(llm.validate(load_mock("llm_output")), [])

    def test_missing_field_reported(self):
        issues = llm.validate({"event_summary": "only"})
        self.assertTrue(any("task_proposals" in i for i in issues))

    def test_bad_pid_type_reported(self):
        frame = {"risk_explanations": [{"pid": "3", "reason": "x"}]}
        self.assertTrue(any("pid" in i for i in llm.validate(frame)))


if __name__ == "__main__":
    unittest.main(verbosity=2)