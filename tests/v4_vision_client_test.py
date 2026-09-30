"""V4.1 Phase7.3 VisionClient 测试 (mock transport, 离线, 不依赖网络/Pi/摄像头).

验证:
  1. 正常上传: POST 被调用, JSON 含 persons, 字段正确, 返回 ok:true
  2. 网络失败: transport 抛异常, 返回 {ok:false}, 不 raise
  3. 无 URL: 不发 HTTP, 返回 {ok:false}
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.vision_client import VisionClient  # noqa: E402


class FakeTransport:
    def __init__(self, status_code=200, error=None):
        self.status_code = status_code
        self.error = error
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return type("Resp", (), {"status_code": self.status_code})()


class VisionClientTest(unittest.TestCase):

    PERSON = [{"person_id": 1, "state": "Fall", "risk_score": 0.9, "priority": "HIGH",
               "bbox": [100, 100, 300, 400]}]

    # 1. 正常上传
    def test_upload_success_payload(self):
        ft = FakeTransport(status_code=200)
        c = VisionClient(snapshot_url="http://s/api/v1/vision/snapshot", transport=ft, timeout=3)
        r = c.upload(self.PERSON)
        self.assertTrue(r["ok"])
        self.assertEqual(len(ft.calls), 1)
        call = ft.calls[0]
        self.assertEqual(call["url"], "http://s/api/v1/vision/snapshot")
        payload = call["json"]
        self.assertIn("persons", payload)
        p0 = payload["persons"][0]
        self.assertEqual(p0["person_id"], 1)
        self.assertEqual(p0["state"], "Fall")
        self.assertEqual(p0["priority"], "HIGH")

    # 2. 网络失败不异常
    def test_network_failure_returns_ok_false(self):
        ft = FakeTransport(error=ConnectionError("boom"))
        c = VisionClient(snapshot_url="http://s/vis", transport=ft)
        r = c.upload(self.PERSON)
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    # 3. server 非 2xx → ok:false, 不异常
    def test_server_error_returns_ok_false(self):
        ft = FakeTransport(status_code=500)
        c = VisionClient(snapshot_url="http://s/vis", transport=ft)
        r = c.upload(self.PERSON)
        self.assertFalse(r["ok"])

    # 4. 无 URL: 不发 HTTP
    def test_no_url_skips_http(self):
        ft = FakeTransport(status_code=200)
        c = VisionClient(snapshot_url=None, transport=ft)
        r = c.upload(self.PERSON)
        self.assertFalse(r["ok"])
        self.assertEqual(len(ft.calls), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)