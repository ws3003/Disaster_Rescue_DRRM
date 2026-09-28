"""API Gateway 上传专项测试 (Stage7-A, tests/p1): multipart 上传 + 临时文件清理。

复用 FakeTransport 注入, 全程不调用真实 Qwen API、不需要真实 Key。
"""

import base64
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

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"padding" * 8
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"padding" * 8

FAKE_VLM = {
    "scene_type": "fire",
    "overall_severity": "high",
    "hazards": [],
    "environment_note": "upload 测试假环境",
}
FAKE_LLM = {
    "event_summary": "upload 假推理",
    "risk_explanations": [],
    "rescue_suggestions": [],
    "task_proposals": [{"action": "log", "target": None, "payload": {}}],
}

PERSONS_JSON = json.dumps(
    [{"person_id": 7, "state": "Lying", "risk_score": 0.9, "priority": "HIGH"}],
    ensure_ascii=False,
)


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
        self.text = ""

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


class UploadTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("VLM_API_KEY", "LLM_API_KEY", "VLM_PROVIDER", "LLM_PROVIDER"):
            self._saved[key] = os.environ.get(key)
        os.environ["VLM_API_KEY"] = "vlm"
        os.environ["LLM_API_KEY"] = "llm"
        os.environ.pop("VLM_PROVIDER", None)
        os.environ.pop("LLM_PROVIDER", None)

    def tearDown(self):
        api.app.dependency_overrides.pop(api.get_clients, None)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _override(self, vlm_content=json.dumps(FAKE_VLM, ensure_ascii=False),
                  llm_content=json.dumps(FAKE_LLM, ensure_ascii=False),
                  vlm_error=None, llm_error=None):
        vlm_tr = FakeTransport(content=vlm_content, error=vlm_error)
        llm_tr = FakeTransport(content=llm_content, error=llm_error)

        def _factory():
            return (VLMClient(provider="qwen", transport=vlm_tr),
                    LLMClient(provider="qwen", transport=llm_tr))

        api.app.dependency_overrides[api.get_clients] = _factory
        return vlm_tr, llm_tr

    def _post(self, image=JPEG_BYTES, filename="frame.jpg", persons=PERSONS_JSON):
        return TestClient(api.app).post(
            "/api/v1/analyze",
            files={"image": (filename, image, "application/octet-stream")},
            data={"person_events": persons} if persons is not None else {},
        )

    def _temp_files(self):
        upload_dir = api._UPLOAD_DIR
        if not os.path.isdir(upload_dir):
            return []
        return [f for f in os.listdir(upload_dir) if f.startswith("tmp_")]


class UploadSuccessTest(UploadTestCase):
    def test_upload_jpg_success(self):
        self._override()
        resp = self._post(image=JPEG_BYTES, filename="frame.jpg")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    def test_upload_png_success(self):
        self._override()
        resp = self._post(image=PNG_BYTES, filename="frame.png")
        self.assertEqual(resp.status_code, 200)

    def test_person_events_parsed(self):
        self._override()
        resp = self._post(persons=PERSONS_JSON)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    def test_vlm_receives_uploaded_image_bytes(self):
        """VLM 收到的 data URL 解码后应与上传字节一致 (证明读的是临时文件)。"""
        vlm_tr, _ = self._override()
        self._post(image=JPEG_BYTES)
        img_url = vlm_tr.calls[0]["json"]["messages"][1]["content"][0]["image_url"]["url"]
        self.assertTrue(img_url.startswith("data:image/jpeg;base64,"))  # 按内容魔数识别的 MIME
        payload_b64 = img_url.split(",", 1)[1]
        self.assertEqual(base64.b64decode(payload_b64), JPEG_BYTES)

    def test_llm_receives_person_events(self):
        _, llm_tr = self._override()
        self._post(persons=PERSONS_JSON)
        user_content = llm_tr.calls[0]["json"]["messages"][1]["content"]
        self.assertIn("7", user_content)         # person_id
        self.assertIn("Lying", user_content)     # state
        self.assertIn("0.9", user_content)       # risk_score

    def test_temp_file_cleaned_after_success(self):
        self._override()
        self.assertEqual(self._temp_files(), [])
        self._post()
        # 请求结束后临时文件应被删除
        self.assertEqual(self._temp_files(), [])


class UploadErrorTest(UploadTestCase):
    def test_empty_image_400(self):
        self._override()
        resp = self._post(image=b"")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("图片", resp.json()["error"] or "")

    def test_non_image_400(self):
        self._override()
        resp = self._post(image=b"<html>not an image</html>")
        self.assertEqual(resp.status_code, 400)

    def test_bad_json_422(self):
        self._override()
        resp = self._post(persons="{oops")
        self.assertEqual(resp.status_code, 422)

    def test_model_error_502(self):
        self._override(vlm_error=requests.ConnectionError("down"))
        resp = self._post()
        self.assertEqual(resp.status_code, 502)

    def test_temp_file_cleaned_after_error(self):
        self._override(vlm_error=requests.ConnectionError("down"))
        self.assertEqual(self._temp_files(), [])
        self._post()  # 虽 502, 但临时文件也应清理
        self.assertEqual(self._temp_files(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)