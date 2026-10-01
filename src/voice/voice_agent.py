"""src/voice/voice_agent.py — 语音问答与自动播报。

复用: src/voice/{button,recorder,asr,speaker} 与 src/assistant/responder、src/tools/voice_tool。
- run():      按钮→录音→ASR→AssistantResponder→TTS→Speaker 完整交互闭环。
- broadcast(): 纯文本→TTS→Speaker 自动播报入口(可被任意文本源调用, 如已生成的回答)。
不新建 Agent/LLM/VLM/DB/Comm; 不改变 VoiceTool/Speaker API。
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

        # TTS Output: answer -> mp3 -> Speaker (失败保护, 不阻塞主流程)
        bar = self.broadcast(answer)
        audio = bar.get("audio")

        return {"text": text, "answer": answer, "audio": audio}

    def broadcast(self, text: Optional[str] = None) -> dict:
        """自动播报闭环: 文本 → VoiceTool.text_to_audio → audio_path → Speaker.play。

        TTS/播放失败仅打印, 不抛异常, 不影响调用方。
        返回 {ok, audio, error}; audio 为音频路径(成功时)。
        """
        if not text:
            return {"ok": False, "audio": None, "error": "no text to speak"}

        tts_res = self._tts.text_to_audio(text)
        if not tts_res.get("ok"):
            print("[TTS] unavailable:", tts_res.get("error"))
            return {"ok": False, "audio": None, "error": tts_res.get("error")}

        audio = tts_res.get("audio_path") or tts_res.get("audio_url")
        print("[TTS] audio:", audio)
        try:
            self._speaker.play(audio)
        except Exception as exc:  # noqa: BLE001 — 播放失败不中断
            print("[TTS] play failed:", exc)
        return {"ok": True, "audio": audio}