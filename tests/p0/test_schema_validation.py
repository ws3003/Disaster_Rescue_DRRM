"""Schema 校验测试 (A4 / A8): 使用真实 schemas/*.json + jsonschema。

断言:
  1. 现有 mock 数据能通过对应 schema 校验器;
  2. parser.to_dict 的产物能通过对应 schema 校验器 (契约守门);
  3. 破坏性字段会触发校验失败。
"""

import unittest

import jsonschema

import _setup
from src.llm import parser as llm
from src.vlm import parser as vlm


def validate_against(name: str, data) -> None:
    """data 不满足 schemas/<name>.json 时抛 jsonschema.ValidationError。"""
    jsonschema.validate(data, _setup.load_schema(name))


class VlmSchemaTest(unittest.TestCase):
    def test_mock_passes(self):
        validate_against("vlm_output", _setup.load_mock("vlm_output"))

    def test_parse_output_passes(self):
        mock = _setup.load_mock("vlm_output")
        validate_against("vlm_output", vlm.parse(mock).to_dict())

    def test_fallback_output_still_passes(self):
        # 全默认兜底结果也要能过正式 schema 校验
        validate_against("vlm_output", vlm.parse({}).to_dict())

    def test_bad_severity_rejected(self):
        broken = {"scene_type": "fire", "overall_severity": "extreme", "hazards": [], "environment_note": ""}
        with self.assertRaises(jsonschema.ValidationError):
            validate_against("vlm_output", broken)

    def test_missing_field_rejected(self):
        with self.assertRaises(jsonschema.ValidationError):
            validate_against("vlm_output", {"scene_type": "fire"})


class LlmSchemaTest(unittest.TestCase):
    def test_mock_passes(self):
        validate_against("llm_output", _setup.load_mock("llm_output"))

    def test_parse_output_passes(self):
        mock = _setup.load_mock("llm_output")
        validate_against("llm_output", llm.parse(mock).to_dict())

    def test_empty_task_proposals_output_passes(self):
        # 空 task_proposals (rule-based 降级) 也要能过校验
        validate_against("llm_output", llm.parse({}).to_dict())

    def test_bad_pid_type_rejected(self):
        broken = {
            "event_summary": "",
            "risk_explanations": [{"pid": "3", "reason": "x"}],
            "rescue_suggestions": [],
            "task_proposals": [],
        }
        with self.assertRaises(jsonschema.ValidationError):
            validate_against("llm_output", broken)

    def test_missing_field_rejected(self):
        with self.assertRaises(jsonschema.ValidationError):
            validate_against("llm_output", {"event_summary": "x"})


if __name__ == "__main__":
    unittest.main(verbosity=2)