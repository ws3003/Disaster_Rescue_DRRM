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
        # 现场中文回答层(纯文本生成, 不调 LLM/TTS)
        from .scene_responder import SceneResponder
        self.scene_responder = SceneResponder()
        # 智能路由(决策: scene=现场查询 / agent=救援决策 / None=交给命令路由)
        from .voice_decision_router import VoiceDecisionRouter
        self._decision_router = VoiceDecisionRouter()

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

        # 智能路由(V4.3): scene=现场查询 / risk=风险分析 / agent=救援决策。
        # 其余(None)仍走既有命令路由(天气/医院/路线/发送报警), 不回归。
        _route = self._decision_router.route(text)
        if _route == "scene":
            return self._answer_scene_context(text)
        if _route == "risk":
            return self._answer_risk(text)
        if _route == "agent":
            return self._answer_agent(text)

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
    _RESCUE_KEYWORDS = ("现场", "情况", "伤员", "人员", "风险", "危险", "任务", "救援", "受困")

    @classmethod
    def _is_rescue_query(cls, text: str) -> bool:
        """是否请求"现场/伤员/任务"类救援上下文(不含天气/医院/路线/报警)。"""
        t = text or ""
        return any(kw in t for kw in cls._RESCUE_KEYWORDS)

    def _answer_scene_context(self, question: str) -> Dict[str, Any]:
        """用 context_tool 查 SQLite → SceneResponder 生成中文现场回答(复用本类 TTS 链路)。"""
        from ..tools.context_tool import get_current_scene_context
        ctx = get_current_scene_context()
        answer = self.scene_responder.answer_scene(ctx)
        intent = "scene_context"
        audio_url = None
        if answer:
            tts_res = self._tts(answer)
            audio_url = (tts_res or {}).get("audio_url") if (tts_res or {}).get("ok") else None
        return {"question": question, "answer": answer, "audio_url": audio_url,
                "intent": intent, "require_confirm": False, "confirmed": False}

    def _answer_risk(self, question: str) -> Dict[str, Any]:
        """风险分析/评估 —— 只读查询, 不触发 Agent/Executor/save_task。

        数据来源: context_tool.get_current_scene_context()(SQLite/events 只读),
        按 risk_score 排序取最高风险人员并生成中文回答。
        """
        from ..tools.context_tool import get_current_scene_context
        ctx = get_current_scene_context()
        persons = ctx.get("persons") or []
        answer = self._render_risk_answer(persons)
        audio_url = None
        if answer:
            tts_res = self._tts(answer)
            audio_url = (tts_res or {}).get("audio_url") if (tts_res or {}).get("ok") else None
        return {"question": question, "answer": answer, "audio_url": audio_url,
                "intent": "risk_analysis", "require_confirm": False, "confirmed": True}

    def _render_risk_answer(self, persons: list) -> str:
        """按 risk_score 降序渲染风险回答; 并列时按 person_id 升序。"""
        def _key(p):
            rs = p.get("risk_score")
            try:
                rs = float(rs) if rs is not None else 0.0
            except (TypeError, ValueError):
                rs = 0.0
            return (-rs, int(p.get("person_id") or 0))

        if not persons:
            return "当前没有检测到人员，暂无法评估风险。系统将继续监测现场。"

        ordered = sorted(persons, key=_key)
        top = ordered[0]
        lines = [f"当前发现{len(ordered)}名人员。"]
        for i, p in enumerate(ordered, start=1):
            state = self.scene_responder._state_text(p.get("state"))
            pri = str(p.get("priority") or "未知")
            lines.append(f"{i}号人员处于{state}状态，\n风险等级{pri}。")
        top_pid = top.get("person_id")
        lines.append(f"当前最高风险人员为{top_pid}号，\n建议优先检查。")
        return "\n".join(lines)

    def _answer_agent(self, question: str) -> Dict[str, Any]:
        """救援建议/处理问题(route==agent, 属咨询/原因语境)。

        仅给出决策辅助(agent_reasoning), 绝不调用 AgentBridge / save_task / 创建任务。
        明确动作命令(发送报警/通知救援)因无 agent 关键词而 route=None, 走 command_router
        的执行路径(含危险二次确认), 不进入本方法。
        """
        return self._answer_agent_reasoning(question)

    def _answer_agent_reasoning(self, question: str) -> Dict[str, Any]:
        """救援建议/原因分析: 仅给出决策辅助文本, 绝不触发执行/创建任务。

        当前项目无"分析型"专用 LLM 接口按需接入(Agent 为执行链), 故返回结构化占位
        建议; 不伪造任务执行。
        """
        answer = "建议先确认现场风险，优先处理高风险人员。"
        audio_url = None
        tts_res = self._tts(answer)
        audio_url = (tts_res or {}).get("audio_url") if (tts_res or {}).get("ok") else None
        return {"question": question, "answer": answer, "audio_url": audio_url,
                "intent": "agent_reasoning",
                "require_confirm": False, "confirmed": False}

    def ask_text(self, question: str) -> Dict[str, Any]:
        """文本问答(跳过 ASR, 仅做回答 + TTS)。"""
        out = self._responder.answer(question)
        answer_txt = (out or {}).get("answer", "")
        audio_url = None
        if answer_txt:
            tts_res = self._tts(answer_txt)
            audio_url = (tts_res or {}).get("audio_url") if (tts_res or {}).get("ok") else None
        return {"question": question, "answer": answer_txt, "audio_url": audio_url}