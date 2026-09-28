"""
LLM prompts — 参谋 system prompt 模板 (P0)
=========================================
职责: 为灾害救援"决策支持"LLM 生成提示词, 输入 vlm_output + person_events
(环境证据 + 人员风险证据), 输出符合 schemas/llm_output.json 的 JSON。

角色定位: 灾害救援分析参谋 —— 只负责分析 / 提出建议 / 生成任务候选。
不负责直接执行救援、自动发送报警或控制设备。动作仅限信息类提议,
最终是否执行、如何执行由下游 Agent 系统决定。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List


# LLM 可提议的动作词表 —— 解决 task_proposals.action 与 Agent actions/tool 的映射。
# 模型只能从这里取值作为任务建议的动作, 不直接调用任何工具。
TASK_PROPOSAL_VOCAB: Dict[str, str] = {
    "send_alert": "对指定 person(person_id) 发送报警",
    "save_event": "记录一次事件(可为 null 目标)",
    "get_snapshot": "查询视觉快照(person_id 或 null)",
    "get_ranking": "查询救援排序(person_id 或 null)",
    "assess_person": "评估指定 person(必须指向有效 person_id)",
}


def _format_vocab() -> str:
    """将动作词表渲染为 prompt 中带含义说明的列表。"""
    lines = []
    for action, meaning in TASK_PROPOSAL_VOCAB.items():
        lines.append(f"- {action}: {meaning}")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "你是一名灾害救援分析参谋, 为现场指挥提供决策支持。"
    "你会拿到两方面的证据: 环境理解结果(VLM 对现场的观察)与人员风险数据(边缘感知对每名人员的状态与风险评分)。"
    "你的职责是根据这些证据, 生成一份结构化推理输出, 包括事件总结、逐一人员的风险解释、救援建议与任务候选。\n"
    "你必须输出一个 JSON 对象, 且仅此一个 JSON, 不能包括任何其他文本。\n"
    "输出字段必须严格遵循以下定义:\n"
    "- event_summary: 对本次灾害事件的整体总结, 一句话\n"
    "- risk_explanations: 数组, 每项含 pid(人员编号, 对应传入的人员 person_id) 与 reason(该人员风险分级的原因)\n"
    "- rescue_suggestions: 数组, 每条为一条面向现场指挥的救援建议\n"
    "- task_proposals: 数组, 建议下游执行的任务候选; 每项为 {action, target, payload}, "
    "action 必须取自下列词表, target 必须为人员数据中已存在的 person_id(非数组下标, 无特定对象则 null), "
    "payload 仅用于补充如 message 等说明, 禁止携带 state/risk_score/priority 等人员事实字段\n"
    "动作词表(只能取其一):\n"
    f"{_format_vocab()}\n"
    "人员事实权威: 传入的人员 person_id/state/risk_score/priority 是外部 observation 提供的"
    "Edge 事实, 你只能读取并据此推理, 不得修改、不得重新判断、不得生成新的 state/risk_score/priority; "
    "也不得生成 state_conf/bbox/frame_idx/observation_id/timestamp。\n"
    "职责边界:\n"
    "- 你只负责分析、提出建议、生成任务候选。\n"
    "- 你不负责直接执行救援, 不负责报警发送, 不负责设备控制; 你不代替下游系统作出最终处置。\n"
    "- 若无确凿证据支撑某动作, 不要凭空编造 task_proposals。\n"
    "- 禁止使用 Markdown 代码块包裹 JSON, 只输出 JSON。"
)


def _render_evidence(vlm_output: Dict[str, Any], person_events: List[Dict[str, Any]]) -> str:
    """将环境与人员证据整理为 JSON 注入文本。"""
    env_json = json.dumps(vlm_output, ensure_ascii=False, indent=2)
    persons_json = json.dumps(person_events, ensure_ascii=False, indent=2)
    return (
        "以下是本次灾害现场的输入证据, 请以此为依据进行分析:\n"
        "\n【环境理解结果 vlm_output】\n"
        f"{env_json}\n"
        "\n【人员风险数据 person_events】(每人一条, person_id 与 pid 对应)\n"
        f"{persons_json}"
    )


def build_advice_prompt(vlm_output: Dict[str, Any], person_events: List[Dict[str, Any]]) -> str:
    """构造 LLM 用户侧 prompt, 注入环境与人员证据。

    参数:
        vlm_output: 符合 schemas/vlm_output.json 的字典。
        person_events: 人员风险事件列表, 每项含 person_id/state/risk_score 等。

    返回:
        一段完整用户 prompt, 包含证据 JSON 与输出要求。
    """
    evidence = _render_evidence(vlm_output, person_events)
    return (
        f"{evidence}\n\n"
        "请基于上述证据, 综合分析现场环境风险与人员风险, "
        "按 System 中的字段定义输出一个符合要求的 JSON, "
        "包含事件总结、人员风险解释、救援建议与任务候选。"
    )