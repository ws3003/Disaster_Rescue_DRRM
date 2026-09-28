"""Qwen VLM Provider 测试 (Stage3): fake transport 注入, 不发真实网络请求。

验证:
  1. VLMClient(provider="qwen") 可初始化(无需 API Key);
  2. 请求格式正确(model/messages/system/user/图片 data URL/timeout/auth);
  3. 错误处理正确(缺 Key/坏图片/网络/超时/HTTP/协议/空内容);
  4. mock provider 行为不受影响。
"""

import os
import unittest

import requests

import _setup  # noqa: F401  (路径注入)
from src.vlm import prompts as vlm_prompts
from src.vlm.client import (
    UnsupportedProviderError,
    VLMClient,
    VLMConfigError,
    VLMInputError,
    VLMProviderError,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"0" * 16


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


def ok_response(content='{"scene_type": "fire", "overall_severity": "high", '
                        '"hazards": [], "environment_note": "test"}'):
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


class QwenEnvTestCase(unittest.TestCase):
    """基类: 注入/还原 qwen 相关环境变量。"""

    ENV = {
        "VLM_API_KEY": "test-key-000",
        "VLM_BASE_URL": "https://fake.example/v1",
        "VLM_MODEL": "test-vl-model",
        "VLM_TIMEOUT": "7",
    }

    def setUp(self):
        self._saved = {}
        for key, value in self.ENV.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        # 隔离 VLM_PROVIDER, 避免外部环境互相影响
        self._saved["VLM_PROVIDER"] = os.environ.get("VLM_PROVIDER")
        os.environ.pop("VLM_PROVIDER", None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class QwenInitTest(QwenEnvTestCase):
    def test_qwen_client_initializes(self):
        client = VLMClient(provider="qwen")
        self.assertEqual(client.provider, "qwen")

    def test_env_provider_as_default(self):
        os.environ["VLM_PROVIDER"] = "qwen"
        self.assertEqual(VLMClient().provider, "qwen")

    def test_explicit_provider_overrides_env(self):
        os.environ["VLM_PROVIDER"] = "qwen"
        self.assertEqual(VLMClient(provider="mock").provider, "mock")

    def test_unknown_provider_still_raises(self):
        with self.assertRaises(UnsupportedProviderError):
            VLMClient(provider="unknown")


class QwenRequestFormatTest(QwenEnvTestCase):
    def _call(self, image_input=PNG_BYTES, description="a collapse scene"):
        transport = FakeTransport(response=ok_response())
        client = VLMClient(provider="qwen", transport=transport)
        raw = client.analyze(image_input, image_description=description)
        return transport, raw

    def test_url_and_auth_header(self):
        transport, _ = self._call()
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://fake.example/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-key-000")

    def test_payload_model_and_messages(self):
        transport, _ = self._call()
        payload = transport.calls[0]["json"]
        self.assertEqual(payload["model"], "test-vl-model")
        # system 消息使用现有 SYSTEM_PROMPT (未重新设计)
        self.assertEqual(
            payload["messages"][0], {"role": "system", "content": vlm_prompts.SYSTEM_PROMPT}
        )
        # user 消息: 图片 + build_scene_prompt 文本
        user_content = payload["messages"][1]["content"]
        self.assertEqual(user_content[0]["type"], "image_url")
        self.assertTrue(
            user_content[0]["image_url"]["url"].startswith("data:image/png;base64,")
        )
        self.assertEqual(
            user_content[1],
            {"type": "text", "text": vlm_prompts.build_scene_prompt("a collapse scene")},
        )

    def test_timeout_from_env(self):
        transport, _ = self._call()
        self.assertEqual(transport.calls[0]["timeout"], 7.0)

    def test_returns_model_content(self):
        _, raw = self._call()
        self.assertIsInstance(raw, str)
        self.assertIn("scene_type", raw)

    def test_jpeg_mime_detected(self):
        transport = FakeTransport(response=ok_response())
        client = VLMClient(provider="qwen", transport=transport)
        client.analyze(JPEG_BYTES)
        url = transport.calls[0]["json"]["messages"][1]["content"][0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))

    def test_defaults_when_optional_env_missing(self):
        # 仅保留 API Key, 其余用默认值
        for key in ("VLM_BASE_URL", "VLM_MODEL", "VLM_TIMEOUT"):
            os.environ.pop(key, None)
        transport = FakeTransport(response=ok_response())
        client = VLMClient(provider="qwen", transport=transport)
        client.analyze(PNG_BYTES)
        call = transport.calls[0]
        self.assertEqual(
            call["url"],
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        )
        self.assertEqual(call["json"]["model"], "qwen3-vl-plus")
        self.assertEqual(call["timeout"], 30.0)


class QwenErrorHandlingTest(QwenEnvTestCase):
    def _client(self, transport):
        return VLMClient(provider="qwen", transport=transport)

    def test_missing_api_key_raises_config_error(self):
        os.environ.pop("VLM_API_KEY", None)
        client = self._client(FakeTransport(response=ok_response()))
        with self.assertRaises(VLMConfigError):
            client.analyze(PNG_BYTES)

    def test_none_image_raises_input_error(self):
        client = self._client(FakeTransport(response=ok_response()))
        with self.assertRaises(VLMInputError):
            client.analyze(None)

    def test_bad_image_type_raises_input_error(self):
        client = self._client(FakeTransport(response=ok_response()))
        with self.assertRaises(VLMInputError):
            client.analyze(12345)

    def test_unreadable_file_raises_input_error(self):
        client = self._client(FakeTransport(response=ok_response()))
        with self.assertRaises(VLMInputError):
            client.analyze("Z:/no/such/file.png")

    def test_empty_image_bytes_raises_input_error(self):
        client = self._client(FakeTransport(response=ok_response()))
        with self.assertRaises(VLMInputError):
            client.analyze(b"")

    def test_connection_error_raises_provider_error(self):
        client = self._client(FakeTransport(error=requests.ConnectionError("refused")))
        with self.assertRaises(VLMProviderError):
            client.analyze(PNG_BYTES)

    def test_timeout_raises_provider_error(self):
        client = self._client(FakeTransport(error=requests.Timeout("timed out")))
        with self.assertRaises(VLMProviderError) as ctx:
            client.analyze(PNG_BYTES)
        self.assertIn("超时", str(ctx.exception))

    def test_http_error_raises_provider_error(self):
        resp = FakeResponse(500, {"error": "server boom"})
        resp.text = "Internal Server Error"
        client = self._client(FakeTransport(response=resp))
        with self.assertRaises(VLMProviderError) as ctx:
            client.analyze(PNG_BYTES)
        self.assertIn("500", str(ctx.exception))

    def test_non_json_body_raises_provider_error(self):
        client = self._client(FakeTransport(response=FakeResponse(200, None)))
        with self.assertRaises(VLMProviderError):
            client.analyze(PNG_BYTES)

    def test_missing_choices_raises_provider_error(self):
        client = self._client(FakeTransport(response=FakeResponse(200, {})))
        with self.assertRaises(VLMProviderError):
            client.analyze(PNG_BYTES)

    def test_empty_choices_raises_provider_error(self):
        client = self._client(FakeTransport(response=FakeResponse(200, {"choices": []})))
        with self.assertRaises(VLMProviderError):
            client.analyze(PNG_BYTES)

    def test_empty_content_raises_provider_error(self):
        client = self._client(FakeTransport(response=ok_response("   ")))
        with self.assertRaises(VLMProviderError):
            client.analyze(PNG_BYTES)

    def test_none_content_raises_provider_error(self):
        resp = FakeResponse(200, {"choices": [{"message": {"content": None}}]})
        client = self._client(FakeTransport(response=resp))
        with self.assertRaises(VLMProviderError):
            client.analyze(PNG_BYTES)


class MockUnaffectedTest(QwenEnvTestCase):
    def test_mock_provider_still_works(self):
        # Stage1 的 mock 行为不受 Stage3 改动影响
        client = VLMClient(provider="mock")
        raw = client.analyze(None)
        self.assertIn("scene_type", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)