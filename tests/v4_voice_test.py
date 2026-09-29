"""V4.1 Phase 4 语音交互测试 (mock, 离线)。

验证:
  1. ASR mock 返回文本
  2. voice -> assistant -> answer
  3. TTS 返回 audio_url
  4. assistant 不创建 task
  5. assistant 不调用 communication_tool / rescue_tool
  6. 不回归既有测试(由外部一起跑)
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.voice_tool import VoiceTool  # noqa: E402
from src.assistant.voice import VoiceAssistant  # noqa: E402
from src.assistant.responder import AssistantResponder  # noqa: E402


class VoiceTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "v.db")
        self.snap_dir = os.path.join(self._tmp.name, "snapshots")
        os.makedirs(self.snap_dir, exist_ok=True)
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS tasks ("
                      "task_id TEXT PRIMARY KEY, trigger_event TEXT, plan_trace TEXT, "
                      "status TEXT, created TEXT, finished TEXT)")
        Path(self.snap_dir, "vc_20260929.jpg").write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    def _mk_voice(self):
        from src.assistant.context import AssistantContext
        ctx = AssistantContext(db_path=self.db, snapshot_dir=self.snap_dir)
        return VoiceAssistant(responder=AssistantResponder(context=ctx))

    # 1. ASR mock 返回文本
    def test_asr_returns_text(self):
        t = VoiceTool()
        r = t.run(action="asr", audio_path="demo.wav")
        self.assertTrue(r["ok"])
        self.assertTrue(r["text"])

    # 2. voice -> assistant -> answer
    def test_voice_to_answer(self):
        va = self._mk_voice()
        out = va.ask_text("现场情况怎么样")
        self.assertTrue(out["answer"])
        self.assertEqual(out["question"], "现场情况怎么样")

    def test_voice_handle_audio_uses_asr(self):
        va = self._mk_voice()
        audio = os.path.join(self._tmp.name, "in.wav")
        Path(audio).write_bytes(b"fake-wave")
        out = va.handle(audio_path=audio)
        # mock ASR 返回默认文本 → 走应答; audio_url 由 TTS 生成
        self.assertTrue(out["question"])
        self.assertTrue(out["answer"])
        self.assertTrue(out["audio_url"])

    # 3. TTS 返回 audio_url
    def test_tts_returns_audio_url(self):
        t = VoiceTool()
        r = t.run(action="tts", text="当前天气晴，温度25℃")
        self.assertTrue(r["ok"])
        self.assertTrue(r["audio_url"])

    # 4. 不创建 task
    def test_voice_does_not_generate_task(self):
        before = self._count_tasks()
        va = self._mk_voice()
        for q in ("现场情况怎么样", "有没有危险"):
            va.ask_text(q)
        self.assertEqual(before, self._count_tasks())

    # 5. 不调用 communication / rescue
    def test_voice_does_not_call_tools(self):
        with patch("src.tools.communication_tool.CommunicationTool.run") as mcomm, \
             patch("src.tools.rescue_tool.RescueTool.run") as mrescue:
            va = self._mk_voice()
            va.ask_text("有没有危险")
            mcomm.assert_not_called()
            mrescue.assert_not_called()

    def _count_tasks(self):
        with sqlite3.connect(self.db) as c:
            return c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


if __name__ == "__main__":
    unittest.main(verbosity=2)