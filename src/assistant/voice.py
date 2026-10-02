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
from . import command_router
from .agent_bridge import AgentBridge
from ..tools.voice_tool import VoiceTool


class VoiceAssistant:
    """负责任音交互编排。asr / tts / responder 均可注入, 便于替换与测试。"""

    def __init__(
        self,
        asr: Optional[Callable[[str], Dict[str, Any]]] = None,
        tts: Optional[Callable[[str], Dict[str, Any]]] = None,
        responder: Optional[AssistantResponder] = None,
        bridge: Optional[Any] = None,
    ):
        _vt = VoiceTool()
        self._asr = asr or _vt.audio_to_text
        self._tts = tts or _vt.text_to_audio
        self._responder = responder or AssistantResponder()
        self._bridge = bridge or AgentBridge()

    def handle(
        self,
        audio_path: Optional[str] = None,
        text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """音频(经 ASR)或直接文本 → 命令路由 → (信息 Responder | 操作 AgentBridge)。

        返回 {question, answer, audio_url, intent, require_confirm, confirmed}。
        """
        if not text and audio_path:
            asr_res = self._asr(audio_path)
            text = (asr_res or {}).get("text") if (asr_res or {}).get("ok") else None
        if not text:
            return {"question": None, "answer": "", "audio_url": None,
                    "intent": "unknown", "require_confirm": False, "confirmed": False,
                    "error": "no audio or text provided, or ASR 失败"}

        # 救援现场查询分支(V4.4, 只读 SQLite, 不进 Agent/不调用永续 LLM):
        # 命中现场/伤员/人员/风险/任务/救援等关键词 → context_tool 生成中文回答。
        # 天气/医院/路线/发送报警等不含这些关键词, 仍走原命令路由, 不回归。
        if self._is_rescue_query(text):
            return self._answer_scene_context(text)

        # 命令路由
        cmd = command_router.classify(text)
        intent = cmd.get("intent", "unknown")
        if cmd.get("category") == "reject":
            return {"question": text, "answer": "抱歉，未识别的指令。",
                    "audio_url": None, "intent": intent,
                    "require_confirm": False, "confirmed": False}

        if cmd.get("category") == "info":
            out = self._responder.answer(text)
            answer_txt = (out or {}).get("answer", "")
            audio_url = None
            if answer_txt:
                tts_res = self._tts(answer_txt)
                audio_url = (tts_res or {}).get("audio_url") if (tts_res or {}).get("ok") else None
            return {"question": text, "answer": answer_txt, "audio_url": audio_url,
                    "intent": intent, "require_confirm": False, "confirmed": False}

        # action 命令 → AgentBridge(二次确认 / 进入 Agent)
        action = cmd.get("intent")
        need_confirm = cmd.get("require_confirm", False)
        bridge = self._bridge
        res = bridge.handle(action, confirmed=(not need_confirm))
        answer = res.get("confirm_message") if (need_confirm and not res.get("confirmed")) \
            else self._summarize_action(res, action)
        audio_url = None
        if answer:
            tts_res = self._tts(answer)
            audio_url = (tts_res or {}).get("audio_url") if (tts_res or {}).get("ok") else None
        return {"question": text, "answer": answer, "audio_url": audio_url,
                "intent": intent,
                "require_confirm": bool(need_confirm),
                "confirmed": bool(res.get("confirmed"))}

    @staticmethod
    def _summarize_action(res: Dict[str, Any], intent: str) -> str:
        if res.get("ok"):
            return f"已通过调度执行 {intent}。"
        return f"{intent} 未能执行。"

    # ------------------------------------------------------------
    # 救援现场查询(V4.4)——只读 SQLite, 不进 Agent / 非万能聊天
    # ------------------------------------------------------------
    _RESCUE_KEYWORDS = ("现场", "情况", "伤员", "人员", "风险", "任务", "救援", "受困")

    @classmethod
    def _is_rescue_query(cls, text: str) -> bool:
        """是否请求"现场/伤员/任务"类救援上下文(不含天气/医院/路线/报警)。"""
        t = text or ""
        return any(kw in t for kw in cls._RESCUE_KEYWORDS)

    def _answer_scene_context(self, question: str) -> Dict[str, Any]:
        """用 context_tool 查 SQLite → 生成中文现场回答(复用本类 TTS 链路)。"""
        from ..tools.context_tool import get_current_scene_context
        ctx = get_current_scene_context()
        persons = ctx.get("persons") or []
        latest_task = ctx.get("latest_task")
        answer = self._render_scene_answer(persons, latest_task)
        intent = "scene_context"
        audio_url = None
        if answer:
            tts_res = self._tts(answer)
            audio_url = (tts_res or {}).get("audio_url") if (tts_res or {}).get("ok") else None
        return {"question": question, "answer": answer, "audio_url": audio_url,
                "intent": intent, "require_confirm": False, "confirmed": False}

    @staticmethod
    def _render_scene_answer(persons: list, latest_task: dict) -> str:
        """由 persons/latest_task 生成中文现场总结。"""
        if not persons:
            base = "当前没有检测到受困人员。"
        else:
            n = len(persons)
            parts = [f"当前发现{n}名人员。"]
            for p in persons:
                state = p.get("state")
                prior = p.get("priority") or "未知"
                parts.append(f"人员{p.get('person_id')}: 状态{state}，风险等级{prior}。")
            base = "".join(parts)
        if latest_task:
            task_id = latest_task.get("task_id")
            status = latest_task.get("status")
            base += f"已生成救援任务{task_id}（状态{status}）。"
        return base

    def ask_text(self, question: str) -> Dict[str, Any]:
        """文本问答(跳过 ASR, 仅做回答 + TTS)。"""
        out = self._responder.answer(question)
        answer_txt = (out or {}).get("answer", "")
        audio_url = None
        if answer_txt:
            tts_res = self._tts(answer_txt)
            audio_url = (tts_res or {}).get("audio_url") if (tts_res or {}).get("ok") else None
        return {"question": question, "answer": answer_txt, "audio_url": audio_url}