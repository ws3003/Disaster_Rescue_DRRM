"""API Gateway 测试 (Stage7-A, tests/p1): multipart 上传 + FakeTransport 注入。

- 不调用真实 Qwen API、不需要真实 Key;
- 通过 app.dependency_overrides[get_clients] 注入带 FakeTransport 的客户端;
- 请求用 multipart: files={"image": ...}, data={"person_events": "..."}.
"""

import json
import os
import sys
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
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"0" * 16

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
PERSONS_JSON = json.dumps(PERSONS, ensure_ascii=False)


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
        for key in ("VLM_API_KEY", "LLM_API_KEY", "VLM_PROVIDER", "LLM_PROVIDER"):
            self._saved_env[key] = os.environ.get(key)
        os.environ["VLM_API_KEY"] = "vlm"
        os.environ["LLM_API_KEY"] = "llm"
        os.environ.pop("VLM_PROVIDER", None)
        os.environ.pop("LLM_PROVIDER", None)

    def tearDown(self):
        api.app.dependency_overrides.pop(api.get_clients, None)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _override(self, vlm_content=chat_text(FAKE_VLM), llm_content=chat_text(FAKE_LLM),
                  vlm_error=None, llm_error=None):
        vlm_tr = FakeTransport(content=vlm_content, error=vlm_error)
        llm_tr = FakeTransport(content=llm_content, error=llm_error)

        def _factory():
            return (VLMClient(provider="qwen", transport=vlm_tr),
                    LLMClient(provider="qwen", transport=llm_tr))

        api.app.dependency_overrides[api.get_clients] = _factory
        return vlm_tr, llm_tr

    def _client(self):
        return TestClient(api.app)

    def _post(self, image=PNG_BYTES, filename="frame.png", persons=PERSONS_JSON):
        files = {"image": (filename, image, "application/octet-stream")}
        data = {"person_events": persons} if persons is not None else {}
        return self._client().post("/api/v1/analyze", files=files, data=data)


class GatewaySuccessTest(GatewayTestCase):
    def test_success_returns_both_outputs(self):
        self._override()
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertIsNone(body["error"])
        self.assertEqual(body["vlm_output"]["scene_type"], "collapse")
        # Phase2-B authoritative contract: action 对齐 B 词表(alert→send_alert), payload 剥离事实, 顶层含 observation/persons
        self.assertEqual(body["llm_output"]["task_proposals"][0]["action"], "send_alert")
        self.assertEqual(body["llm_output"]["contract_version"], "0.1-observation-v1")
        self.assertIn("observation", body["llm_output"])
        self.assertIn("persons", body["llm_output"])

    def test_vlm_receives_real_temp_image(self):
        vlm_tr, _ = self._override()
        self._post()
        self.assertEqual(len(vlm_tr.calls), 1)
        payload = vlm_tr.calls[0]["json"]
        img_url = payload["messages"][1]["content"][0]["image_url"]["url"]
        self.assertTrue(img_url.startswith("data:image/png;base64,"))

    def test_llm_receives_person_events(self):
        _, llm_tr = self._override()
        self._post(persons=PERSONS_JSON)
        user_content = llm_tr.calls[0]["json"]["messages"][1]["content"]
        self.assertIn("1", user_content)        # person_id
        self.assertIn("Fall", user_content)     # state

    def test_empty_person_events_ok(self):
        self._override()
        resp = self._post(persons=None)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    def test_response_keys_consistent(self):
        self._override()
        body = self._post().json()
        self.assertEqual(set(body.keys()),
                         {"success", "vlm_output", "llm_output", "error", "snapshot_url"})


class GatewayRequestSchemaTest(GatewayTestCase):
    def test_missing_image_rejected(self):
        self._override()
        resp = self._client().post("/api/v1/analyze", data={"person_events": PERSONS_JSON})
        self.assertEqual(resp.status_code, 422)  # FastAPI 自动校验必填 File

    def test_bad_person_json_rejected(self):
        self._override()
        resp = self._post(persons="not json{")
        self.assertEqual(resp.status_code, 422)

    def test_person_not_array_rejected(self):
        self._override()
        resp = self._post(persons='{"person_id":1}')
        self.assertEqual(resp.status_code, 422)

    def test_person_bad_fields_rejected(self):
        self._override()
        resp = self._post(persons=json.dumps([{"person_id": 1}], ensure_ascii=False))
        self.assertEqual(resp.status_code, 422)


class GatewayErrorTest(GatewayTestCase):
    def test_empty_image_yields_400(self):
        self._override()
        resp = self._post(image=b"")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["success"])

    def test_non_image_yields_400(self):
        self._override()
        resp = self._post(image=b"plain text not an image")
        self.assertEqual(resp.status_code, 400)

    def test_vlm_failure_yields_502(self):
        self._override(vlm_error=requests.ConnectionError("refused"))
        resp = self._post()
        self.assertEqual(resp.status_code, 502)
        self.assertIn("VLM", resp.json()["error"] or "")

    def test_llm_failure_yields_502(self):
        self._override(llm_error=requests.Timeout("timed out"))
        resp = self._post()
        self.assertEqual(resp.status_code, 502)

    def test_vlm_parse_failure_yields_502(self):
        self._override(vlm_content="totally not json")
        resp = self._post()
        self.assertEqual(resp.status_code, 502)
        self.assertIn("解析", resp.json()["error"] or "")

    def test_missing_vlm_key_yields_500(self):
        os.environ.pop("VLM_API_KEY", None)
        self._override()
        resp = self._post()
        self.assertEqual(resp.status_code, 500)
        self.assertIn("配置", resp.json()["error"] or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)