"""src/voice/voice_agent.py — 按钮→录音→ASR→AssistantResponder 回答。

复用: src/voice/{button,recorder,asr} 与 src/assistant/responder(既有文本→灾害回答层)。
不新建 Agent/LLM/VLM/DB/Comm; 不在这里做 TTS(预留 audio=None)。
"""

from __future__ import annotations

from typing import Any, Optional

from src.voice.button import VoiceButton  # Button 别名
from src.voice.recorder import record
from src.voice.asr import ASR
from src.voice.speaker import Speaker
from src.assistant.responder import AssistantResponder
from src.tools.voice_tool import VoiceTool

_OUT_WAV = "data/audio/input.wav"


class VoiceAgent:
    def __init__(
        self,
        button: Optional[Any] = None,
        asr: Optional[ASR] = None,
        responder: Optional[AssistantResponder] = None,
        tts: Optional[VoiceTool] = None,
        speaker: Optional[Any] = None,
    ):
        self._button = button or VoiceButton()
        self._asr = asr or ASR()
        self._responder = responder or AssistantResponder()
        self._tts = tts or VoiceTool()
        self._speaker = speaker or Speaker

    def _record(self, seconds: float) -> str:
        return record(duration=seconds, out_path=_OUT_WAV)

    def run(self, seconds: float = 5.0) -> dict:
        """等待按键→录音→ASR→AssistantResponder→TTS(edge/mock)→Speaker.play。

        返回 {text, answer, audio}。
        """
        print("\n[BUTTON] waiting…")
        if not self._button.wait_press():
            return {"text": "", "answer": "", "audio": None,
                    "error": "button not pressed"}
        print("[BUTTON] pressed")

        wav = self._record(seconds)
        print("[RECORD] saved", wav)

        asr_res = self._asr.transcribe(wav)
        if not asr_res.get("ok"):
            print("[ASR] failed:", asr_res.get("error"))
            return {"text": "", "answer": "", "audio": None,
                    "error": asr_res.get("error")}
        text = asr_res.get("text", "")
        print("[ASR] text:", text)

        out = self._responder.answer(text)
        answer = (out or {}).get("answer", "")
        print("[ASSISTANT] answer:", answer)

        # TTS Output: answer -> mp3 -> Speaker
        audio = None
        tts_res = self._tts.text_to_audio(answer) if answer else {}
        if tts_res.get("ok"):
            audio = tts_res.get("audio_path") or tts_res.get("audio_url")
            print("[TTS] audio:", audio)
            self._speaker.play(audio)
        else:
            print("[TTS] skip/unavailable:", tts_res.get("error"))

        return {"text": text, "answer": answer, "audio": audio}