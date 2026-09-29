"""Assistant 回答器 (模板回答, 暂不接入 LLM 自由生成)。

流程: 问题 → intent.classify → context.collect → 按意图选模板 → {answer, sources, intent}。

意图模板在"灾害现场信息辅助决策助手"口径下生成:
  - 只陈述/提示信息;
  - 不调用 rescue_tool / communication_tool;
  - 不触发报警 / 救援任务。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import intent as intent_mod
from .context import AssistantContext


class AssistantResponder:

    def __init__(
        self,
        db_path: Optional[str] = None,
        snapshot_dir: Optional[str] = None,
        context: Optional[AssistantContext] = None,
    ):
        self._ctx = context or AssistantContext(db_path=db_path, snapshot_dir=snapshot_dir)

    # ---------------------------------------------------------------- 模板
    def _answer_scene(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        task = ctx.get("latest_task") or {}
        scene = ctx.get("scene") or {}
        summary = scene.get("summary") or task.get("trigger_event")
        status = scene.get("status") or task.get("status")
        tid = task.get("task_id")
        sources: list[Dict[str, Any]] = []
        if tid:
            sources.append({"type": "sqlite", "task_id": tid, "status": status})
        if summary:
            ans = (f"最近一次识别到现场事件：{summary}（任务 {tid}，状态 {status}）。"
                   f"如需进一步确认，请结合现场图片与人员状态人工研判。")
        else:
            ans = "当前暂未获取到现场状态数据。"
        return {"answer": ans, "sources": sources}

    def _answer_risk(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        task = ctx.get("latest_task") or {}
        summary = (ctx.get("scene") or {}).get("summary") or task.get("trigger_event")
        sources: list[Dict[str, Any]] = []
        if task.get("task_id"):
            sources.append({"type": "sqlite", "task_id": task["task_id"]})
        if summary:
            ans = (f"检测到现场事件：{summary}。请据此人工研判人员风险与救援优先级，"
                   f"并结合现场图片确认。系统仅提供信息辅助，不自动派单或发送报警。")
        else:
            ans = "当前暂无风险数据。如有人员处于高风险状态（如 Fall），请人工即时研判。"
        return {"answer": ans, "sources": sources}

    def _answer_snapshot(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        snap = ctx.get("snapshot") or {}
        fname = snap.get("file")
        sources: list[Dict[str, Any]] = []
        if fname:
            sources.append({"type": "snapshot", "file": fname})
            ans = f"最近一张现场快照已保存为：{fname}（公网图片地址请通过 OSS 存储服务访问）。"
        else:
            ans = "当前暂无现场快照。"
        return {"answer": ans, "sources": sources}

    def _answer_general(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        ans = ("我可以为您提供信息辅助：可询问\"现场怎么样\"（场景状态）、"
               "\"有没有危险\"（风险）、\"图片在哪里\"（现场快照）、"
               "\"天气怎么样\"（天气）。系统仅提供信息，不发起任何救援动作。")
        return {"answer": ans, "sources": []}

    def _answer_weather(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        w = ctx.get("weather")
        sources: list[Dict[str, Any]] = []
        if isinstance(w, dict) and w:
            cond = w.get("condition")
            temp = w.get("temperature")
            wind = w.get("wind")
            sources.append({"type": "weather", "timestamp": w.get("timestamp"),
                            "source": w.get("source")})
            cond_txt = cond if isinstance(cond, str) else "未知"
            temp_txt = f"{temp}℃" if temp is not None else "—"
            ans = f"当前天气{cond_txt}，温度{temp_txt}。"
            if wind:
                ans += f"风力{wind}。"
        else:
            ans = "当前未接入实时天气服务，天气信息暂不可用。"
        return {"answer": ans, "sources": sources}

    # ---------------------------------------------------------------- 入口
    def answer(self, question: str) -> Dict[str, Any]:
        """返回 {answer, sources, intent}。"""
        intent = intent_mod.classify(question)
        ctx = self._ctx.collect()
        if intent == intent_mod.INTENT_SCENE:
            out = self._answer_scene(ctx)
        elif intent == intent_mod.INTENT_RISK:
            out = self._answer_risk(ctx)
        elif intent == intent_mod.INTENT_SNAPSHOT:
            out = self._answer_snapshot(ctx)
        elif intent == intent_mod.INTENT_WEATHER:
            out = self._answer_weather(ctx)
        else:
            out = self._answer_general(ctx)
        return {"answer": out["answer"], "sources": out["sources"], "intent": intent}