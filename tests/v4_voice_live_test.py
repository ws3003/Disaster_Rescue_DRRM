"""V4.1 Phase6 语音 live provider 测试 (可选, 默认 skip)。

ASR 仅在 VOICE_ASR_PROVIDER=live 且 ASR_API_KEY 存在时运行;
TTS 仅在 VOICE_TTS_PROVIDER=live 且 TTS_API_KEY 存在时运行。
无凭据/离线环境全部 skip, 不影响回归。
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.voice_tool import VoiceTool  # noqa: E402

_ASR_LIVE = os.environ.get("VOICE_ASR_PROVIDER", "").strip().lower() == "live" \
    and bool(os.environ.get("ASR_API_KEY", "").strip())
_TTS_LIVE = os.environ.get("VOICE_TTS_PROVIDER", "").strip().lower() == "live" \
    and bool(os.environ.get("TTS_API_KEY", "").strip())


class VoiceAsrLiveTest(unittest.TestCase):
    @unittest.skipUnless(_ASR_LIVE, "需 VOICE_ASR_PROVIDER=live + ASR_API_KEY")
    def test_live_asr(self):
        t = VoiceTool(provider="live")
        r = t.run(action="asr", audio_path="demo.wav")
        self.assertTrue(r["ok"], msg=f"live ASR 失败: {r.get('error')}")
        self.assertEqual(r["provider"], "live")
        self.assertIn("text", r)


class VoiceTtsLiveTest(unittest.TestCase):
    @unittest.skipUnless(_TTS_LIVE, "需 VOICE_TTS_PROVIDER=live + TTS_API_KEY")
    def test_live_tts(self):
        t = VoiceTool(provider="live")
        r = t.run(action="tts", text="当前天气晴，温度25℃")
        self.assertTrue(r["ok"], msg=f"live TTS 失败: {r.get('error')}")
        self.assertEqual(r["provider"], "live")
        self.assertIn("audio_url", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)