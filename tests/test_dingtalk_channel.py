"""DingTalk robot channel tests (inject transport, never call real webhook).

Cover:
  - DingTalkChannel env / constructor webhook URL reading
  - markdown payload generation contains keyword "灾害报警" (DingTalk security)
  - HTTP success handling (errcode 0)
  - HTTP failure / network error -> transient, no crash
  - CommunicationTool channel selection via COMMUNICATION_CHANNEL=dingtalk
  - missing DINGTALK_WEBHOOK -> explicit failure, queued (not silent fallback)
"""

import json
import os
import sys
import tempfile
import unittest

import requests

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.tools.channels import DingTalkChannel  # noqa: E402
from src.tools.communication_tool import CommunicationTool  # noqa: E402

_DT_URL = "https://oapi.dingtalk.com/robot/send?access_token=fake"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = body if body is not None else {}
        self.text = str(body)

    def json(self):
        return self.body


class FakeTransport:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        if self.responses:
            resp = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
            return resp
        return FakeResponse()


class DingTalkChannelTest(unittest.TestCase):
    def test_env_webhook_read(self):
        old = os.environ.get("DINGTALK_WEBHOOK")
        os.environ["DINGTALK_WEBHOOK"] = _DT_URL
        old_comm = os.environ.get("COMMUNICATION_CHANNEL")
        os.environ["COMMUNICATION_CHANNEL"] = "dingtalk"
        try:
            tool = CommunicationTool()
            self.assertEqual(tool._channel_name, "dingtalk")
            self.assertEqual(tool._webhook_url, _DT_URL)
            ch = tool._channel()
            self.assertIsInstance(ch, DingTalkChannel)
            self.assertTrue(ch.verify())
        finally:
            if old is None:
                os.environ.pop("DINGTALK_WEBHOOK", None)
            else:
                os.environ["DINGTALK_WEBHOOK"] = old
            if old_comm is None:
                os.environ.pop("COMMUNICATION_CHANNEL", None)
            else:
                os.environ["COMMUNICATION_CHANNEL"] = old_comm

    def test_constructor_webhook_takes_precedence(self):
        ch = DingTalkChannel(webhook_url=_DT_URL, transport=FakeTransport())
        self.assertEqual(ch.webhook_url, _DT_URL)
        self.assertTrue(ch.verify())

    def test_markdown_contains_keyword(self):
        md = CommunicationTool._build_dingtalk_markdown({
            "person_id": 7,
            "extra": {"state": "Fall", "risk_score": 0.92},
            "severity": "HIGH",
            "priority": "HIGH",
            "timestamp": "2026-09-27T00:00:00",
        })
        first_line = md.splitlines()[0]
        self.assertIn("灾害报警", first_line, "钉钉首行必须含安全关键词 灾害报警")
        self.assertIn("人员ID:", md)
        self.assertIn("7", md)
        self.assertIn("状态:", md)
        self.assertIn("Fall", md)
        self.assertIn("风险等级:", md)
        self.assertIn("HIGH", md)
        self.assertIn("风险分数:", md)
        self.assertIn("0.92", md)
        self.assertIn("Raspberry Pi Edge AI", md)

    def test_send_markdown_success(self):
        tr = FakeTransport(responses=[FakeResponse(200, {"errcode": 0})])
        ch = DingTalkChannel(webhook_url=_DT_URL, transport=tr)
        res = ch.send_markdown("## 🚨 灾害报警\n人员ID: 1")
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("channel"), "DINGTALK")
        self.assertEqual(len(tr.calls), 1)
        payload = tr.calls[0]["json"]
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertIn("灾害报警", payload["markdown"]["text"])

    def test_send_markdown_failure_no_crash(self):
        tr = FakeTransport(responses=[FakeResponse(200, {"errcode": 310000, "errmsg": "keywords not in content"})])
        ch = DingTalkChannel(webhook_url=_DT_URL, transport=tr)
        res = ch.send_markdown("no keyword")
        self.assertFalse(res.get("ok"))
        self.assertIs(res.get("transient"), True)
        self.assertEqual(res.get("errcode"), 310000)

    def test_network_error_transient(self):
        tr = FakeTransport(error=requests.ConnectionError("down"))
        ch = DingTalkChannel(webhook_url=_DT_URL, transport=tr)
        res = ch.send_markdown("x")
        self.assertFalse(res.get("ok"))
        self.assertIs(res.get("transient"), True)

    def test_missing_url_explicit_error(self):
        ch = DingTalkChannel(webhook_url="", transport=FakeTransport())
        self.assertFalse(ch.verify())
        tmp = tempfile.TemporaryDirectory()
        try:
            tool = CommunicationTool(channel="dingtalk", webhook_url="",
                                     transport=FakeTransport(),
                                     pending_path=os.path.join(tmp.name, "p.jsonl"),
                                     log_path=os.path.join(tmp.name, "c.log"))
            r = tool.run(action="send_alert", title="t", message="m", person_id=1,
                         extra={"state": "Fall", "risk_score": 0.9})
            self.assertFalse(r.get("ok"))
            self.assertTrue(r.get("queued"))
            self.assertIn("DINGTALK_WEBHOOK", r.get("error", ""))
        finally:
            tmp.cleanup()

    def test_dingtalk_through_tool_success(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            tr = FakeTransport(responses=[FakeResponse(200, {"errcode": 0})])
            tool = CommunicationTool(channel="dingtalk", webhook_url=_DT_URL,
                                     transport=tr,
                                     pending_path=os.path.join(tmp.name, "p.jsonl"),
                                     log_path=os.path.join(tmp.name, "c.log"))
            r = tool.run(action="send_alert", title="High-Risk Personnel Alert",
                         message="Person 1 | Fall", person_id=1,
                         extra={"state": "Fall", "risk_score": 0.92, "priority": "HIGH"})
            self.assertTrue(r.get("ok"))
            self.assertEqual(len(tr.calls), 1)
            self.assertEqual(tr.calls[0]["url"], _DT_URL)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)