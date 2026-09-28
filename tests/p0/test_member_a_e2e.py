"""E2E 链路测试 (Stage5): 用 fake VLM/LLM 响应经真实 client + parser 走通全链,
并对产物逐一做正式 schema 校验。默认不调用真实 API。

目的: 保证 A 侧端到端链路不因单点接口变更而断裂。
"""

import json
import os
import unittest

import jsonschema

import _setup  # noqa: F401
from src.llm.client import LLMClient
from src.llm.parser import LLMParseError, parse as parse_llm
from src.vlm.client import VLMClient
from src.vlm.parser import SceneParseError, parse as parse_vlm

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16

PERSONS = [
    {"person_id": 1, "state": "Fall", "risk_score": 0.92, "priority": 1},
]

FAKE_VLM = {
    "scene_type": "fire",
    "overall_severity": "high",
    "hazards": [{"type": "smoke", "brief": "东北角浓烟"}],
    "environment_note": "E2E 假环境: 火源可见, 通道部分受阻。",
}
FAKE_LLM = {
    "event_summary": "E2E 假推理: 火场 1 名人员高风险。",
    "risk_explanations": [{"pid": 1, "reason": "Fall 状态, 风险分 0.92"}],
    "rescue_suggestions": ["优先处置人员 1"],
    "task_proposals": [{"action": "alert", "target": 1, "payload": {"severity": "high"}}],
}


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
        self.text = "" if body is None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeTransport:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(200, {"choices": [{"message": {"content": self.content}}]})


def chat_text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


class E2EEnvTestCase(unittest.TestCase):
    ENV = {
        "VLM_API_KEY": "e2e-vlm-key",
        "LLM_API_KEY": "e2e-llm-key",
        "VLM_BASE_URL": "https://fake.example/v1",
        "LLM_BASE_URL": "https://fake.example/v1",
    }

    def setUp(self):
        self._saved = {}
        for key in ("VLM_API_KEY", "LLM_API_KEY", "VLM_BASE_URL", "LLM_BASE_URL",
                    "VLM_PROVIDER", "LLM_PROVIDER", "VLM_MODEL", "LLM_MODEL",
                    "VLM_TIMEOUT", "LLM_TIMEOUT"):
            self._saved[key] = os.environ.get(key)
        for key, value in self.ENV.items():
            os.environ[key] = value
        for key, value in (("VLM_PROVIDER", None), ("LLM_PROVIDER", None)):
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class E2EChainTest(E2EEnvTestCase):
    def _run_fake_chain(self):
        """fake VLM 响应 → 真实 VLM client → parser → fake LLM → parser。"""
        vlm_transport = FakeTransport(content=chat_text(FAKE_VLM))
        vlm_raw = VLMClient(provider="qwen", transport=vlm_transport).analyze(PNG_BYTES)
        vlm_output = parse_vlm(vlm_raw).to_dict()
        jsonschema.validate(vlm_output, _setup.load_schema("vlm_output"))

        llm_transport = FakeTransport(content=chat_text(FAKE_LLM))
        llm_raw = LLMClient(provider="qwen", transport=llm_transport).infer(vlm_output, PERSONS)
        llm_output = parse_llm(llm_raw).to_dict()
        jsonschema.validate(llm_output, _setup.load_schema("llm_output"))

        return vlm_output, llm_output

    def test_fake_chain_passes_both_schemas(self):
        vlm_out, llm_out = self._run_fake_chain()
        self.assertEqual(vlm_out["scene_type"], "fire")
        self.assertEqual(llm_out["task_proposals"][0]["action"], "alert")

    def test_llm_user_prompt_receives_vlm_output(self):
        vlm_transport = FakeTransport(content=chat_text(FAKE_VLM))
        parse_vlm(VLMClient("qwen", transport=vlm_transport).analyze(PNG_BYTES)).to_dict()

        # 构造第二条断言: LLM 的 user prompt 应携带 vlm scene_type
        llm_transport = FakeTransport(content=chat_text(FAKE_LLM))
        llm_client = LLMClient(provider="qwen", transport=llm_transport)
        llm_client.infer(FAKE_VLM, PERSONS)
        user_content = llm_transport.calls[0]["json"]["messages"][1]["content"]
        self.assertIn("fire", user_content)
        self.assertIn("Fall", user_content)

    def test_broken_vlm_raw_surfaces_parse_error(self):
        # 模型返回 non-JSON → parser 抛错, 契约守门生效
        bad = FakeTransport(content="totally not json")
        raw = VLMClient("qwen", transport=bad).analyze(PNG_BYTES)
        with self.assertRaises(SceneParseError):
            parse_vlm(raw)

    def test_schema_rejects_broken_output(self):
        broken = {"event_summary": "x"}  # 缺 llm_output 必需字段
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(broken, _setup.load_schema("llm_output"))


class E2ENoTraceTest(E2EEnvTestCase):
    def test_no_api_calls_without_transport(self):
        """构造实例不发请求; 未注入 transport 时请求走 requests.post —— 不加 transport
        只构造、不调用 analyze/infer, 保证不会误触网络。"""
        VLMClient(provider="qwen")
        LLMClient(provider="qwen")


if __name__ == "__main__":
    unittest.main(verbosity=2)