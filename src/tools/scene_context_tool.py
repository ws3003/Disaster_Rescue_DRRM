"""src/tools/scene_context_tool.py — V4.4 统一现场上下文数据抽象层。

作用: 面向 VLM / LLM / RescueReasoner 提供统一、只读的现场上下文读取接口。

原则:
  - 只复用 src/tools/context_tool.get_current_scene_context()(SQLite/events 只读),
    不复制数据库读取代码、不重写 SQL。
  - 本阶段不改写 voice.py 的 _answer_scene_context/_answer_risk; 仅新增数据层。

结构:
  get_current_context() -> {
      "persons":      [{id, state, risk, risk_score, priority}, ...]
      "risk_summary": {"highest": "HIGH"|"MEDIUM"|"LOW"|None, "count": int}
      "events":       [ {event, ...}, ... ]        # 最近事件(可为空)
      "tasks":        [ {task_id, status, ...}, ...]  # 最近任务(可为空)
      "image":        None | <snapshot_url>        # 无则 None
      "timestamp":    <ISO 时间字符串>
  }
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


class SceneContextTool:
    """统一现场上下文读取(只读)。"""

    @staticmethod
    def _field(persons: List[dict]) -> List[dict]:
        """把 context_persons 映射为 {id,state,risk,risk_score,priority}。"""
        out: List[dict] = []
        for p in persons or []:
            if not isinstance(p, dict):
                continue
            out.append({
                "id": p.get("person_id"),
                "state": p.get("state"),
                "risk": p.get("priority"),          # HIGH/MEDIUM/LOW
                "risk_score": p.get("risk_score"),
                "priority": p.get("priority"),
            })
        return out

    @staticmethod
    def _risk_summary(persons: List[dict]) -> Dict[str, Any]:
        """最高风险等级 + 人员数。"""
        _order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        levels = [str(p.get("priority") or "").upper() for p in persons or []]
        valid = [l for l in levels if l in _order]
        highest = min(valid, key=lambda l: _order[l]) if valid else None
        return {"highest": highest, "count": len(persons or [])}

    def get_current_context(self) -> Dict[str, Any]:
        from .context_tool import get_current_scene_context  # 复用既有只读逻辑
        ctx = get_current_scene_context()

        persons = self._field(ctx.get("persons") or [])
        risk_summary = self._risk_summary(ctx.get("persons") or [])

        events: List[dict] = []
        if ctx.get("latest_event"):
            events.append({"type": ctx["latest_event"].get("type"),
                           "person_id": ctx["latest_event"].get("person_id"),
                           "severity": ctx["latest_event"].get("severity")})
        if ctx.get("latest_alert"):
            events.append({"type": "memory_alert",
                           "person_id": ctx["latest_alert"].get("person_id"),
                           "severity": ctx["latest_alert"].get("severity")})

        tasks: List[dict] = []
        if ctx.get("latest_task"):
            tasks.append({"task_id": ctx["latest_task"].get("task_id"),
                          "status": ctx["latest_task"].get("status"),
                          "trigger_event": ctx["latest_task"].get("trigger_event")})

        return {
            "persons": persons,
            "risk_summary": risk_summary,
            "events": events,
            "tasks": tasks,
            "image": ctx.get("snapshot_url"),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(SceneContextTool().get_current_context(),
                      ensure_ascii=False, default=str, indent=2))