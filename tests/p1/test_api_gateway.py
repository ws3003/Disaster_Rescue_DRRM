"""API Gateway 测试 (Stage6, tests/p1): FastAPI endpoint + FakeTransport 注入。

- 不调用真实 Qwen API、不需要真实 Key;
- 通过 app.dependency_overrides[get_clients] 注入带 FakeTransport 的客户端;
- 图片用测试临时 PNG 文件路径 (走真实 VLMClient 的图片编码路径)。
"""

import json
import os
import sys
import tempfile
import unittest

import requests
from fastapi.testclient import TestClient

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import api
from src.llm.client import LLMClient
from src.vlm.client import VLMClient

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16

FAKE_VLM = {
    "scene_type": "collapse",
    "overall_severity": "critical",
    "hazards": [{"type": "debris", "brief": "瓦砾覆盖通道"}],
    "environment_note": "api 测试假环境",
}
FAKE_LLM = {
    "event_summary": "api 测试假推理",
    "risk_explanations": [{"pid": 1, "reason": "Fall 高风险"}],
    "rescue_suggestions": ["优先处置人员 1"],
    "task_proposals": [{"action": "alert", "target": 1, "payload": {"severity": "critical"}}],
}

PERSONS = [
    {"person_id": 1, "state": "Fall", "risk_score": 0.92, "priority": "HIGH"},
]


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
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return FakeResponse(200, {"choices": [{"message": {"content": self.content}}]})


def chat_text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


class GatewayTestCase(unittest.TestCase):
    def setUp(self):
        self._saved_env = {}
        for key in ("VLM_API_KEY", "LLM_API_KEY", "VLM_BASE_URL", "LLM_BASE_URL",
                    "VLM_PROVIDER", "LLM_PROVIDER"):
            self._saved_env[key] = os.environ.get(key)
        os.environ["VLM_API_KEY"] = "vlm"
        os.environ["LLM_API_KEY"] = "llm"
        os.environ.pop("VLM_PROVIDER", None)
        os.environ.pop("LLM_PROVIDER", None)

        # 临时图片
        self._tmp = tempfile.TemporaryDirectory()
        self.image_path = os.path.join(self._tmp.name, "test.png")
        with open(self.image_path, "wb") as fh:
            fh.write(PNG_BYTES)

    def tearDown(self):
        self._tmp.cleanup()
        api.app.dependency_overrides.pop(api.get_clients, None)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _override(self, vlm_content=chat_text(FAKE_VLM), llm_content=chat_text(FAKE_LLM),
                  vlm_error=None, llm_error=None):
        """注入带 FakeTransport 的 qwen 客户端, 返回两个 transport 供断言。"""
        vlm_tr = FakeTransport(content=vlm_content, error=vlm_error)
        llm_tr = FakeTransport(content=llm_content, error=llm_error)

        def _factory():
            return (VLMClient(provider="qwen", transport=vlm_tr),
                    LLMClient(provider="qwen", transport=llm_tr))

        api.app.dependency_overrides[api.get_clients] = _factory
        return vlm_tr, llm_tr

    def _client(self):
        return TestClient(api.app)


class GatewaySuccessTest(GatewayTestCase):
    def test_success_returns_both_outputs(self):
        self._override()
        resp = self._client().post("/api/v1/analyze",
                                   json={"image_path": self.image_path, "person_events": PERSONS})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertIsNone(body["error"])
        self.assertEqual(body["vlm_output"]["scene_type"], "collapse")
        self.assertEqual(body["llm_output"]["task_proposals"][0]["action"], "alert")

    def test_vlm_called_with_image(self):
        vlm_tr, _ = self._override()
        self._client().post("/api/v1/analyze",
                            json={"image_path": self.image_path, "person_events": PERSONS})
        self.assertEqual(len(vlm_tr.calls), 1)
        # 图片被编码为 data URL 发送给 qwen
        payload = vlm_tr.calls[0]["json"]
        img_url = payload["messages"][1]["content"][0]["image_url"]["url"]
        self.assertTrue(img_url.startswith("data:image/png;base64,"))

    def test_llm_receives_person_events(self):
        _, llm_tr = self._override()
        self._client().post("/api/v1/analyze",
                            json={"image_path": self.image_path, "person_events": PERSONS})
        user_content = llm_tr.calls[0]["json"]["messages"][1]["content"]
        self.assertIn("1", user_content)      # person_id
        self.assertIn("Fall", user_content)   # state

    def test_empty_person_events_ok(self):
        self._override()
        resp = self._client().post("/api/v1/analyze",
                                   json={"image_path": self.image_path, "person_events": []})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    def test_response_keys_consistent(self):
        self._override()
        body = self._client().post("/api/v1/analyze",
                                   json={"image_path": self.image_path, "person_events": PERSONS}).json()
        self.assertEqual(set(body.keys()), {"success", "vlm_output", "llm_output", "error"})


class GatewayRequestSchemaTest(GatewayTestCase):
    def test_missing_image_path_rejected(self):
        self._override()
        resp = self._client().post("/api/v1/analyze", json={"person_events": PERSONS})
        self.assertEqual(resp.status_code, 422)

    def test_missing_person_fields_rejected(self):
        self._override()
        resp = self._client().post("/api/v1/analyze",
                                   json={"image_path": self.image_path, "person_events": [{"person_id": 1}]})
        self.assertEqual(resp.status_code, 422)

    def test_wrong_image_type_rejected(self):
        self._override()
        resp = self._client().post("/api/v1/analyze",
                                   json={"image_path": 123, "person_events": []})
        self.assertEqual(resp.status_code, 422)


class GatewayErrorTest(GatewayTestCase):
    def test_image_not_found_yields_404(self):
        self._override()
        resp = self._client().post("/api/v1/analyze",
                                   json={"image_path": "Z:/no/such/file.png", "person_events": []})
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertIn("图片", body["error"] or "")

    def test_vlm_failure_yields_502(self):
        self._override(vlm_error=requests.ConnectionError("refused"))
        resp = self._client().post("/api/v1/analyze",
                                   json={"image_path": self.image_path, "person_events": PERSONS})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("VLM", resp.json()["error"] or "")

    def test_llm_failure_yields_502(self):
        self._override(llm_error=requests.Timeout("timed out"))
        resp = self._client().post("/api/v1/analyze",
                                   json={"image_path": self.image_path, "person_events": PERSONS})
        self.assertEqual(resp.status_code, 502)

    def test_vlm_parse_failure_yields_502(self):
        self._override(vlm_content="totally not json")
        resp = self._client().post("/api/v1/analyze",
                                   json={"image_path": self.image_path, "person_events": PERSONS})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("解析", resp.json()["error"] or "")

    def test_missing_vlm_key_yields_500(self):
        os.environ.pop("VLM_API_KEY", None)
        self._override()
        resp = self._client().post("/api/v1/analyze",
                                   json={"image_path": self.image_path, "person_events": PERSONS})
        self.assertEqual(resp.status_code, 500)
        self.assertIn("配置", resp.json()["error"] or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)