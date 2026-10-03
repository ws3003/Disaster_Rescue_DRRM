"""V4.7 私网图片 URL 下载→OSS 上传测试(离线)。

输入 http://192.168.43.206:8000/snapshots/test.jpg
期望: 下载到本地 → upload_snapshot 得 OSS HTTPS URL → DingTalk 发送(image_sent=True)
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools import communication_tool as ct  # noqa: E402

PRIVATE = "http://192.168.43.206:8000/snapshots/private_test.jpg"
OSS_URL = "https://disaster-rescue-images.oss-cn-hangzhou.aliyuncs.com/snapshots/private_test.jpg"


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass


class PrivateImageTest(unittest.TestCase):
    def setUp(self):
        self.tool = ct.CommunicationTool()
        self._snap = Path(ROOT) / "data" / "snapshots" / "private_test.jpg"
        if self._snap.exists():
            self._snap.unlink()

    def tearDown(self):
        if self._snap.exists():
            self._snap.unlink()

    def test_resolve_downloads_then_uploads(self):
        fake_bytes = b"\xff\xd8fakejpg"
        with mock.patch("requests.get", return_value=_Resp()) as m:
            _Resp.content = fake_bytes
            with mock.patch("src.storage.upload_snapshot",
                            return_value={"ok": True, "url": OSS_URL}) as up:
                url = self.tool._resolve_public_image(PRIVATE)
        self.assertEqual(url, OSS_URL)
        m.assert_called_once_with(PRIVATE, timeout=15)
        up.assert_called_once()   # 下载后确实上传 OSS
        self.assertTrue(self._snap.exists())  # 已落本地

    def test_send_alert_image_sent(self):
        class _FakeChannel:
            name = "dingtalk"

            def verify(self):
                return True

        with mock.patch("requests.get", return_value=_Resp()):
            _Resp.content = b"\xff\xd8fake"
            with mock.patch("src.storage.upload_snapshot",
                            return_value={"ok": True, "url": OSS_URL}):
                with mock.patch.object(self.tool, "_channel", return_value=_FakeChannel()):
                    with mock.patch.object(self.tool, "_send_with_retry",
                                           return_value={"ok": True}):
                        out = self.tool.run(
                            action="send_alert",
                            title="t", message="fall", person_id=1,
                            severity="HIGH", image_path=PRIVATE)
        self.assertTrue(out["image_sent"])
        self.assertEqual(out["image_url"], OSS_URL)


if __name__ == "__main__":
    unittest.main(verbosity=2)