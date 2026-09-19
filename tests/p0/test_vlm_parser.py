"""VLM Parser 单元测试 (A3): 三类输入 + 字段级兜底 + 硬失败。"""

import unittest

from _setup import load_mock  # noqa: F401  (路径注入)
from src.vlm import parser as vlm


class VlmParseInputFormsTest(unittest.TestCase):
    """三种输入形式都应解析出等价结果。"""

    def setUp(self):
        self.valid = {
            "scene_type": "earthquake",
            "overall_severity": "high",
            "hazards": [{"type": "collapse", "brief": "building damage"}],
            "environment_note": "urban disaster scene",
        }

    def test_python_dict(self):
        result = vlm.parse(self.valid)
        self.assertEqual(result.scene_type, "earthquake")
        self.assertEqual(result.overall_severity, "high")

    def test_json_string(self):
        import json

        result = vlm.parse(json.dumps(self.valid))
        self.assertEqual(result.scene_type, "earthquake")

    def test_markdown_fenced_json(self):
        import json

        wrapped = f"```json\n{json.dumps(self.valid, indent=2)}\n```"
        result = vlm.parse(wrapped)
        self.assertEqual(result.scene_type, "earthquake")

    def test_markdown_with_surrounding_text(self):
        import json

        wrapped = f'以下是对现场的解析:\n```json\n{json.dumps(self.valid)}\n```\n以上。'
        result = vlm.parse(wrapped)
        self.assertEqual(result.scene_type, "earthquake")


class VlmFallbackTest(unittest.TestCase):
    """缺失字段应按 interface.md 兜底, 不崩溃。"""

    def test_all_missing_uses_defaults(self):
        result = vlm.parse({})
        self.assertEqual(result.scene_type, "unknown")
        self.assertEqual(result.overall_severity, "medium")
        self.assertEqual(result.hazards, [])
        self.assertEqual(result.environment_note, "")

    def test_invalid_severity_falls_back_medium(self):
        result = vlm.parse({"overall_severity": "extreme"})
        self.assertEqual(result.overall_severity, "medium")

    def test_severity_uppercase_falls_back(self):
        result = vlm.parse({"overall_severity": "HIGH"})
        self.assertEqual(result.overall_severity, "medium")

    def test_hazards_non_list_falls_back_empty(self):
        result = vlm.parse({"hazards": "not-a-list"})
        self.assertEqual(result.hazards, [])

    def test_hazard_items_normalized(self):
        result = vlm.parse({"hazards": [{"type": "fire"}, {"brief": 123}]})
        self.assertEqual(
            result.hazards,
            [{"type": "fire", "brief": ""}, {"type": "unknown", "brief": "123"}],
        )

    def test_none_fields_fall_back(self):
        result = vlm.parse(
            {"scene_type": None, "overall_severity": None, "hazards": None, "environment_note": None}
        )
        self.assertEqual(result.scene_type, "unknown")
        self.assertEqual(result.overall_severity, "medium")
        self.assertEqual(result.hazards, [])
        self.assertEqual(result.environment_note, "")


class VlmParseErrorTest(unittest.TestCase):
    """不可解析输入必须抛错, 不静默降级。"""

    def test_unparseable_garbage_raises(self):
        with self.assertRaises(vlm.SceneParseError):
            vlm.parse("这是一段没有任何 JSON 的文本")

    def test_bare_list_raises(self):
        with self.assertRaises(vlm.SceneParseError):
            vlm.parse("[1, 2, 3]")

    def test_bare_scalar_raises(self):
        with self.assertRaises(vlm.SceneParseError):
            vlm.parse("just a string")

    def test_invalid_type_raises(self):
        with self.assertRaises(vlm.SceneParseError):
            vlm.parse(12345)


class VlmToDictTest(unittest.TestCase):
    """to_dict 应产出含全部四字段的 dict (供下游/校验)。"""

    def test_to_dict_contains_all_fields(self):
        d = vlm.parse({"scene_type": "fire"}).to_dict()
        self.assertEqual(
            set(d.keys()), {"scene_type", "overall_severity", "hazards", "environment_note"}
        )

    def test_mock_roundtrips(self):
        mock = load_mock("vlm_output")
        d = vlm.parse(mock).to_dict()
        self.assertEqual(d["scene_type"], mock["scene_type"])
        self.assertEqual(d["overall_severity"], mock["overall_severity"])
        self.assertEqual(len(d["hazards"]), len(mock["hazards"]))


class VlmValidateTest(unittest.TestCase):
    def test_valid_frame_has_no_issue(self):
        self.assertEqual(vlm.validate(load_mock("vlm_output")), [])

    def test_missing_field_reported(self):
        issues = vlm.validate({"scene_type": "fire"})
        self.assertTrue(any("overall_severity" in i for i in issues))

    def test_bad_severity_reported(self):
        issues = vlm.validate({"overall_severity": "extreme"})
        self.assertTrue(any("overall_severity" in i for i in issues))


if __name__ == "__main__":
    unittest.main(verbosity=2)