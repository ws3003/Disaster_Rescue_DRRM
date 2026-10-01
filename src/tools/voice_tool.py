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
_ASR_DEFAULT_MODEL = "paraformer-v2"
_TTS_API_KEY_ENV = "TTS_API_KEY"
_TTS_BASE_URL_ENV = "TTS_BASE_URL"
_TTS_PROVIDER_ENV = "TTS_PROVIDER"
_TTS_MODEL_ENV = "TTS_MODEL"
_TTS_DEFAULT_MODEL = "qwen-audio-3.1-tts-flash"
_TTS_VOICE_ENV = "TTS_VOICE"
_TTS_DEFAULT_VOICE = "Cherry"
_VOICE_AUDIO_DIR = "data/audio"
_TTS_OUT_DIR = "data/voice"

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
        """阿里云 DashScope Paraformer-v2 异步文件转写(paraformer-v2 -> /asr/transcription)。

        音频 wav 先上传 OSS(复用 src.storage.upload_snapshot) 拿公网 URL,
        提交异步任务拿 task_id, 轮询 /tasks/{task_id} 至 SUCCEEDED, 再解析文本。
        """
        try:
            key = os.environ.get(_ASR_API_KEY_ENV, "").strip()
            model = os.environ.get(_ASR_MODEL_ENV, "").strip() or _ASR_DEFAULT_MODEL
            if not key:
                return {"ok": False, "text": "", "error": "ASR service unavailable"}
            if not audio_path or not os.path.exists(audio_path):
                return {"ok": False, "text": "", "error": "audio file not found"}
            # 上传 wav 到 OSS(图片已复用同一 OSS; 音频对象前缀 audio/)
            from src.storage import upload_snapshot
            up = upload_snapshot(audio_path, prefix="audio")
            if not up.get("ok"):
                print("[ASR] OSS upload failed:", up.get("error"))
                return {"ok": False, "text": "", "error": "ASR service unavailable"}
            file_url = up.get("url")

            import requests  # 惰性加载
            import time

            base = "https://dashscope.aliyuncs.com/api/v1"
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            }
            payload = {
                "model": model,
                "input": {"file_urls": [file_url]},
                # 固化格式/采样率, 减少 DashScope 对 URL/音频格式的解析歧义
                "parameters": {"format": "wav", "sample_rate": 16000},
            }
            # ==== TEMP DEBUG (阶段1: 打印真实运行时变量, 定位后移除) ====
            print("[DEBUG] ASR URL:", file_url)
            print("[DEBUG] payload:", payload)

            # step1: 提交异步文件转写任务 paraformer-v2 -> /asr/transcription
            try:
                resp = requests.post(f"{base}/services/audio/asr/transcription",
                                     json=payload, headers=headers, timeout=30)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                print("[ASR] DashScope submit failed:", exc)
                return {"ok": False, "text": "", "error": "ASR service unavailable"}
            data = resp.json()
            task_id = (data.get("output") or {}).get("task_id")
            if not task_id:
                print(f"[ASR] no task_id in submit: {resp.text[:1200]}")
                return {"ok": False, "text": "", "error": "ASR service unavailable"}
            print(f"[ASR-TASK] task_id={task_id} status={(data.get('output') or {}).get('task_status')}")

            # step2: 轮询任务状态(最多 30 次, 间隔 1s)
            task_url = f"{base}/tasks/{task_id}"
            status, result = "PENDING", None
            for _ in range(30):
                try:
                    gr = requests.get(task_url, headers=headers, timeout=30)
                    gr.raise_for_status()
                except Exception as exc:  # noqa: BLE001
                    print("[ASR] DashScope poll failed:", exc)
                    return {"ok": False, "text": "", "error": "ASR service unavailable"}
                gd = gr.json()
                status = (gd.get("output") or {}).get("task_status", "PENDING")
                print(f"[ASR-TASK] task_id={task_id} status={status}")
                if status == "SUCCEEDED":
                    result = gd
                    break
                if status == "FAILED":
                    print(f"[ASR] task failed body={gr.text[:1200]}")
                    return {"ok": False, "text": "", "error": "ASR service unavailable"}
                time.sleep(1)
            if status != "SUCCEEDED":
                print(f"[ASR] task timed out status={status}")
                return {"ok": False, "text": "", "error": "ASR service unavailable"}

            # step3: 解析文本
            text = self._extract_asr_text(result)
            if not text:
                print(f"[ASR] no text in result body={str(result)[:1200]}")
                return {"ok": False, "text": "", "error": "ASR service unavailable"}
            return {"ok": True, "text": text, "provider": "aliyun"}
        except Exception as exc:  # noqa: BLE001 — 真实服务失败不冒泡
            print("[ASR-DEBUG] exception:", exc)
            print("[ASR] exception:", exc)
            return {"ok": False, "text": "", "error": "ASR service unavailable"}

    def _extract_asr_text(self, data: Any) -> str:
        """从 DashScope 一句话识别响应取 text(尽力尝试多字段)。"""
        if not isinstance(data, dict):
            return ""
        # 常见: {"output": {"text": ...}} / {"result": [{"transcription": {"text": ...}}]}
        out = data.get("output")
        if isinstance(out, dict):
            if out.get("text"):
                return str(out["text"])
            # paraformer-v2: text 在 output.sentence[].text / output.paragraphs[].text
            if out.get("sentences") or out.get("sentence"):
                segs = out.get("sentences") or out.get("sentence")
                return self._join(segs)
            if out.get("paragraphs"):
                return self._join(out.get("paragraphs"))
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

        # 旧结构无文本 -> 尝试 paraformer-v2 异步结果里的 transcription_url
        tu = self._find_transcription_url(data)
        if tu:
            # ==== TEMP DEBUG (定位后移除) ====
            print("[ASR-TRANSCRIPTION] url=", tu)
            try:
                import requests  # 惰性加载
                r = requests.get(tu, timeout=30)
                r.raise_for_status()
                t = self._parse_transcripts(r.json())
            except Exception as exc:  # noqa: BLE001
                print("[ASR] transcription fetch failed:", exc)
                t = ""
            if t:
                # ==== TEMP DEBUG (定位后移除) ====
                print("[ASR-TRANSCRIPTION] text=", t)
                return t
        return ""

    @classmethod
    def _find_transcription_url(cls, obj: Any) -> str:
        """深度优先查找首个 transcription_url(paraformer-v2 异步结果)。"""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "transcription_url" and isinstance(v, str) and v:
                    return v
                found = cls._find_transcription_url(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = cls._find_transcription_url(item)
                if found:
                    return found
        return ""

    @classmethod
    def _parse_transcripts(cls, data: Any) -> str:
        """解析 DashScope 返回 JSON: transcripts[] / sentences[] / text, 空格拼接。"""
        if not isinstance(data, dict):
            return ""
        for key in ("transcripts", "sentences", "sentence", "paragraphs", "results"):
            segs = data.get(key)
            if isinstance(segs, list):
                parts = []
                for s in segs:
                    if isinstance(s, dict) and s.get("text"):
                        parts.append(str(s["text"]))
                if parts:
                    return " ".join(parts)
        if data.get("text"):
            return str(data["text"])
        return ""

    @staticmethod
    def _join(segs: Any) -> str:
        """拼接 segment 列表的 text 字段(供 paraformer sentence/paragraph)。"""
        parts = []
        for s in (segs or []):
            if not isinstance(s, dict):
                continue
            t = s.get("text")
            if t:
                parts.append(str(t))
        return "".join(parts).strip()

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
    def _tts_mode(self) -> str:
        """返回 TTS provider: mock | live(DashScope/旧) | edge(默认, 本地 edge-tts)。

        默认 edge: 只有显式设置 TTS_PROVIDER 或 VOICE_TTS_PROVIDER 才切换;
        不随 VOICE_PROVIDER(ASR)或 self.provider 变(避免默认落入 mock/live)。
        """
        p = (os.environ.get(_TTS_PROVIDER_ENV, "")
             or os.environ.get(_VOICE_TTS_PROVIDER_ENV, "")).strip().lower()
        if p in ("mock", "live", "edge"):
            return p
        return "edge"

    def text_to_audio(self, text: Optional[str] = None) -> Dict[str, Any]:
        mode = self._tts_mode()
        if mode == "edge":
            return self._tts_edge(text)
        if mode == "live":
            return self._tts_live(text)
        return self._tts_mock(text)

    def _tts_edge(self, text: Optional[str] = None) -> Dict[str, Any]:
        """本地 edge-tts(微软) CLI: text -> data/audio/response.mp3, 返回音频路径。

        树莓派实测: edge-tts Python API(Communicate.save) 连不上
        speech.platform.bing.com:443 (Connection reset by peer), 而系统
        edge-tts CLI 可正常输出; 故走 subprocess 调用 CLI。
        文本过长经 stdin 传入, 避免 argv 长度/转义问题。
        """
        import shutil
        import subprocess

        out_path = "data/audio/response.mp3"
        os.makedirs("data/audio", exist_ok=True)

        cli = shutil.which("edge-tts")
        if not cli:
            return {"ok": False, "error": "edge-tts CLI not found (pip install edge-tts)"}
        try:
            # -f - 从 stdin 读文本(util.py: -f 为 - /dev/stdin 时读取 stdin),
            # 避免 --text 在 argv 传长文/中文转义问题, 也符合 user 已验证的 CLI 用法。
            args = [cli, "-f", "-", "--voice", "zh-CN-XiaoxiaoNeural",
                    "--write-media", out_path]
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            proc = subprocess.run(
                args, input=str(text or "").encode("utf-8"), capture_output=True,
                timeout=60, env=env)
        except Exception as exc:  # noqa: BLE001 — CLI 缺失/超时/编码
            return {"ok": False, "error": f"edge-tts unavailable: {exc}"}
        if proc.returncode != 0 or not os.path.exists(out_path):
            msg = proc.stderr.decode("utf-8", "replace").strip() or "edge-tts produced no file"
            return {"ok": False, "error": msg}
        return {"ok": True, "audio_path": out_path, "provider": "edge"}

    def _tts_mock(self, text: Optional[str] = None) -> Dict[str, Any]:
        try:
            seed = abs(hash(str(text or ""))) % (10 ** 8)
            audio_url = f"mock://voice/output_{seed}.mp3"
            return {"ok": True, "audio_url": audio_url, "provider": "mock"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "audio_url": "", "error": str(exc)}

    def _tts_live(self, text: Optional[str] = None) -> Dict[str, Any]:
        """百炼 Qwen-Audio-3.1-TTS(DashScope SDK SpeechSynthesizer)。"""
        try:
            key = os.environ.get(_TTS_API_KEY_ENV, "").strip()
            model = os.environ.get(_TTS_MODEL_ENV, "").strip() or _TTS_DEFAULT_MODEL
            voice = os.environ.get(_TTS_VOICE_ENV, "").strip() or "Cherry"
            if not key:
                return {"ok": False, "audio_url": "", "error": "TTS service unavailable"}
            import dashscope  # 惰性加载
            from dashscope.audio.tts import SpeechSynthesizer

            dashscope.api_key = key
            result = SpeechSynthesizer.call(
                model=model,
                text=str(text or ""),
                voice=voice,
            )
            if not result or result.get_status_code() != 200:
                return {"ok": False, "audio_url": "", "error": "TTS service unavailable"}
            audio_bytes = result.get_audio_data()
            if not audio_bytes:
                return {"ok": False, "audio_url": "", "error": "TTS service unavailable"}
            return {"ok": True, "audio_url": self._save_audio(audio_bytes, "mp3"),
                    "provider": "aliyun"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "audio_url": "", "error": "TTS service unavailable"}

    def _save_audio(self, data: bytes, ext: str) -> str:
        """把音频字节保存到 data/voice/<ts>.<ext>, 返回绝对路径。"""
        from pathlib import Path
        from datetime import datetime
        out_dir = Path(_TTS_OUT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"tts_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}"
        path = out_dir / name
        path.write_bytes(data)
        return str(path)

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