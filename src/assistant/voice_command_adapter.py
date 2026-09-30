"""src/assistant/voice_command_adapter.py — Voice 任务输入适配层 (V4.2).

只做"适配"，不执行：把命令封装成 VoiceTaskRequest，并把操作命令转成
既有 Agent 能消费的 legacy llm_output(供 Agent.ingest_llm_output → Planner → Executor → Tools)。
不伪造 vision frame; persons 取自真实 vision_snapshot(若有)。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..vision_snapshot import read_latest


def build_voice_task(intent: str, confirmed: bool,
                     persons: Optional[list] = None, payload: Optional[Dict] = None) -> Dict[str, Any]:
    """生成 VoiceTaskRequest。"""
    return {
        "source": "voice",
        "intent": intent,
        "confirmed": confirmed,
        "context": {"persons": persons or [], "payload": payload or {}},
    }


def latest_persons() -> list:
    """读取真实 vision 人员(供 rescue 类命令); 空则 []。"""
    d = read_latest()
    if isinstance(d, dict) and isinstance(d.get("persons"), list):
        return d["persons"]
    return []


def to_llm_output(intent: str, persons: list, payload: Optional[Dict] = None) -> Dict[str, Any]:
    """把操作命令转成 Agent 可跑的 legacy llm_output。"""
    payload = payload or {}
    pid = payload.get("person_id")
    target = [p for p in persons if str(p.get("id")) == str(pid)] if pid else [p for p in persons]
    persons_out = [
        {"person_id": p.get("id"), "state": p.get("state"),
         "risk": p.get("risk"), "risk_score": p.get("risk_score"),
         "priority": p.get("priority"), "bbox": p.get("bbox"),
         "image_url": p.get("image_url")}
        for p in persons if p.get("id") is not None
    ]
    proposals: list[Dict[str, Any]] = []
    if intent in ("needs_rescue", "max_risk"):
        proposals.append({"action": "get_ranking", "target": None, "payload": {}})
    if intent in ("send_alert", "notify_rescue") and persons_out:
        p0 = persons_out[0]
        proposals.append({
            "action": "send_alert",
            "target": p0["person_id"],
            "payload": {
                "severity": payload.get("severity", "HIGH"),
                "priority": p0.get("priority") or "HIGH",
                "state": p0.get("state") or "Unknown",
                "risk_score": p0.get("risk_score"),
                "message": payload.get("message") or f"{intent} 触发报警",
            },
        })
    return {
        "event_summary": f"voice command: {intent}",
        "risk_explanations": [],
        "rescue_suggestions": [],
        "task_proposals": proposals,
    }