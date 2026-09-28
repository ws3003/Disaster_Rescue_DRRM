"""
VLM prompts — 环境理解提示词模板 (P0)
=====================================
职责: 为灾害场景 VLM 生成结构化提示词 (System + User), 引导模型输出
符合 schemas/vlm_output.json 的 JSON。

定位约束: VLM 仅是"灾害现场视觉分析助手", 只做视觉理解
(灾害类型/危险因素/严重程度/环境描述), 不做人员调度或自动救援决策。

设计说明: 未来真实 VLM 输出可能不稳定, 因此 prompt 明确要求
"只输出 JSON、禁止 Markdown 包裹、禁止多余解释"。至于模型是否遵守,
全部交给 src/vlm/parser.py 去兼容(parser 已支持剥离 ```json 代码块与杂文本),
此处不做双重校验。
"""

from __future__ import annotations

import json
from typing import Optional


# 灾害场景类型白名单 —— 用于限制模型输出类别范围
SCENE_TYPES = [
    "earthquake",   # 地震
    "fire",         # 火情/火灾
    "flood",        # 洪涝/积水
    "collapse",     # 建筑/结构垮塌
    "unknown",      # 无法判定
]


# 输出 JSON 的结构示例, 与 schemas/vlm_output.json 对齐 (仅约束格式, 非期望值)
_JSON_FORMAT_EXAMPLE = {
    "scene_type": "earthquake",
    "overall_severity": "high",
    "hazards": [
        {"type": "collapse", "brief": "东北角墙体开裂, 有二次垮塌风险"}
    ],
    "environment_note": "对现场整体环境的一句话描述。",
}

# 输出 JSON 字段说明, 注入 System Prompt
_FIELD_SPEC = {
    "scene_type": "灾害类型, 只能取 {" + "|".join(SCENE_TYPES) + "} 之一",
    "overall_severity": "现场整体严重程度, 只能取 low/medium/high/critical 之一",
    "hazards": "危险因素数组, 每项含 type 与 brief 两个字段",
    "environment_note": "现场整体环境的自然语言描述",
}


def _format_field_spec() -> str:
    """将字段说明渲染为 prompt 中的编号列表。"""
    lines = []
    for name, desc in _FIELD_SPEC.items():
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "你是一名灾害现场视觉分析助手。你的唯一职责是基于输入图片, "
    "从视觉角度识别并描述灾害现场环境。\n"
    "你必须输出一个 JSON 对象, 且仅此一个 JSON, 不能包括任何其他文本。\n"
    "输出字段必须严格遵循以下定义:\n"
    f"{_format_field_spec()}\n"
    "输出格式示例如下(仅表示结构, 不代表这是一个真实现场):\n"
    f"{json.dumps(_JSON_FORMAT_EXAMPLE, ensure_ascii=False, indent=2)}\n"
    "约束:\n"
    "- 禁止使用 Markdown 代码块包裹 JSON。\n"
    "- 禁止输出任何解释、前言或后缀文本, 只输出 JSON。\n"
    "- 你只负责视觉理解与描述; 你不负责人员调度, 不负责处置执行, 不提出任何行动指令。"
)


# 预置输出格式要求(单独一段, 便于在 User Prompt 复用)
_JSON_OUTPUT_REQUIREMENT = (
    "请将你的视觉分析结果整理为一个 JSON 对象并整体输出。\n"
    "字段与取值约束见上述 System 说明; 若图片中无法判断灾害类型, "
    'scene_type 请填 "unknown", 并尽量用 environment_note 补充可观察信息。'
)


def build_scene_prompt(image_description: Optional[str] = None) -> str:
    """构造 VLM 用户侧 prompt。

    参数:
        image_description: 可选的图片文字描述/提示, 帮助模型定位分析重点。

    返回:
        一段完整用户 prompt, 包含场景描述、分析要求与 JSON 输出要求。
    """
    parts = [
        "请分析这张灾害现场图片。",
    ]
    if image_description:
        parts.append(f"现场背景参考: {image_description}")
    parts.append(
        "请从以下角度看图分析:\n"
        "- 属于什么灾害场景、涉及什么建筑/环境物;\n"
        "- 存在哪些可见危险因素(塌方、垮塌、火源、积水、电线等);\n"
        "- 整体严重程度如何;\n"
        "- 用一句话概括现场整体环境。"
    )
    parts.append(_JSON_OUTPUT_REQUIREMENT)
    return "\n".join(parts)