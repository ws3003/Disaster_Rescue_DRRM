"""P0-17 Communication 单元测试 (只连本地/注入 transport, 不依赖真实 Webhook)。

覆盖: MockChannel / WeComChannel / errcode 校验 / retry(initial+2) / 指数退避(1s,4s 注入) /
persistent pending 队列 / flush_pending 恢复(成功移除, 失败保留) / 缺 URL 显式出错并入队。
"""

import json
import os
import sys
import tempfile
import unittest

import requests

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.tools.channels import MockChannel, WeComChannel  # noqa: E402
from src.tools.communication_tool import CommunicationTool  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self.body = body if body is not None else {}
        self.text = text if text else str(body)

    def json(self):
        return self.body


class FakeTransport:
    """记录请求, 可编程返回响应序列或抛网络异常。"""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        if self.responses:
            if len(self.responses) > 1:
                return self.responses.pop(0)
            return self.responses[0]
        return FakeResponse()


class SleepCollector:
    def __init__(self):
        self.delays = []

    def __call__(self, delay):
        self.delays.append(delay)


def ok_resp(msgid="wecom-1"):
    return FakeResponse(200, {"errcode": 0, "msgid": msgid})


class BaseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self._tmp.name, "comm.log.jsonl")
        self.pending = os.path.join(self._tmp.name, "pending.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, channel="mock", webhook_url=None, transport=None,
             sleep=None, **kw):
        return CommunicationTool(
            log_path=self.log, pending_path=self.pending,
            channel=channel, webhook_url=webhook_url,
            transport=transport, sleep_fn=sleep,
            max_retries=kw.pop("max_retries", 2), backoff=kw.pop("backoff", (1.0, 4.0)),
            **kw)

    def pending_entries(self):
        if not os.path.exists(self.pending):
            return []
        out = []
        for line in open(self.pending, encoding="utf-8"):
            if line.strip():
                out.append(json.loads(line))
        return out


class MockChannelTest(BaseCase):
    def test_send_markdown_success(self):
        ch = MockChannel(log_path=self.log)
        r = ch.send_markdown("## hi")
        self.assertTrue(r["ok"])
        self.assertEqual(r["channel"], "LOCAL_MOCK")
        self.assertTrue(r.get("msgid"))
        self.assertTrue(ch.verify())

    def test_send_image_success(self):
        img = os.path.join(self._tmp.name, "a.jpg")
        with open(img, "wb") as fh:
            fh.write(b"\xff\xd8\xff fake")
        ch = MockChannel(log_path=self.log)
        r = ch.send_image(img)
        self.assertTrue(r["ok"])
        self.assertEqual(r["channel"], "LOCAL_MOCK")


class WeComChannelTest(BaseCase):
    def test_http200_errcode0_success(self):
        tr = FakeTransport(responses=[FakeResponse(200, {"errcode": 0, "msgid": "m1"})])
        ch = WeComChannel(webhook_url="https://fake/webhook", transport=tr)
        r = ch.send_markdown("hi")
        self.assertTrue(r["ok"])
        self.assertEqual(r["msgid"], "m1")
        self.assertEqual(tr.calls[0]["json"]["msgtype"], "markdown")

    def test_http200_errcode_nonzero_failure(self):
        tr = FakeTransport(responses=[FakeResponse(200, {"errcode": 93000, "errmsg": "invalid key"})])
        ch = WeComChannel(webhook_url="https://fake/webhook", transport=tr)
        r = ch.send_markdown("hi")
        self.assertFalse(r["ok"])
        self.assertEqual(r["errcode"], 93000)
        self.assertTrue(r["transient"])


class RetryBackoffTest(BaseCase):
    def _net_fail_tool(self):
        self.sleeper = SleepCollector()
        self.tr = FakeTransport(error=requests.ConnectionError("down"))
        return self.tool(channel="wecom", webhook_url="https://fake", transport=self.tr,
                         sleep=self.sleeper)

    def test_network_failure_retries_3_times_then_pending(self):
        tool = self._net_fail_tool()
        r = tool.run(action="send_alert", severity="HIGH", title="t", message="m",
                     person_id=1)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("queued"))
        self.assertEqual(len(self.tr.calls), 3)      # 首次 + 2 次重试
        self.assertEqual(len(self.pending_entries()), 1)

    def test_backoff_delays_are_1s_and_4s(self):
        self._net_fail_tool().run(action="send_alert", severity="HIGH",
                                  title="t", message="m")
        self.assertEqual(self.sleeper.delays, [1.0, 4.0])

    def test_retry_attempts_total_is_three(self):
        tool = self._net_fail_tool()
        tool.run(action="send_alert", severity="HIGH", title="t", message="m")
        self.assertEqual(len(self.tr.calls), 3)


class PendingQueueTest(BaseCase):
    def test_pending_is_persisted(self):
        tool = self.tool(channel="wecom", webhook_url="https://fake",
                         transport=FakeTransport(error=requests.ConnectionError("down")),
                         sleep=lambda _: None)
        tool.run(action="send_alert", severity="HIGH", title="t", message="m")
        entries = self.pending_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "pending")

    def test_flush_success_removes_pending(self):
        tool = self.tool(channel="wecom", webhook_url="https://fake",
                         transport=FakeTransport(error=requests.ConnectionError("down")),
                         sleep=lambda _: None)
        tool.run(action="send_alert", severity="HIGH", title="t", message="m")
        self.assertEqual(len(self.pending_entries()), 1)

        # 恢复后用成功 transport 的另一个 tool 执行补发(读同一 pending 文件)
        ok_tool = self.tool(channel="wecom", webhook_url="https://fake",
                            transport=FakeTransport(responses=[ok_resp()]),
                            sleep=lambda _: None)
        res = ok_tool.run(action="flush_pending")
        self.assertEqual(res["flushed"], 1)
        self.assertEqual(res["remaining"], 0)
        self.assertEqual(self.pending_entries(), [])

    def test_flush_failure_remains_pending(self):
        tool = self.tool(channel="wecom", webhook_url="https://fake",
                         transport=FakeTransport(error=requests.ConnectionError("down")),
                         sleep=lambda _: None)
        tool.run(action="send_alert", severity="HIGH", title="t", message="m")
        res = tool.run(action="flush_pending")
        self.assertEqual(res["flushed"], 0)
        self.assertGreaterEqual(res["remaining"], 1)
        self.assertEqual(len(self.pending_entries()), 1)


class MissingUrlTest(BaseCase):
    def test_wecom_missing_url_explicit_failure_and_pending(self):
        tr = FakeTransport()
        tool = self.tool(channel="wecom", webhook_url="", transport=tr, sleep=lambda _: None)
        r = tool.run(action="send_alert", severity="HIGH", title="t", message="m")
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("queued"))
        self.assertIn("WECOM_WEBHOOK_URL", r.get("error", ""))
        self.assertEqual(len(tr.calls), 0)          # 缺 URL 不发起 POST、不重试
        self.assertEqual(len(self.pending_entries()), 1)


class ContractTest(BaseCase):
    def test_default_mock_send_alert_ok_contract(self):
        tool = self.tool()  # 默认 mock 渠道
        r = tool.run(action="send_alert", severity="HIGH", title="High-Risk",
                     message="fallen", person_id=1)
        self.assertTrue(r["ok"])
        self.assertEqual(r["channel"], "LOCAL_MOCK")
        self.assertTrue(r.get("msgid"))
        self.assertEqual(r["status"], "SENT")

    def test_invalid_action(self):
        tool = self.tool()
        r = tool.run(action="nope")
        self.assertFalse(r["ok"])

    def test_missing_title(self):
        tool = self.tool()
        r = tool.run(action="send_alert", message="m")
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)