"""V4.2 DashScope CosyVoice TTS 测试。

- 主用 mock transport(无网络): 返回 output.audio(base64) → provider=aliyun + audio_url 落盘。
- live 分支(有 TTS_API_KEY): skipUnless 保护。
"""

import base64
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.voice_tool import VoiceTool  # noqa: E402

_LIVE = bool(os.environ.get("TTS_API_KEY", "").strip())


class _FakeResp:
    status_code = 200
    headers = {"Content-Type": "application/json"}
    text = ""

    def json(self):
        return {"output": {"audio": base64.b64encode(b"ID3qwen-audio-tts").decode("ascii")}}

    def raise_for_status(self):
        return None


class TtsLiveTest(unittest.TestCase):
    def test_mock_tts_aliyun(self):
        with patch("requests.post", return_value=_FakeResp()) as mp, \
             patch.dict(os.environ, {"TTS_API_KEY": "k"}):
            v = VoiceTool(provider="live")
            r = v.text_to_audio("测试语音")
        self.assertTrue(r["ok"])

        self.assertEqual(r["provider"], "aliyun")
        self.assertTrue(r["audio_url"].endswith(".mp3"))
        Path(r["audio_url"]).unlink(missing_ok=True)  # 清理落盘

    @unittest.skipUnless(_LIVE, "需 TTS_API_KEY")
    def test_live_tts(self):
        v = VoiceTool(provider="live")
        r = v.text_to_audio("测试语音")
        self.assertTrue(r["ok"], msg=f"live TTS 失败: {r.get('error')}")
        self.assertEqual(r["provider"], "aliyun")
        self.assertTrue(r["audio_url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)