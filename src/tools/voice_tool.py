"""src/tools/voice_tool.py — 语音输入/输出工具 (V4.1 Assistant 交互层)。

定位: 语音只是 Assistant 的输入输出方式; 不进入 Agent 执行链,
      不触发 rescue_tool / communication_tool / 报警/救援任务。

Phase 4 阶段: 使用 mock provider(零网络), 不接真实 ASR/TTS。
真实接入预留(env, 不写死): ASR_API_KEY / TTS_API_KEY。

工具契约: 继承 src.tools.base.Tool, run(**kwargs) 分发:
  - "asr"   audio_to_text(audio_path)  -> {ok, text}
  - "tts"   text_to_audio(text)        -> {ok, audio_url}
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.tools.base import Tool

_ASR_API_KEY_ENV = "ASR_API_KEY"
_TTS_API_KEY_ENV = "TTS_API_KEY"
# mock ASR 默认转写的文本(真实语音识别由外部服务提供; mock 仅用于链路测试)
_DEFAULT_ASR_TEXT = "现场情况怎么样"


class VoiceTool(Tool):
    """语音 ASR / TTS(mock provider)。仅作输入输出转换, 不产生任何动作。"""

    name = "voice_tool"

    @property
    def description(self) -> str:
        return ("语音交互工具: asr(音频转文本) / tts(文本转音频)。"
                "仅做输入输出转换, 不触发任何救援动作。")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["asr", "tts"]},
                "audio_path": {"type": "string", "description": "asr: 音频文件路径"},
                "text": {"type": "string", "description": "tts: 要合成的文本"},
            },
            "additionalProperties": True,
        }

    def run(self, **kwargs: Any) -> Dict[str, Any]:
        action = kwargs.get("action")
        if action == "asr":
            return self.audio_to_text(audio_path=kwargs.get("audio_path"))
        if action == "tts":
            return self.text_to_audio(text=kwargs.get("text"))
        return {"ok": False, "action": action or "?", "error": f"Unsupported voice action: {action}"}

    def audio_to_text(self, audio_path: Optional[str] = None) -> Dict[str, Any]:
        """音频 → 文本(mock)。真实接入时代理会使用 ASR_API_KEY 调语音服务。"""
        try:
            text = _DEFAULT_ASR_TEXT
            if audio_path:
                text = f"{_DEFAULT_ASR_TEXT} (来自 {audio_path})"
            return {"ok": True, "text": text, "provider": "mock"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "asr", "error": str(exc)}

    def text_to_audio(self, text: Optional[str] = None) -> Dict[str, Any]:
        """文本 → 音频(mock)。真实接入时代理会使用 TTS_API_KEY。"""
        try:
            seed = abs(hash(str(text or ""))) % (10 ** 8)
            audio_url = f"mock://voice/output_{seed}.mp3"
            return {"ok": True, "audio_url": audio_url, "provider": "mock"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "tts", "error": str(exc)}


if __name__ == "__main__":
    t = VoiceTool()
    print(t.info())
    print("Key env 预留:", _ASR_API_KEY_ENV, _TTS_API_KEY_ENV)
    print(t.run(action="asr", audio_path="demo.wav"))
    print(t.run(action="tts", text="当前天气晴，温度25℃"))