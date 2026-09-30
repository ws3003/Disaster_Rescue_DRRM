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

    _BAD_WEATHER = ("暴雨", "大雨", "台风", "大风", "雷暴", "冰雹", "大雾", "暴雪", "雾")

    def _answer_env_suitability(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """聚合天气 + 预警 + 人员风险 → 救援适宜性建议(信息辅助, 不自动行动)。"""
        sources: list[Dict[str, Any]] = []
        hazards: list[str] = []

        # ── 天气情况 ──
        w = ctx.get("weather")
        cond_txt = "未知"
        if isinstance(w, dict) and w:
            cond = w.get("condition") or "未知"
            temp = w.get("temperature")
            hum = w.get("humidity")
            cond_txt = str(cond)
            parts = [f"{cond}"]
            if temp is not None:
                parts.append(f"{temp}℃")
            if hum is not None:
                parts.append(f"湿度{hum}%")
            sources.append({"type": "weather", "condition": cond,
                            "temperature": temp, "humidity": hum})
            if any(b in cond_txt for b in self._BAD_WEATHER):
                hazards.append(f"当前天气（{cond_txt}）不利")
            weather_line = "，".join(parts)
        else:
            weather_line = "未取得实时天气数据"

        # ── 天气预警 ──
        warn = ctx.get("warning")
        if isinstance(warn, list) and warn:
            sources.append({"type": "weather_warning", "count": len(warn)})
            hazards.append(f"{len(warn)}项天气预警")

        # ── 现场人员情况 ──
        v = ctx.get("vision")
        persons = (v.get("persons") or []) if isinstance(v, dict) else []
        high = []
        for p in persons:
            risk = p.get("risk") or p.get("priority")
            risk_score = p.get("risk_score")
            if str(risk or "").upper() == "HIGH" \
                    or (isinstance(risk_score, (int, float)) and risk_score >= 0.6):
                high.append(p.get("id"))
        if high:
            sources.append({"type": "vision", "high_risk_count": len(high)})
            hazards.append(f"{len(high)}名人员处于高风险")

        # ── 综合输出 ──
        weather_risk = "、".join(hazards) if hazards else "无明显不利因素"
        people_line = (f"{len(persons)} 名人员"
                       + (f"，其中 {len(high)} 名高风险" if high else ""))
        if not hazards:
            advice = "总体较适宜开展救援，但仍请人工现场研判后再行动。"
        else:
            advice = ("存在上述不利因素，建议在做好防护且条件允许时开展救援，"
                      "并优先处置高风险人员；请人工研判决策。")
        ans = ("当前救援环境评估：\n"
               f"- 天气情况：{weather_line}\n"
               f"- 天气风险：{weather_risk}\n"
               f"- 现场人员情况：{people_line}\n"
               f"- 综合建议：{advice}\n"
               "（系统仅提供信息辅助，不自动决定是否救援。）")
        return {"answer": ans, "sources": sources}

    def _answer_warning(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        wlist = ctx.get("warning")
        sources: list[Dict[str, Any]] = []
        if isinstance(wlist, list) and wlist:
            sources.append({"type": "weather_warning", "count": len(wlist)})
            parts = ["当前存在以下天气预警："]
            for w in wlist:
                if not isinstance(w, dict):
                    continue
                level = w.get("level") or ""
                type_ = w.get("type") or w.get("title") or ""
                parts.append(f"- {type_}{level}预警")
            ans = "\n".join(parts) + " 请注意防范。"
        else:
            ans = "当前暂无天气预警。"
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

    def _answer_route(self, ctx: Dict[str, Any], question: str = "") -> Dict[str, Any]:
        # 优先: 从问题提取目的地 → 动态查询路线("现场" → 目标设施)
        dest = self._extract_destination(question)
        if dest:
            target = dest
            prefix = f"现场 → {dest}"
            # 设施泛称(医院/消防站/避难所/加油站): 先查最近真实 POI 名作 destination,
            # 避免高德无法 geocode 泛称。
            poi_list = self._ctx.query_nearby(dest)
            if isinstance(poi_list, list) and poi_list:
                first = poi_list[0] if isinstance(poi_list[0], dict) else {}
                name = first.get("name")
                if name:
                    target = name
                    prefix = f"现场 → {dest}({name})"
            route = self._ctx.query_route(origin="现场", destination=target)
            if route:
                return self._format_route(route, prefix=prefix)
        # 兜底: 保持旧逻辑 —— 读取 context 固定 route
        m = ctx.get("map") or {}
        route = m.get("route") or {}
        if route.get("distance_km") is not None and route.get("duration_min") is not None:
            return self._format_route(route, prefix=None)
        return {"answer": "当前未连接地图服务，暂无法提供路线建议。", "sources": []}

    def _format_route(self, route: Dict[str, Any], prefix: Optional[str]) -> Dict[str, Any]:
        dist = route.get("distance_km")
        dur = route.get("duration_min")
        sources: list[Dict[str, Any]] = [{"type": "map", "route": {k: route.get(k)
                for k in ("distance_km", "duration_min", "summary")}}]
        lines = []
        if prefix:
            lines.append(f"路线：{prefix}")
        if dist is not None:
            lines.append(f"距离：{dist}公里")
        if dur is not None:
            lines.append(f"预计时间：{dur}分钟")
        if route.get("summary"):
            lines.append(f"概要：{route.get('summary')}")
        return {"answer": "\n".join(lines) if lines else "当前暂无路线信息。", "sources": sources}

    # 附近设施/医院/消防站/避难所等关键词(优先于 location("哪里"))
    _NEARBY_KEYWORDS = ("消防站", "避难所", "避难", "加油站", "医院")

    def _extract_destination(self, question: str) -> Optional[str]:
        """从用户问题提取目标地点(医院/消防站/避难所/避难/加油站); 未匹配返回 None。"""
        if not isinstance(question, str):
            return None
        q = question
        for kw in ("消防站", "避难所", "避难", "加油站", "医院"):
            if kw in q:
                return kw
        return None

    def _answer_nearby(self, ctx: Dict[str, Any], question: str) -> Dict[str, Any]:
        keyword = None
        if isinstance(question, str):
            q = question.strip()
            for kw in self._NEARBY_KEYWORDS:
                if kw in q:
                    keyword = kw
                    break
        keyword = keyword or "医院"
        try:
            # query_nearby 是 AssistantContext 实例方法(非 collect() 的 dict)
            poi = self._ctx.query_nearby(keyword)
        except Exception:  # noqa: BLE001
            poi = None
        sources: list[Dict[str, Any]] = []
        if not poi:
            return {"answer": "当前暂无周边设施信息。", "sources": sources}
        sources.append({"type": "map", "nearby_keyword": keyword, "count": len(poi)})
        lines = [f"附近找到以下{keyword}设施："]
        for i, p in enumerate(poi, 1):
            if not isinstance(p, dict):
                continue
            name = p.get("name") or "—"
            dist = p.get("distance") or "距离未知"
            t = p.get("type") or keyword
            lines.append(f"{i}. {name}\n    类型：{t}\n    距离：{dist}")
        return {"answer": "\n".join(lines), "sources": sources}

    def _answer_nearby_rescue_resource(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """结合 vision 人员风险 + 地图 POI → 附近救援资源语义回答(信息辅助)。"""
        sources: list[Dict[str, Any]] = []

        # 人员统计
        v = ctx.get("vision") if isinstance(ctx.get("vision"), dict) else {}
        persons = v.get("persons") or []
        high = []
        for p in persons:
            risk = p.get("risk") or p.get("priority")
            rs = p.get("risk_score")
            if str(risk or "").upper() == "HIGH" \
                    or (isinstance(rs, (int, float)) and rs >= 0.6):
                high.append(p.get("id"))
        if persons:
            sources.append({"type": "vision", "persons": len(persons),
                            "high_risk": len(high)})

        kw_groups = (("医院",), ("消防站",), ("避难所",),)

        def _one_poi(keyword: str):
            try:
                poi_list = self._ctx.query_nearby(keyword)
                if isinstance(poi_list, list) and poi_list:
                    p0 = poi_list[0] if isinstance(poi_list[0], dict) else {}
                    return {"name": p0.get("name") or "—", "distance": p0.get("distance") or "—"}
            except Exception:  # noqa: BLE001
                return None
            return None

        lines = [f"当前现场检测到：{len(persons)} 名人员"
                 + (f"，其中 {len(high)} 名高风险。" if high else "。")]
        lines.append("附近救援资源：")
        for (kw,) in kw_groups:
            poi = _one_poi(kw)
            if poi:
                lines.append(f"{kw}：\n  - {poi['name']}（{poi['distance']}）")
                sources.append({"type": "map", "keyword": kw, "name": poi["name"]})
            else:
                lines.append(f"{kw}：暂无数据")

        high_tip = "请优先根据人员风险，关注就近医疗与救援资源并及时处置高风险人员；" if high else \
            "可结合现场人员状态评估就近救援资源需求。"
        lines.append(f"建议：{high_tip}")
        lines.append("系统仅提供信息辅助，不自动执行救援。")
        return {"answer": "\n".join(lines), "sources": sources}

    def _answer_rescue_route_priority(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """人员风险 + 附近医院 → geocode → 路线 → 救援推荐(信息辅助)。"""
        sources: list[Dict[str, Any]] = []

        v = ctx.get("vision") if isinstance(ctx.get("vision"), dict) else {}
        persons = v.get("persons") or []
        high = [p for p in persons
                if str((p.get("risk") or p.get("priority") or "")).upper() == "HIGH"
                or (isinstance(p.get("risk_score"), (int, float)) and p["risk_score"] >= 0.6)]
        if persons:
            sources.append({"type": "vision", "persons": len(persons), "high_risk": len(high)})

        try:
            poi_list = self._ctx.query_nearby("医院")
        except Exception:  # noqa: BLE001
            poi_list = None
        hospital = {}
        if isinstance(poi_list, list) and poi_list and isinstance(poi_list[0], dict):
            hospital = poi_list[0]
        name = hospital.get("name") or "最近医院"

        loc = None
        if name and name != "最近医院":
            loc = self._ctx.query_geocode(name)
        route = None
        if loc and loc.get("lat") is not None and loc.get("lng") is not None:
            route = self._ctx.query_route(origin="现场", destination=f"{loc['lng']},{loc['lat']}")
        elif name != "最近医院":
            route = self._ctx.query_route(origin="现场", destination=name)

        lines = [f"当前现场检测到：{len(persons)} 名人员"
                 + (f"，其中 {len(high)} 名高风险。" if high else "。")]
        lines.append(f"推荐救援目标：{name}")
        if route and route.get("distance_km") is not None:
            dist = route.get("distance_km")
            dur = route.get("duration_min")
            lines.append(f"路线信息：距离约 {dist} km，预计约 {dur} 分钟。"
                         if dur is not None else f"路线信息：距离约 {dist} km。")
            sources.append({"type": "map", "hospital": name,
                            "distance_km": dist, "duration_min": dur})
        else:
            lines.append("路线信息：暂无法获取路线。")
            sources.append({"type": "map", "hospital": name})

        lines.append("建议：优先关注高风险人员，并结合现场情况选择救援路线。")
        lines.append("系统仅提供信息辅助，不自动执行救援。")
        return {"answer": "\n".join(lines), "sources": sources}

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
        elif intent == intent_mod.INTENT_WARNING:
            out = self._answer_warning(ctx)
        elif intent == intent_mod.INTENT_ENV:
            out = self._answer_env_suitability(ctx)
        elif intent == intent_mod.INTENT_LOCATION:
            out = self._answer_location(ctx)
        elif intent == intent_mod.INTENT_ROUTE:
            out = self._answer_route(ctx, question)
        elif intent == intent_mod.INTENT_NEARBY:
            out = self._answer_nearby(ctx, question)
        elif intent == intent_mod.INTENT_RESCUE_RESOURCE:
            out = self._answer_nearby_rescue_resource(ctx)
        elif intent == intent_mod.INTENT_RESCUE_ROUTE:
            out = self._answer_rescue_route_priority(ctx)
        else:
            out = self._answer_general(ctx)
        return {"answer": out["answer"], "sources": out["sources"], "intent": intent}
