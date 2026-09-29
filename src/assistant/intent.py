"""Assistant 意图分类 (规则匹配, 不引入复杂模型)。

目的: 把用户自然语言问题归类为已知意图, 供 responder 选择模板回答。
仅规则关键词匹配, 依赖内置同义词, 可后续扩展。
"""

from __future__ import annotations

INTENT_SCENE = "scene_status"       # 询问现场状态/情况
INTENT_RISK = "risk_query"          # 询问风险/危险
INTENT_SNAPSHOT = "snapshot_query"  # 询问现场图片/快照
INTENT_WEATHER = "weather_query"    # 询问天气/温度/降雨
INTENT_GENERAL = "general_query"    # 其它

# (intent, 关键词元组) —— 顺序即优先级。
# 天气词(天气/温度/雨…)最专一, 置于最前, 避免与 scene 的通用词(怎么样/情况)冲突。
_PATTERNS = [
    (INTENT_WEATHER, ("天气", "下雨", "温度", "多少度", "晴", "雨", "风力", "weather")),
    (INTENT_SCENE, ("现场", "怎么样", "怎样", "状态", "情况", "目前")),
    (INTENT_RISK, ("危险", "风险", "危险吗", "是否危险", "有没有危险")),
    (INTENT_SNAPSHOT, ("图片", "照片", "快照", "图在", "现场图", "snapshot", "pic")),
]


def classify(question: str) -> str:
    """返回问题意图标签。未匹配时回退 general_query。"""
    if not isinstance(question, str):
        return INTENT_GENERAL
    q = question.strip().lower()
    if not q:
        return INTENT_GENERAL
    for intent, keywords in _PATTERNS:
        for kw in keywords:
            if kw in q:
                return intent
    return INTENT_GENERAL