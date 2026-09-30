"""V4.2 Voice 测试 (全部 mock, 无真实麦克风/ASR/TTS/网络)。

覆盖:
  1. VoiceTool mock ASR
  2. VoiceTool mock TTS
  3. VoiceAssistant 完整链路 (注入 fake asr/tts; 复用 AssistantResponder)
  4. 错误处理: ASR 失败 / TTS 失败
  5. live: 无环境变量自动 skip
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.voice_tool import VoiceTool  # noqa: E402
from src.assistant.voice import VoiceAssistant  # noqa: E402
from src.assistant.responder import AssistantResponder  # noqa: E402
from src.assistant.context import AssistantContext  # noqa: E402

_ASR_LIVE = os.environ.get("VOICE_ASR_PROVIDER", "").strip().lower() == "live" \
    and bool(os.environ.get("ASR_API_KEY", "").strip())
_TTS_LIVE = os.environ.get("VOICE_TTS_PROVIDER", "").strip().lower() == "live" \
    and bool(os.environ.get("TTS_API_KEY", "").strip())


def _ctx(db, snap_dir, vision_path):
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE IF NOT EXISTS tasks ("
                  "task_id TEXT PRIMARY KEY, trigger_event TEXT, plan_trace TEXT, "
                  "status TEXT, created TEXT, finished TEXT)")
    return AssistantContext(db_path=db, snapshot_dir=snap_dir, vision_path=vision_path)


class FakeAsr:
    def __init__(self, ok=True, text="现场有几个人？"):
        self.ok = ok
        self.text = text
        self.calls = 0

    def __call__(self, audio_path):
        self.calls += 1
        return {"ok": self.ok, "text": self.text}


class FakeTts:
    def __init__(self, ok=True, url="mock://voice/out.mp3"):
        self.ok = ok
        self.url = url
        self.calls = 0

    def __call__(self, answer):
        self.calls += 1
        return {"ok": self.ok, "audio_url": self.url}


class VoiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "v.db")
        self.snap_dir = os.path.join(self._tmp.name, "snap")
        self.vision = os.path.join(self._tmp.name, "vision.json")
        os.makedirs(self.snap_dir, exist_ok=True)
        Path(self.vision).write_text("{}", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    # 1. mock ASR
    def test_mock_asr(self):
        t = VoiceTool(provider="mock")
        r = t.run(action="asr", audio_path="demo.wav")
        self.assertTrue(r["ok"])
        self.assertTrue(r["text"])

    # 2. mock TTS
    def test_mock_tts(self):
        t = VoiceTool(provider="mock")
        r = t.run(action="tts", text="当前天气晴")
        self.assertTrue(r["ok"])
        self.assertTrue(r["audio_url"].startswith("mock://"))

    # 3. VoiceAssistant 完整链路 (fake asr/tts + 既有 responder)
    def test_voice_full_chain(self):
        asr = FakeAsr()
        tts = FakeTts()
        resp = AssistantResponder(context=_ctx(self.db, self.snap_dir, self.vision))
        va = VoiceAssistant(asr=asr, tts=tts, responder=resp)
        out = va.handle(audio_path="fake.wav")
        self.assertEqual(out["question"], "现场有几个人？")
        self.assertTrue(out["answer"])       # responder 已给出回答
        self.assertEqual(out["audio_url"], "mock://voice/out.mp3")
        self.assertEqual(asr.calls, 1)
        self.assertEqual(tts.calls, 1)

    # 4a. ASR 失败
    def test_asr_failure(self):
        asr = FakeAsr(ok=False)
        tts = FakeTts()
        resp = AssistantResponder(context=_ctx(self.db, self.snap_dir, self.vision))
        va = VoiceAssistant(asr=asr, tts=tts, responder=resp)
        out = va.handle(audio_path="fake.wav")
        self.assertIsNone(out["question"])
        self.assertEqual(out["answer"], "")
        self.assertEqual(tts.calls, 0)  # ASR 失败不触发 TTS

    # 4b. TTS 失败: 仍有 answer, 无 audio_url
    def test_tts_failure(self):
        asr = FakeAsr()
        tts = FakeTts(ok=False)
        resp = AssistantResponder(context=_ctx(self.db, self.snap_dir, self.vision))
        va = VoiceAssistant(asr=asr, tts=tts, responder=resp)
        out = va.handle(audio_path="fake.wav")
        self.assertTrue(out["answer"])
        self.assertIsNone(out["audio_url"])

    # 5a. live ASR skip (无 env)
    @unittest.skipUnless(_ASR_LIVE, "需 VOICE_ASR_PROVIDER=live + ASR_API_KEY")
    def test_live_asr(self):
        t = VoiceTool(provider="live")
        r = t.run(action="asr", audio_path="demo.wav")
        self.assertEqual(r["provider"], "live")

    # 5b. live TTS skip (无 env)
    @unittest.skipUnless(_TTS_LIVE, "需 VOICE_TTS_PROVIDER=live + TTS_API_KEY")
    def test_live_tts(self):
        t = VoiceTool(provider="live")
        r = t.run(action="tts", text="天气")
        self.assertEqual(r["provider"], "live")


if __name__ == "__main__":
    unittest.main(verbosity=2)