"""src/tools/voice_tool.py — 语音输入/输出工具 (V4.1 Assistant 交互层)。

定位: 语音只是 Assistant 的输入输出方式; 不进入 Agent 执行链,
      不触发 rescue_tool / communication_tool / 报警/救援任务。

Provider 架构 (Phase6):
  - mock  默认, 零网络, 返回固定转写/占位音频 URL(离线/测试/CI)
  - live  真实 ASR/TTS, 惰性 requests, 由环境变量驱动(架构级, 不绑定具体厂商;
          预留接入阿里云语音 / 讯飞 / CosyVoice)

环境变量:
  VOICE_PROVIDER      总选择: mock(默认) / live
  VOICE_ASR_PROVIDER  ASR 侧选择(mock默认/live); 缺省回落 VOICE_PROVIDER
  ASR_API_KEY/ASR_BASE_URL   live ASR 凭据与端点
  VOICE_TTS_PROVIDER  TTS 侧选择(mock默认/live); 缺省回落 VOICE_PROVIDER
  TTS_API_KEY/TTS_BASE_URL   live TTS 凭据与端点

接口不变: audio_to_text(audio_path) -> {ok,text,provider} ; text_to_audio(text)->{ok,audio_url,provider}
失败统一: {ok:false, error:"..."} (不抛异常)
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from src.tools.base import Tool

_VOICE_PROVIDER_ENV = "VOICE_PROVIDER"
_VOICE_ASR_PROVIDER_ENV = "VOICE_ASR_PROVIDER"
_VOICE_TTS_PROVIDER_ENV = "VOICE_TTS_PROVIDER"
_ASR_API_KEY_ENV = "ASR_API_KEY"
_ASR_BASE_URL_ENV = "ASR_BASE_URL"
_ASR_MODEL_ENV = "ASR_MODEL"
_ASR_DEFAULT_MODEL = "paraformer-realtime-v2"
_TTS_API_KEY_ENV = "TTS_API_KEY"
_TTS_BASE_URL_ENV = "TTS_BASE_URL"
_VOICE_AUDIO_DIR = "data/audio"

_DEFAULT_ASR_TEXT = "现场情况怎么样"
_VOICE_OUT_DIR = "data/voice"


class VoiceTool(Tool):
    """语音 ASR / TTS (mock / live 双 provider)。仅输入输出转换, 不产生动作。"""

    name = "voice_tool"

    SUPPORTED_PROVIDERS = ("mock", "live")

    def __init__(self, provider: Optional[str] = None):
        """provider 缺省读 VOICE_PROVIDER 环境变量, 再缺省 "mock"。"""
        if provider is None:
            provider = os.environ.get(_VOICE_PROVIDER_ENV, "mock")
        provider = str(provider or "mock").lower()
        if provider not in self.SUPPORTED_PROVIDERS:
            raise ValueError(
                f"unsupported voice provider: {provider!r}, "
                f"support: {self.SUPPORTED_PROVIDERS}"
            )
        self.provider = provider

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

    # ------------------------------------------------------------ 侧级选择
    def _asr_mode(self) -> str:
        """返回 ASR provider: mock | live | aliyun。VOICE_ASR_PROVIDER 优先, 否则 provider。"""
        p = (os.environ.get(_VOICE_ASR_PROVIDER_ENV, "").strip().lower()
             or self.provider or "mock")
        return p if p in ("mock", "live", "aliyun") else "mock"

    def _tts_is_live(self) -> bool:
        p = (os.environ.get(_VOICE_TTS_PROVIDER_ENV, "").strip().lower() or self.provider)
        return p == "live"

    # ------------------------------------------------------------ ASR
    def audio_to_text(self, audio_path: Optional[str] = None) -> Dict[str, Any]:
        mode = self._asr_mode()
        if mode == "aliyun":
            return self._asr_aliyun(audio_path)
        if mode == "live":
            return self._asr_live(audio_path)
        return self._asr_mock(audio_path)

    def _asr_mock(self, audio_path: Optional[str] = None) -> Dict[str, Any]:
        try:
            text = _DEFAULT_ASR_TEXT
            if audio_path:
                text = f"{_DEFAULT_ASR_TEXT} (来自 {audio_path})"
            return {"ok": True, "text": text, "provider": "mock"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "error": str(exc)}

    def _asr_live(self, audio_path: Optional[str] = None) -> Dict[str, Any]:
        try:
            key = os.environ.get(_ASR_API_KEY_ENV, "").strip()
            url = os.environ.get(_ASR_BASE_URL_ENV, "").strip()
            if not key or not url:
                return {"ok": False, "text": "", "error": "ASR service unavailable"}
            import requests  # 惰性加载, 不影响 mock / 启动

            if audio_path and os.path.exists(audio_path):
                with open(audio_path, "rb") as fh:
                    payload = fh.read()
            else:
                payload = b""
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/octet-stream"}
            resp = requests.post(url, data=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            text = self._extract_text(resp)
            if not text:
                return {"ok": False, "text": "", "error": "ASR service unavailable"}
            return {"ok": True, "text": text, "provider": "live"}
        except Exception as exc:  # noqa: BLE001 — 真实服务失败不冒泡
            return {"ok": False, "text": "", "error": "ASR service unavailable"}

    def _asr_aliyun(self, audio_path: Optional[str] = None) -> Dict[str, Any]:
        """阿里云 DashScope 一句话识别(Paraformer)。音频先落 data/audio 供静态 URL, 再 POST asr。"""
        try:
            key = os.environ.get(_ASR_API_KEY_ENV, "").strip()
            base = os.environ.get("STORAGE_BASE_URL", "").strip()
            model = os.environ.get(_ASR_MODEL_ENV, "").strip() or _ASR_DEFAULT_MODEL
            if not key or not base:
                return {"ok": False, "text": "", "error": "ASR service unavailable"}
            if not audio_path or not os.path.exists(audio_path):
                return {"ok": False, "text": "", "error": "audio file not found"}
            # 拷入 data/audio 以生成可访问 URL(供 DashScope 拉取)
            from pathlib import Path
            from datetime import datetime
            adir = Path(_VOICE_AUDIO_DIR)
            adir.mkdir(parents=True, exist_ok=True)
            name = f"asr_{datetime.now().strftime('%Y%m%d%H%M%S')}.wav"
            dest = adir / name
            dest.write_bytes(Path(audio_path).read_bytes())
            file_url = f"{base.rstrip('/')}/api/v1/assistant/audio/{name}"

            import requests  # 惰性加载

            url = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/recognition"
            payload = {
                "model": model,
                "input": {"file_urls": [file_url]},
            }
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            text = self._extract_asr_text(resp.json())
            if not text:
                return {"ok": False, "text": "", "error": "ASR service unavailable"}
            return {"ok": True, "text": text, "provider": "aliyun"}
        except Exception as exc:  # noqa: BLE001 — 真实服务失败不冒泡
            return {"ok": False, "text": "", "error": "ASR service unavailable"}

    @staticmethod
    def _extract_asr_text(data: Any) -> str:
        """从 DashScope 一句话识别响应取 text(尽力尝试多字段)。"""
        if not isinstance(data, dict):
            return ""
        # 常见: {"output": {"text": ...}} / {"result": [{"transcription": {"text": ...}}]}
        out = data.get("output")
        if isinstance(out, dict) and out.get("text"):
            return str(out["text"])
        result = data.get("result")
        if isinstance(result, dict) and isinstance(result.get("transcription"), dict):
            t = result["transcription"].get("text")
            if t:
                return str(t)
        if isinstance(result, list):
            for r in result:
                if isinstance(r, dict):
                    tr = r.get("transcription")
                    if isinstance(tr, dict) and tr.get("text"):
                        return str(tr["text"])
                    if r.get("text"):
                        return str(r["text"])
        if data.get("text"):
            return str(data["text"])
        return ""

    @staticmethod
    def _extract_text(resp: Any) -> str:
        try:
            data = resp.json()
        except Exception:
            return str(resp.text or "").strip()
        if isinstance(data, dict):
            for k in ("text", "result", "transcript", "words"):
                if k in data:
                    v = data[k]
                    if isinstance(v, list) and v:
                        v = v[0]
                    if isinstance(v, dict):
                        v = v.get("text")
                    if v:
                        return str(v)
            return ""
        return str(data)

    # ------------------------------------------------------------ TTS
    def text_to_audio(self, text: Optional[str] = None) -> Dict[str, Any]:
        if self._tts_is_live():
            return self._tts_live(text)
        return self._tts_mock(text)

    def _tts_mock(self, text: Optional[str] = None) -> Dict[str, Any]:
        try:
            seed = abs(hash(str(text or ""))) % (10 ** 8)
            audio_url = f"mock://voice/output_{seed}.mp3"
            return {"ok": True, "audio_url": audio_url, "provider": "mock"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "audio_url": "", "error": str(exc)}

    def _tts_live(self, text: Optional[str] = None) -> Dict[str, Any]:
        try:
            key = os.environ.get(_TTS_API_KEY_ENV, "").strip()
            url = os.environ.get(_TTS_BASE_URL_ENV, "").strip()
            if not key or not url:
                return {"ok": False, "audio_url": "", "error": "TTS service unavailable"}
            import requests  # 惰性加载

            headers = {"Authorization": f"Bearer {key}"}
            resp = requests.post(url, params={"text": str(text or "")},
                                 headers=headers, timeout=20)
            resp.raise_for_status()
            audio_url = self._resolve_audio_url(resp, str(text or ""))
            if not audio_url:
                return {"ok": False, "audio_url": "", "error": "TTS service unavailable"}
            return {"ok": True, "audio_url": audio_url, "provider": "live"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "audio_url": "", "error": "TTS service unavailable"}

    def _resolve_audio_url(self, resp: Any, text: str) -> str:
        """优先取响应 JSON 中的 audio_url/url; 否则若是音频二进制则落盘本地返回路径。"""
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            for k in ("audio_url", "url", "data.url"):
                if k in data and data[k]:
                    return str(data[k])
        ctype = str(resp.headers.get("Content-Type", ""))
        if ctype.startswith("audio/") or resp.content:
            try:
                from pathlib import Path
                from datetime import datetime
                out_dir = Path(_VOICE_OUT_DIR)
                out_dir.mkdir(parents=True, exist_ok=True)
                name = f"tts_{datetime.now().strftime('%Y%m%d%H%M%S')}_{abs(hash(text)) % 10 ** 6}.mp3"
                out = out_dir / name
                out.write_bytes(resp.content)
                return str(out)
            except Exception:
                return ""
        return ""


if __name__ == "__main__":
    t = VoiceTool()
    print(t.info())
    print("Provider(=mock 若未 set):", t.provider)
    print("live 需 env:", _VOICE_ASR_PROVIDER_ENV, _ASR_API_KEY_ENV, _ASR_BASE_URL_ENV,
          _VOICE_TTS_PROVIDER_ENV, _TTS_API_KEY_ENV, _TTS_BASE_URL_ENV)
    print(t.run(action="asr", audio_path="demo.wav"))
    print(t.run(action="tts", text="当前天气晴，温度25℃"))