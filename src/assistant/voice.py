"""src/assistant/voice.py — 语音交互编排 (V4.1 Assistant 交互层)。

流程:
    audio
      |
      v  voice_tool.ASR (audio -> text)
      |
      v  assistant.responder.ask (text -> answer)
      |
      v  voice_tool.TTS (answer -> audio_url)
      |
    {question, answer, audio_url}

约束: 语音仅为输入输出方式, 不进入 Agent 执行链, 不触发任何救援动作。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .responder import AssistantResponder
from ..tools.voice_tool import VoiceTool


class VoiceAssistant:
    """负责任音交互编排。asr / tts / responder 均可注入, 便于替换与测试。"""

    def __init__(
        self,
        asr: Optional[Callable[[str], Dict[str, Any]]] = None,
        tts: Optional[Callable[[str], Dict[str, Any]]] = None,
        responder: Optional[AssistantResponder] = None,
    ):
        _vt = VoiceTool()
        self._asr = asr or _vt.audio_to_text
        self._tts = tts or _vt.text_to_audio
        self._responder = responder or AssistantResponder()

    def handle(
        self,
        audio_path: Optional[str] = None,
        text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """音频(经 ASR)或直接文本 → 回答 → 音频 URL。返回 {question, answer, audio_url}。"""
        if not text and audio_path:
            asr_res = self._asr(audio_path)
            text = (asr_res or {}).get("text") if (asr_res or {}).get("ok") else None
        if not text:
            return {"question": None, "answer": "", "audio_url": None,
                    "error": "no audio or text provided, or ASR 失败"}

        out = self._responder.answer(text)
        answer_txt = (out or {}).get("answer", "")

        audio_url = None
        if answer_txt:
            tts_res = self._tts(answer_txt)
            audio_url = (tts_res or {}).get("audio_url") if (tts_res or {}).get("ok") else None

        return {"question": text, "answer": answer_txt, "audio_url": audio_url}

    def ask_text(self, question: str) -> Dict[str, Any]:
        """文本问答(跳过 ASR, 仅做回答 + TTS)。"""
        out = self._responder.answer(question)
        answer_txt = (out or {}).get("answer", "")
        audio_url = None
        if answer_txt:
            tts_res = self._tts(answer_txt)
            audio_url = (tts_res or {}).get("audio_url") if (tts_res or {}).get("ok") else None
        return {"question": question, "answer": answer_txt, "audio_url": audio_url}