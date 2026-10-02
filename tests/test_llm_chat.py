"""LLMClient.ask() 对话接口单元测试(V4.3 Chat)。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.llm.client import LLMClient  # noqa: E402
from src.llm.prompts import CHAT_SYSTEM_PROMPT, build_chat_prompt  # noqa: E402


class _FakeResp:
    def __init__(self, body, status=200, text=""):
        self._body = body
        self.status_code = status
        self.text = text or str(body)[:200]

    def json(self):
        return self._body


class ChatAskTest(unittest.TestCase):
    def test_prompt_builds(self):
        self.assertIn("现场怎么样？", build_chat_prompt("现场怎么样？"))
        self.assertIn("persons", build_chat_prompt("现场?", {"persons": []}))

    def test_mock_ask_returns_text(self):
        out = LLMClient(provider="mock").ask("现场怎么样？")
        self.assertIsInstance(out, str)
        self.assertIn("提问", out)

    def test_mock_ask_with_context_counts_persons(self):
        out = LLMClient(provider="mock").ask(
            "现场怎么样？", context={"scene": "collapse", "persons": [{"person_id": 1}]})
        self.assertIn("1 名人员", out)
        self.assertIn("collapse", out)

    def test_qwen_ask_uses_chat_prompt_and_returns_content(self):
        captured = {}

        def fake_transport(url, json, headers, timeout):
            captured.update(url=url, payload=json)
            return _FakeResp({"choices": [{"message": {"content": "现场状态正常。"}}]})

        c = LLMClient(provider="qwen", transport=fake_transport)
        # 注入假配置, 避免依赖环境变量
        c._llm_config = {"api_key": "k", "base_url": "https://x/v1",
                         "model": "qwen-plus", "timeout": 1}
        out = c.ask("现场怎么样？", context={"persons": []})
        self.assertEqual(out, "现场状态正常。")
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        msgs = captured["payload"]["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("信息辅助", msgs[0]["content"])
        self.assertEqual(msgs[1]["role"], "user")
        self.assertIn("现场怎么样？", msgs[1]["content"])

    def test_qwen_ask_missing_key_raises_config_error(self):
        from src.llm.client import LLMConfigError
        c = LLMClient(provider="qwen")
        c._llm_config = {"api_key": "", "base_url": "https://x/v1",
                         "model": "qwen-plus", "timeout": 1}
        with self.assertRaises(LLMConfigError):
            c.ask("hi")

    def test_infer_regression_mock_still_llm_output(self):
        # 决策链 infer 不受 ask 新增影响: mock 仍返回 llm_output JSON
        import json
        raw = LLMClient(provider="mock").infer(
            {"scene_type": "fire", "overall_severity": "high"},
            [{"person_id": 1, "state": "Fall", "risk_score": 0.9}])
        data = json.loads(raw)
        self.assertIn("task_proposals", data)
        self.assertIn("risk_explanations", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)