"""src/assistant/agent_bridge.py — Voice 到既有 Agent 的安全桥 (V4.2).

白名单 + 二次确认; 确认后构造 legacy llm_output 喂既有 Agent.ingest_llm_output,
由 Agent → Planner → Executor → Tools 完成(rescue/communication 仍由 Agent 体系调用)。
不直连 communication_tool / rescue_tool; 不伪造 vision frame (persons 取真实快照)。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .command_router import ACTION_WHITELIST, DANGEROUS
from . import voice_command_adapter as adapter


def _agent_config() -> Dict[str, Any]:
    return {
        "agent": {"trigger": {"confirm_frames": 3, "high_risk_must_alert": True,
                              "heartbeat_minutes": 5},
                  "memory": {"cooldown_minutes": 30},
                  "planner": {"max_steps": 3, "cooldown_minutes": 30}},
        "db": {"path": os.environ.get("DRRM_DB", "data/drrm.db")},
        "vision": {"snapshot_path": "data/latest_vision.json",
                   "recent_path": "data/vision_snapshots.jsonl",
                   "scene_window_sec": 10},
        "communication": {"channel": os.environ.get("COMMUNICATION_CHANNEL", "mock"),
                          "max_retries": 1, "backoff_seconds": [1],
                          "timeout": 5, "log_path": "data/communication_log.jsonl",
                          "pending_path": "data/pending_communication.jsonl"},
        "rescue": {"snapshot_path": "data/latest_vision.json"},
    }


class AgentBridge:
    """action 命令网关。构造一次 Agent 跨命令复用(不新增第二套 Agent 流程)。"""

    def __init__(self, agent=None):
        # 惰性: 首次 action 时构建既有 Agent
        self._agent = agent
        self._overflow = False

    def _get_agent(self):
        if self._agent is None:
            from src.agent.agent import Agent
            self._agent = Agent(config=_agent_config())
        return self._agent

    def handle(self, intent: str, confirmed: bool = False,
               payload: Optional[Dict] = None) -> Dict[str, Any]:
        if intent not in ACTION_WHITELIST:
            return {"ok": False, "intent": intent, "error": "not an allowed action"}
        # 危险命令: 未确认 → 仅要求确认, 不触 Agent/comm
        if intent in DANGEROUS and not confirmed:
            return {"ok": False, "intent": intent, "require_confirm": True,
                    "confirmed": False, "confirm_message": f"确认执行{intent}吗？"}
        # 确认后 / 非危险 action → 进入既有 Agent
        persons = adapter.latest_persons()
        task = adapter.build_voice_task(intent, True, persons, payload)
        llm_output = adapter.to_llm_output(intent, persons, payload)
        # 传 context.image_url → 复用 Agent 既有图片注入(自动把 args["image_path"] 补上)
        image_url = (persons[0].get("image_url") if persons else None)
        context = {"image_url": image_url} if image_url else {}
        try:
            agent = self._get_agent()
            return {
                "ok": True,
                "intent": intent,
                "confirmed": True,
                "task": task,
                "agent_result": agent.ingest_llm_output(llm_output, context=context),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "intent": intent, "confirmed": True, "error": str(exc)}