"""Client 层单元测试 (A1 / A5): mock provider + 调用边界 + 未知 provider 报错。"""

import unittest

import jsonschema

import _setup
from src.llm import parser as llm_parser
from src.llm import prompts as llm_prompts
from src.llm.client import LLMClient, UnsupportedProviderError as LLMProviderError
from src.vlm import parser as vlm_parser
from src.vlm.client import UnsupportedProviderError as VLMProviderError
from src.vlm.client import VLMClient


PERSONS = [
    {"person_id": 1, "state": "Standing", "risk_score": 0.2},
    {"person_id": 2, "state": "Fall", "risk_score": 0.95},
]
VLM_FRAME = {"scene_type": "fire", "overall_severity": "high"}


class VlmClientTest(unittest.TestCase):
    def test_mock_client_creates(self):
        client = VLMClient(provider="mock")
        self.assertEqual(client.provider, "mock")

    def test_default_provider_is_mock(self):
        self.assertEqual(VLMClient().provider, "mock")

    def test_analyze_returns_raw(self):
        raw = VLMClient("mock").analyze("placeholder.png")
        self.assertTrue(isinstance(raw, str) and raw.strip())

    def test_analyze_accepts_any_placeholder_input(self):
        for placeholder in (None, "img.jpg", b"\x89PNG...", 123):
            self.assertTrue(VLMClient("mock").analyze(placeholder))

    def test_raw_parses_to_vlm_output(self):
        raw = VLMClient("mock").analyze("placeholder.png")
        frame = vlm_parser.parse(raw).to_dict()
        for field in ("scene_type", "overall_severity", "hazards", "environment_note"):
            self.assertIn(field, frame)

    def test_parsed_output_passes_schema(self):
        raw = VLMClient("mock").analyze(None)
        frame = vlm_parser.parse(raw).to_dict()
        jsonschema.validate(frame, _setup.load_schema("vlm_output"))

    def test_unknown_provider_raises(self):
        with self.assertRaises(VLMProviderError):
            VLMClient(provider="unknown")


class LlmClientTest(unittest.TestCase):
    def test_mock_client_creates(self):
        client = LLMClient(provider="mock")
        self.assertEqual(client.provider, "mock")

    def test_default_provider_is_mock(self):
        self.assertEqual(LLMClient().provider, "mock")

    def test_infer_returns_raw(self):
        raw = LLMClient("mock").infer(VLM_FRAME, PERSONS)
        self.assertTrue(isinstance(raw, str) and raw.strip())

    def test_raw_parses_to_llm_output(self):
        raw = LLMClient("mock").infer(VLM_FRAME, PERSONS)
        frame = llm_parser.parse(raw).to_dict()
        for field in ("event_summary", "risk_explanations", "rescue_suggestions", "task_proposals"):
            self.assertIn(field, frame)

    def test_parsed_output_passes_schema(self):
        raw = LLMClient("mock").infer(VLM_FRAME, PERSONS)
        frame = llm_parser.parse(raw).to_dict()
        jsonschema.validate(frame, _setup.load_schema("llm_output"))

    def test_actions_only_from_vocab(self):
        raw = LLMClient("mock").infer(VLM_FRAME, PERSONS)
        for proposal in llm_parser.parse(raw).task_proposals:
            self.assertIn(proposal["action"], llm_prompts.TASK_PROPOSAL_VOCAB)

    def test_alert_targets_highest_risk_person(self):
        raw = LLMClient("mock").infer(VLM_FRAME, PERSONS)
        alerts = [
            p for p in llm_parser.parse(raw).task_proposals if p["action"] == "send_alert"
        ]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["target"], 2)  # risk 0.95 最高

    def test_empty_person_events_still_valid(self):
        raw = LLMClient("mock").infer({"scene_type": "flood"}, [])
        frame = llm_parser.parse(raw).to_dict()
        jsonschema.validate(frame, _setup.load_schema("llm_output"))
        # 无人员时不应出现 send_alert, 但 save_event 留痕仍在
        actions = {p["action"] for p in frame["task_proposals"]}
        self.assertNotIn("send_alert", actions)
        self.assertIn("save_event", actions)

    def test_unknown_provider_raises(self):
        with self.assertRaises(LLMProviderError):
            LLMClient(provider="unknown")


class MockPipelineTest(unittest.TestCase):
    """Step3 目标链路: VLM Mock→Parser→vlm_output→LLM Mock→Parser→llm_output。"""

    def test_full_mock_chain(self):
        vlm_raw = VLMClient("mock").analyze("scene.png")
        vlm_frame = vlm_parser.parse(vlm_raw).to_dict()
        jsonschema.validate(vlm_frame, _setup.load_schema("vlm_output"))

        llm_raw = LLMClient("mock").infer(vlm_frame, PERSONS)
        llm_frame = llm_parser.parse(llm_raw).to_dict()
        jsonschema.validate(llm_frame, _setup.load_schema("llm_output"))

        # 链路末端的 task_proposals 可安全交给下游 planner
        for proposal in llm_frame["task_proposals"]:
            self.assertIn(proposal["action"], llm_prompts.TASK_PROPOSAL_VOCAB)


if __name__ == "__main__":
    unittest.main(verbosity=2)