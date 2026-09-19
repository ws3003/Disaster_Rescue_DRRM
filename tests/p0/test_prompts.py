"""Prompt 层单元测试 (A2 / A6): 模板可生成、证据可注入、词表完整。"""

import unittest

from _setup import load_mock  # noqa: F401
from src.llm import prompts as llm_prompts
from src.vlm import prompts as vlm_prompts


class VlmPromptTest(unittest.TestCase):
    def test_scene_types_required_covered(self):
        for st in ("earthquake", "fire", "flood", "collapse", "unknown"):
            self.assertIn(st, vlm_prompts.SCENE_TYPES, f"缺少灾种 {st}")

    def test_system_prompt_non_empty(self):
        self.assertTrue(vlm_prompts.SYSTEM_PROMPT.strip())

    def test_system_prompt_matches_schema_fields(self):
        for field in ("scene_type", "overall_severity", "hazards", "environment_note"):
            self.assertIn(field, vlm_prompts.SYSTEM_PROMPT)

    def test_system_prompt_no_rescue_overreach(self):
        # VLM 只负责视觉理解, 明确"不负责调度/处置执行"
        self.assertIn("只负责视觉理解", vlm_prompts.SYSTEM_PROMPT)
        self.assertIn("你不负责", vlm_prompts.SYSTEM_PROMPT)

    def test_build_scene_prompt_without_desc(self):
        p = vlm_prompts.build_scene_prompt()
        self.assertTrue(isinstance(p, str) and p.strip())
        # 应包含输出 JSON 要求
        self.assertIn("JSON", p)

    def test_build_scene_prompt_with_desc(self):
        p = vlm_prompts.build_scene_prompt("urban building collapse scene")
        self.assertIn("urban building collapse scene", p)


class LlmPromptTest(unittest.TestCase):
    def test_system_prompt_non_empty(self):
        self.assertTrue(llm_prompts.SYSTEM_PROMPT.strip())

    def test_system_prompt_matches_schema_fields(self):
        for field in ("event_summary", "risk_explanations", "rescue_suggestions", "task_proposals"):
            self.assertIn(field, llm_prompts.SYSTEM_PROMPT)

    def test_system_prompt_is_advisor_not_executor(self):
        # 参谋只分析/建议/生成候选, 明确"不负责执行/报警/控设备"
        self.assertIn("参谋", llm_prompts.SYSTEM_PROMPT)
        self.assertIn("只负责分析", llm_prompts.SYSTEM_PROMPT)
        self.assertIn("你不负责", llm_prompts.SYSTEM_PROMPT)

    def test_build_advice_prompt_injects_vlm_output(self):
        vlm = {"scene_type": "earthquake", "overall_severity": "high"}
        persons = [{"person_id": 1, "state": "Fall", "risk_score": 90}]
        p = llm_prompts.build_advice_prompt(vlm, persons)
        self.assertIn("earthquake", p)
        self.assertIn("high", p)
        self.assertIn("Fall", p)
        self.assertIn("risk_score", p)

    def test_build_advice_prompt_injects_person_id(self):
        vlm = {"scene_type": "fire"}
        persons = [{"person_id": 7, "state": "Lying"}]
        p = llm_prompts.build_advice_prompt(vlm, persons)
        self.assertIn("person_id", p)
        self.assertIn("pid", p)  # 明确告知 pid 与 person_id 对应

    def test_build_advice_prompt_non_empty(self):
        p = llm_prompts.build_advice_prompt({}, [])
        self.assertTrue(isinstance(p, str) and p.strip())

    def test_advice_prompt_directs_json_output(self):
        p = llm_prompts.build_advice_prompt({}, [])
        self.assertIn("JSON", p)


class TaskProposalVocabTest(unittest.TestCase):
    def test_vocab_exists_and_has_required_actions(self):
        self.assertIsInstance(llm_prompts.TASK_PROPOSAL_VOCAB, dict)
        for action in ("alert", "notify", "log"):
            self.assertIn(action, llm_prompts.TASK_PROPOSAL_VOCAB)

    def test_actions_have_meaning(self):
        for action, meaning in llm_prompts.TASK_PROPOSAL_VOCAB.items():
            self.assertTrue(meaning.strip(), f"action {action} 缺含义说明")

    def test_vocab_injected_into_system_prompt(self):
        for action in ("alert", "notify", "log"):
            self.assertIn(action, llm_prompts.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)