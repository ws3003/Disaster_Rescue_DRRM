"""src/assistant/command_router.py — 语音命令路由 (V4.2).

纯分类: 文本/意图 → Command{intent, category, require_confirm, payload}。
不调用任何 tool / Agent / communication。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

INFO_COMMANDS = {"scene_status", "count_query", "fall_query", "weather", "plan_route",
                 "env_suitability", "nearby_rescue_resource", "rescue_route_priority"}
ACTION_WHITELIST = {"needs_rescue", "max_risk", "send_alert", "notify_rescue"}
DANGEROUS = {"send_alert", "notify_rescue"}

# 关键词 → 命令
_CMD_KEYWORDS: List[tuple] = [
    ("send_alert", ("发送报警", "报警", "发报警")),
    ("notify_rescue", ("通知救援", "叫救援", "呼叫救援")),
    ("needs_rescue", ("谁需要救援", "哪些人需要救援")),
    ("max_risk", ("最高风险", "最危险", "风险最高")),
    ("scene_status", ("查看现场", "现场情况", "现场怎么样", "现场状态")),
    ("count_query", ("多少人", "几个人", "当前几人", "人数")),
    ("fall_query", ("倒地", "有没有倒地", "倒下")),
    ("weather", ("天气", "温度")),
    ("env_suitability", ("适合救援", "适宜救援", "救援条件", "环境适合", "能否救援", "适合开展救援")),
    ("plan_route", ("规划路线", "救援路线", "怎么走")),
    ("nearby_rescue_resource", ("附近哪里可以救援", "哪里可以救援", "附近医院在哪里",
                                "附近消防在哪里", "附近消防站在哪里", "最近避难点",
                                "附近救援资源", "附近的避难所在哪")),
    ("rescue_route_priority", ("最近医院路线", "去医院怎么走", "去最近医院怎么走",
                               "救援路线", "规划救援路线", "导航到医院", "规划路线")),
]


def _find_intent(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    for intent, kws in _CMD_KEYWORDS:
        for kw in kws:
            if kw in t:
                return intent
    return None


def classify(text: str) -> Dict[str, Any]:
    """返回 Command 对象 dict。未知命令 -> category="reject"。"""
    intent = _find_intent(text)
    if intent is None:
        return {"intent": "unknown", "category": "reject", "require_confirm": False,
                "payload": {}}
    category = "action" if intent in ACTION_WHITELIST else "info"
    require_confirm = intent in DANGEROUS
    return {"intent": intent, "category": category,
            "require_confirm": require_confirm, "payload": {}}


def is_action(intent: str) -> bool:
    return intent in ACTION_WHITELIST