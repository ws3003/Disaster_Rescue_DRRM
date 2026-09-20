"""Qwen LLM Provider 测试 (Stage4): fake transport 注入, 不发真实网络请求。

验证:
  1. LLMClient(provider="qwen") 可初始化(无需 API Key);
  2. 请求格式正确(model/messages/system/user 证据注入/timeout/auth);
  3. 错误处理正确(缺 Key/网络/超时/HTTP/协议/空内容);
  4. mock provider 行为不受影响;
  5. LLM 配置与 VLM 配置相互独立。
"""

import os
import unittest

import requests

import _setup  # noqa: F401  (路径注入)
from src.llm import prompts as llm_prompts
from src.llm.client import (
    UnsupportedProviderError,
    LLMClient,
    LLMConfigError,
    LLMProviderError,
)

VLM_OUT = {"scene_type": "fire", "overall_severity": "high"}
PERSONS = [
    {"person_id": 1, "state": "Fall", "risk_score": 0.95},
    {"person_id": 2, "state": "Standing", "risk_score": 0.2},
]


class FakeResponse:
    """模拟 requests.Response (仅实现 client 用到的接口)。"""

    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
        self.text = "" if body is None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("响应体不是 JSON")
        return self._body


class FakeTransport:
    """记录请求参数并可编程返回/抛错的假 requests.post。"""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


def ok_response(content='{"event_summary": "test", "risk_explanations": [], '
                        '"rescue_suggestions": [], "task_proposals": []}'):
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


class LlmEnvTestCase(unittest.TestCase):
    """基类: 注入/还原 LLM 相关环境变量, 并隔离 VLM 与 PROVIDER 变量。"""

    ENV = {
        "LLM_API_KEY": "test-key-000",
        "LLM_BASE_URL": "https://fake.example/v1",
        "LLM_MODEL": "test-llm-model",
        "LLM_TIMEOUT": "9",
    }

    def setUp(self):
        self._saved = {}
        for key in self.ENV:
            self._saved[key] = os.environ.get(key)
            os.environ[key] = self.ENV[key]
        # 隔离 PROVIDER 与 VLM 变量, 避免互相影响
        for key in ("LLM_PROVIDER", "VLM_API_KEY", "VLM_BASE_URL", "VLM_MODEL", "VLM_TIMEOUT"):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class LlmInitTest(LlmEnvTestCase):
    def test_qwen_client_initializes(self):
        self.assertEqual(LLMClient(provider="qwen").provider, "qwen")

    def test_env_provider_as_default(self):
        os.environ["LLM_PROVIDER"] = "qwen"
        self.assertEqual(LLMClient().provider, "qwen")

    def test_explicit_provider_overrides_env(self):
        os.environ["LLM_PROVIDER"] = "qwen"
        self.assertEqual(LLMClient(provider="mock").provider, "mock")

    def test_unknown_provider_still_raises(self):
        with self.assertRaises(UnsupportedProviderError):
            LLMClient(provider="unknown")


class LlmRequestFormatTest(LlmEnvTestCase):
    def _call(self, vlm=VLM_OUT, persons=PERSONS):
        transport = FakeTransport(response=ok_response())
        client = LLMClient(provider="qwen", transport=transport)
        raw = client.infer(vlm, persons)
        return transport, raw

    def test_url_and_auth_header(self):
        transport, _ = self._call()
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://fake.example/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-key-000")

    def test_payload_model_and_messages(self):
        transport, _ = self._call()
        payload = transport.calls[0]["json"]
        self.assertEqual(payload["model"], "test-llm-model")
        # system 消息使用现有 SYSTEM_PROMPT (未重新设计)
        self.assertEqual(
            payload["messages"][0], {"role": "system", "content": llm_prompts.SYSTEM_PROMPT}
        )

    def test_user_content_injects_vlm_and_persons(self):
        transport, _ = self._call()
        user_content = transport.calls[0]["json"]["messages"][1]["content"]
        # prompt 由现有 build_advice_prompt 生成并注入 vlm_output 与 person_events
        self.assertEqual(
            user_content, llm_prompts.build_advice_prompt(VLM_OUT, PERSONS)
        )
        self.assertIn("fire", user_content)     # vlm_output.scene_type
        self.assertIn("high", user_content)     # vlm_output.overall_severity
        self.assertIn("Fall", user_content)     # person_events.state
        self.assertIn("0.95", user_content)     # person_events.risk_score

    def test_timeout_from_env(self):
        transport, _ = self._call()
        self.assertEqual(transport.calls[0]["timeout"], 9.0)

    def test_returns_model_content(self):
        _, raw = self._call()
        self.assertIsInstance(raw, str)
        self.assertIn("event_summary", raw)

    def test_defaults_when_optional_env_missing(self):
        for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT"):
            os.environ.pop(key, None)
        client = LLMClient(provider="qwen", transport=FakeTransport(response=ok_response()))
        client.infer(VLM_OUT, PERSONS)
        call = client._transport.calls[-1]
        self.assertEqual(
            call["url"],
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        )
        self.assertEqual(call["json"]["model"], "qwen-plus")
        self.assertEqual(call["timeout"], 30.0)


class LlmErrorHandlingTest(LlmEnvTestCase):
    def _client(self, transport):
        return LLMClient(provider="qwen", transport=transport)

    def test_missing_api_key_raises_config_error(self):
        os.environ.pop("LLM_API_KEY", None)
        with self.assertRaises(LLMConfigError):
            self._client(FakeTransport(response=ok_response())).infer(VLM_OUT, PERSONS)

    def test_vlm_key_does_not_satisfy_llm(self):
        # 只设 VLM_API_KEY 不代表 LLM_ has key (配置隔离)
        os.environ.pop("LLM_API_KEY", None)
        os.environ["VLM_API_KEY"] = "vlm-key-only"
        with self.assertRaises(LLMConfigError):
            self._client(FakeTransport(response=ok_response())).infer(VLM_OUT, PERSONS)

    def test_connection_error_raises_provider_error(self):
        client = self._client(FakeTransport(error=requests.ConnectionError("refused")))
        with self.assertRaises(LLMProviderError):
            client.infer(VLM_OUT, PERSONS)

    def test_timeout_raises_provider_error(self):
        client = self._client(FakeTransport(error=requests.Timeout("timed out")))
        with self.assertRaises(LLMProviderError) as ctx:
            client.infer(VLM_OUT, PERSONS)
        self.assertIn("超时", str(ctx.exception))

    def test_http_error_raises_provider_error(self):
        resp = FakeResponse(502, {"error": "bad gateway"})
        resp.text = "Bad Gateway"
        client = self._client(FakeTransport(response=resp))
        with self.assertRaises(LLMProviderError) as ctx:
            client.infer(VLM_OUT, PERSONS)
        self.assertIn("502", str(ctx.exception))

    def test_non_json_body_raises_provider_error(self):
        client = self._client(FakeTransport(response=FakeResponse(200, None)))
        with self.assertRaises(LLMProviderError):
            client.infer(VLM_OUT, PERSONS)

    def test_missing_choices_raises_provider_error(self):
        client = self._client(FakeTransport(response=FakeResponse(200, {})))
        with self.assertRaises(LLMProviderError):
            client.infer(VLM_OUT, PERSONS)

    def test_empty_choices_raises_provider_error(self):
        client = self._client(FakeTransport(response=FakeResponse(200, {"choices": []})))
        with self.assertRaises(LLMProviderError):
            client.infer(VLM_OUT, PERSONS)

    def test_empty_content_raises_provider_error(self):
        client = self._client(FakeTransport(response=ok_response("   ")))
        with self.assertRaises(LLMProviderError):
            client.infer(VLM_OUT, PERSONS)


class MockUnaffectedTest(LlmEnvTestCase):
    def test_mock_provider_still_works(self):
        # Stage1 的 mock 行为不受 Stage4 改动影响, 且动作仍限于词表
        client = LLMClient(provider="mock")
        raw = client.infer(VLM_OUT, PERSONS)
        self.assertIn("task_proposals", raw)
        for proposal in __import__("json").loads(raw)["task_proposals"]:
            self.assertIn(proposal["action"], llm_prompts.TASK_PROPOSAL_VOCAB)


if __name__ == "__main__":
    unittest.main(verbosity=2)