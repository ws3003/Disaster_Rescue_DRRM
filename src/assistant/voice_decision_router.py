"""src/assistant/voice_decision_router.py — V4.3 语音智能路由层。

定位: 依据用户问题关键词, 决定走"简单现场查询(scene)"还是"救援决策(agent)"。

  route(text) -> "scene" | "agent" | None
    - "scene": 灾害现场事实查询(现场人员/情况/任务/状态/数量)
    - "agent": 风险分析/救援决策(危险/风险/为什么/怎么办/建议/是否→去"是否")
    - None  : 非决策类问题(天气/医院/报警等), 交给上层既有命令路由, 不误判

刻意避免宽泛词: 不使用裸"有没有""是否"(会劫持 附近有没有医院 / WiFi 等),
scene 词必须是灾害现场明确表达; pure 规则, 不调用 LLM / Agent / 数据库。
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

# agent(风险分析/救援决策)关键词
_AGENT_KEYWORDS = (
    "危险", "风险", "为什么", "怎么办", "建议",
    "原因", "分析", "能不能", "适合", "进入", "处理",
    "评估", "判断", "先救", "要不要",
)


class VoiceDecisionRouter:
    """按关键词把用户问题路由到 scene / agent 模式。"""

    @staticmethod
    def route(text: str) -> Optional[str]:
        """返回 "scene" / "agent" / None(不命中则交给上层命令路由)。"""
        t = text or ""
        # 优先 scene(灾害现场事实); 命中即返回
        if any(kw in t for kw in _SCENE_KEYWORDS):
            return "scene"
        if any(kw in t for kw in _AGENT_KEYWORDS):
            return "agent"
        return None