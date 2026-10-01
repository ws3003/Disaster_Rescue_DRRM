"""src/voice/asr.py — 语音识别封装(复用 src/tools/voice_tool 的 ASR, 不重新实现)。"""

from __future__ import annotations

from typing import Optional

from src.tools.voice_tool import VoiceTool


class ASR:
    """封装 VoiceTool.audio_to_text: wav -> text。"""

    def __init__(self, voice_tool: Optional[VoiceTool] = None):
        self._vt = voice_tool or VoiceTool()

    def transcribe(self, wav_path: str) -> dict:
        """wav -> {ok, text} / {ok, error}。不抛异常。"""
        try:
            r = self._vt.audio_to_text(wav_path)
            if isinstance(r, dict) and r.get("ok"):
                return {"ok": True, "text": r.get("text", "")}
            return {"ok": False, "error": r.get("error", "ASR service unavailable")}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"ASR exception: {exc}"}