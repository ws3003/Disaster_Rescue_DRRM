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
        # 优先返回 vision 人员携带的现场图片 URL(若存在)
        v = ctx.get("vision") if isinstance(ctx.get("vision"), dict) else {}
        img_url = next((p.get("image_url") for p in (v.get("persons") or [])
                        if p.get("image_url")), None)
        sources: list[Dict[str, Any]] = []
        if img_url:
            sources.append({"type": "vision", "image_url": img_url})
            ans = f"最近现场快照图片地址：{img_url}。"
        elif fname:
            sources.append({"type": "snapshot", "file": fname})
            ans = f"最近一张现场快照已保存为：{fname}（公网图片地址请通过 OSS 存储服务访问）。"
        else:
            ans = "当前暂无现场快照。"
        return {"answer": ans, "sources": sources}

    def _answer_general(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        ans = ("我可以为您提供信息辅助：可询问\"现场怎么样\"（场景状态）、"
               "\"有没有危险\"（风险）、\"图片在哪里\"（现场快照）、"
               "\"天气怎么样\"（天气）、\"在哪里\"（位置）、\"路线怎么走\"（路线）。"
               "系统仅提供信息，不发起任何救援动作。")
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

    def _answer_location(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        loc = ctx.get("location") or {}
        sources: list[Dict[str, Any]] = []
        place = loc.get("place")
        lat = loc.get("lat")
        lng = loc.get("lng")
        if place or (lat is not None and lng is not None):
            sources.append({"type": "map", "place": place, "lat": lat, "lng": lng})
            pos = place or f"({lat}, {lng})"
            ans = f"当前现场位置为：{pos}。"
        else:
            ans = "当前未连接地图服务，暂无法提供位置信息。"
        return {"answer": ans, "sources": sources}

    def _answer_route(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        m = ctx.get("map") or {}
        route = m.get("route") or {}
        sources: list[Dict[str, Any]] = []
        dist = route.get("distance_km")
        dur = route.get("duration_min")
        if dist is not None and dur is not None:
            sources.append({"type": "map", "route": {k: route.get(k)
                            for k in ("distance_km", "duration_min", "summary")}})
            ans = (f"建议路线距离约{dist}公里，预计{dur}分钟。"
                   + (f" 概要：{route.get('summary')}" if route.get("summary") else ""))
        else:
            ans = "当前未连接地图服务，暂无法提供路线建议。"
        return {"answer": ans, "sources": sources}

    def _answer_people(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        v = ctx.get("vision") if isinstance(ctx.get("vision"), dict) else {}
        persons = v.get("persons") or []
        sources: list[Dict[str, Any]] = [{"type": "vision", "ts": v.get("ts"),
                                          "count": len(persons)}]
        if not persons:
            return {"answer": "当前暂无现场人员状态信息。", "sources": sources}
        lines = [f"现场共识别到 {len(persons)} 人。"]
        high = []
        for p in persons:
            pid = p.get("id") or p.get("person_id")
            st = p.get("state")
            risk = p.get("risk") or p.get("priority")
            lines.append(f"- 人员 {pid}：状态 {st}，风险 {risk}。")
            if str(risk or "").upper() == "HIGH":
                high.append(pid)
        if high:
            lines.append("其中高风险人员请人工重点研判处理。")
        # 附带最近现场快照图片 URL(若 vision 人员带 image_url)
        img_url = next((p.get("image_url") for p in persons if p.get("image_url")), None)
        if img_url:
            lines.append(f"现场图片：{img_url}")
        return {"answer": "\n".join(lines), "sources": sources}

    # ---------------------------------------------------------------- 入口
    def answer(self, question: str) -> Dict[str, Any]:
        """返回 {answer, sources, intent}。"""
        intent = intent_mod.classify(question)
        ctx = self._ctx.collect()
        if intent == intent_mod.INTENT_SCENE:
            out = self._answer_scene(ctx)
        elif intent == intent_mod.INTENT_PEOPLE:
            out = self._answer_people(ctx)
        elif intent == intent_mod.INTENT_RISK:
            out = self._answer_risk(ctx)
        elif intent == intent_mod.INTENT_SNAPSHOT:
            out = self._answer_snapshot(ctx)
        elif intent == intent_mod.INTENT_WEATHER:
            out = self._answer_weather(ctx)
        elif intent == intent_mod.INTENT_LOCATION:
            out = self._answer_location(ctx)
        elif intent == intent_mod.INTENT_ROUTE:
            out = self._answer_route(ctx)
        else:
            out = self._answer_general(ctx)
        return {"answer": out["answer"], "sources": out["sources"], "intent": intent}