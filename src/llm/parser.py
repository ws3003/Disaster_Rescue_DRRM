"""
LLM parser — LLMOutput 校验与兜底 (P0)
======================================
职责: 将 LLM 原始推理输出转换为符合 schemas/llm_output.json 的数据结构。

支持以下输入形式(与 src/vlm/parser.py 对称):
  1. Python dict
  2. JSON 字符串
  3. 包裹 ```json ... ``` Markdown 代码块的 JSON 字符串

兜底规则(与 docs/interface.md 对齐):
  - event_summary 缺失默认 ""
  - risk_explanations 缺失默认 []
  - rescue_suggestions 缺失默认 []
  - task_proposals 缺失默认 [] (下游 planner 收到空提议即走 rule-based, 不阻塞)
真正无法提取出 JSON 字典的输入抛出 LLMParseError, 不静默降级。
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, Dict, List, Optional, Union


# llm_output 必要字段
_DEFAULT_EMPTY_STR = ""


class LLMParseError(ValueError):
    """输入既不是 dict 也无法提取出 JSON 字典时抛出。"""


def _extract_dict(raw: Union[dict, str]) -> dict:
    """统一将三种输入形式规整为顶层 dict, 失败抛出 LLMParseError。"""
    if isinstance(raw, dict):
        return raw

    if not isinstance(raw, str):
        raise LLMParseError(f"不支持的输入类型: {type(raw).__name__}, 期望 dict/str")

    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMParseError(f"无法解析 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise LLMParseError(f"JSON 顶层应为对象, 实际为 {type(data).__name__}")
    return data


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_risk_explanations(value: Any) -> List[Dict[str, Any]]:
    """risk_explanations 数组兜底, 逐项规整为 {pid:int, reason:str}。

    pid 必须可转为整数, 否则丢弃该项(无法可靠定位人员)。
    """
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        items.append({"pid": pid, "reason": _safe_str(item.get("reason"))})
    return items


def _coerce_rescue_suggestions(value: Any) -> List[str]:
    """rescue_suggestions 数组兜底, 仅保留字符串项。"""
    if not isinstance(value, list):
        return []
    return [_safe_str(s) for s in value if isinstance(s, str)]


def _coerce_task_proposals(value: Any) -> List[Dict[str, Any]]:
    """task_proposals 数组兜底, 逐项规整为 {action, target, payload}。"""
    if not isinstance(value, list):
        return []
    items = []
    for proposal in value:
        if not isinstance(proposal, dict):
            continue
        action = _safe_str(proposal.get("action"), "log")

        target = proposal.get("target")
        if isinstance(target, bool):  # True/False 是 int 子类, 排除
            target = None
        elif not isinstance(target, int):
            # 兼容数字字符串, 其余按无目标处理
            target_nullable = None
            if isinstance(target, str) and target.strip().lstrip("-").isdigit():
                target_nullable = int(target)
            target = target_nullable

        payload = proposal.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        items.append({"action": action, "target": target, "payload": payload})
    return items


@dataclasses.dataclass
class LLMOutput:
    """符合 schemas/llm_output.json 的推理输出。"""

    event_summary: str = _DEFAULT_EMPTY_STR
    risk_explanations: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    rescue_suggestions: List[str] = dataclasses.field(default_factory=list)
    task_proposals: List[Dict[str, Any]] = dataclasses.field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_summary": self.event_summary,
            "risk_explanations": self.risk_explanations,
            "rescue_suggestions": self.rescue_suggestions,
            "task_proposals": self.task_proposals,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LLMOutput":
        return cls(
            event_summary=_safe_str(data.get("event_summary")),
            risk_explanations=_coerce_risk_explanations(data.get("risk_explanations")),
            rescue_suggestions=_coerce_rescue_suggestions(data.get("rescue_suggestions")),
            task_proposals=_coerce_task_proposals(data.get("task_proposals")),
        )


def parse(raw: Union[dict, str]) -> LLMOutput:
    """将 LLM 原始输出解析为 LLMOutput, 含字段级兜底。"""
    data = _extract_dict(raw)
    return LLMOutput.from_dict(data)


def validate(frame: dict) -> List[str]:
    """轻量校验一个 llm_output 字典是否符合 schema 约束。"""
    issues: List[str] = []
    if not isinstance(frame, dict):
        return ["顶层必须为对象"]

    for field in ("event_summary", "risk_explanations", "rescue_suggestions", "task_proposals"):
        if field not in frame:
            issues.append(f"缺少必需字段: {field}")

    if "event_summary" in frame and not isinstance(frame["event_summary"], str):
        issues.append("event_summary 必须为字符串")

    if "risk_explanations" in frame:
        if not isinstance(frame["risk_explanations"], list):
            issues.append("risk_explanations 必须为数组")
        else:
            for i, item in enumerate(frame["risk_explanations"]):
                if not isinstance(item, dict) or "pid" not in item:
                    issues.append(f"risk_explanations[{i}] 必须为含 pid 的对象")
                    continue
                if not isinstance(item["pid"], int) or isinstance(item["pid"], bool):
                    issues.append(f"risk_explanations[{i}].pid 必须为整数")

    if "rescue_suggestions" in frame and not isinstance(frame["rescue_suggestions"], list):
        issues.append("rescue_suggestions 必须为数组")

    if "task_proposals" in frame:
        if not isinstance(frame["task_proposals"], list):
            issues.append("task_proposals 必须为数组")
        else:
            for i, item in enumerate(frame["task_proposals"]):
                if not isinstance(item, dict) or "action" not in item:
                    issues.append(f"task_proposals[{i}] 必须为含 action 的对象")
                    continue
                if not isinstance(item["action"], str):
                    issues.append(f"task_proposals[{i}].action 必须为字符串")

    return issues