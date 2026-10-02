"""src/assistant/voice_decision_router.py — V4.3 语音智能路由层。

定位: 依据用户问题关键词, 决定走 现场查询(scene) / 风险分析(risk) /
      救援建议(agent) 或交给命令路由(None)。

  route(text) -> "scene" | "risk" | "agent" | None
    - "scene": 灾害现场事实查询(人员/情况/任务/状态/数量)
    - "risk" : 风险分析/评估(危险吗/风险怎么样/最高风险/严重吗)
    - "agent": 救援建议/处理方案(怎么办/怎么处理/下一步/怎么救援)
    - None  : 非决策类问题(天气/医院/报警等), 交给上层既有命令路由, 不误判

刻意避免宽泛词: 不使用裸"有没有""是否"(会劫持 附近有没有医院 / WiFi 等)。
纯规则分类, 不调用 LLM / Agent / 数据库。
"""

from __future__ import annotations

from typing import Optional

# scene(灾害现场事实查询)关键词 —— 明确现场表达, 不含裸"有没有/是否"
_SCENE_KEYWORDS = (
    "现场有没有", "现场情况", "现场怎么样", "现场状态",          # 现场明确
    "当前几人", "当前多少人", "当前人员", "当前状态", "当前情况",  # 当前
    "最近任务", "当前任务", "任务状态", "任务是什么", "任务",   # 任务(救援任务属灾害场景)
    "伤员", "人员", "几个人", "多少人",                       # 人员/数量
)

# risk(风险分析/评估)关键词
_RISK_KEYWORDS = (
    "危险吗", "有危险吗", "风险吗", "风险怎么样", "最高风险",
    "严重吗", "现在危险", "情况严重吗",
)

# agent(救援建议/原因分析/决策辅助)关键词
_AGENT_KEYWORDS = (
    "建议", "怎么办", "怎么处理", "如何处理", "原因",
    "分析", "下一步", "怎么救援", "为什么",
)


class VoiceDecisionRouter:
    """按关键词把用户问题路由到 scene / risk / agent 模式。"""

    @staticmethod
    def route(text: str) -> Optional[str]:
        """返回 "scene" / "risk" / "agent" / None(不命中则交给上层命令路由)。

        判定顺序固定: 1)scene  2)risk  3)agent  4)None。
        """
        t = text or ""
        if any(kw in t for kw in _SCENE_KEYWORDS):
            return "scene"
        if any(kw in t for kw in _RISK_KEYWORDS):
            return "risk"
        if any(kw in t for kw in _AGENT_KEYWORDS):
            return "agent"
        return None