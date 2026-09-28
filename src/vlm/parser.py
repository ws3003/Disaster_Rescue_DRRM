"""
VLM parser — SceneAnalysis 校验与兜底 (P0)
==========================================
职责: 将 VLM 原始输出转换为符合 schemas/vlm_output.json 的数据结构。

支持以下输入形式:
  1. Python dict
  2. JSON 字符串
  3. 包裹 ```json ... ``` Markdown 代码块的 JSON 字符串

兜底规则(与 docs/interface.md 对齐):
  - scene_type 缺失/非法默认 "unknown"
  - overall_severity 缺失/非法默认 "medium"
  - hazards 缺失/非法默认空列表
  - environment_note 缺失默认 ""
真正无法提取出 JSON 字典的输入(垃圾文本等)抛出 SceneParseError,
不静默降级, 避免高危场景被当成安全场景。
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, Dict, List, Union


# vlm_output 必要字段与约束
_VALID_SEVERITIES = {"low", "medium", "high", "critical"}
_DEFAULT_SCENE_TYPE = "unknown"
_DEFAULT_SEVERITY = "medium"
_DEFAULT_NOTE = ""


class SceneParseError(ValueError):
    """输入既不是 dict 也无法提取出 JSON 字典时抛出。"""


def _extract_dict(raw: Union[dict, str]) -> dict:
    """统一将三种输入形式规整为顶层 dict。

    规整失败(无有效 JSON 对象)时抛出 SceneParseError。
    """
    if isinstance(raw, dict):
        return raw

    if not isinstance(raw, str):
        raise SceneParseError(f"不支持的输入类型: {type(raw).__name__}, 期望 dict/str")

    text = raw.strip()
    # 剥离 Markdown 代码块围栏 (```json ... ``` 或 ``` ... ```)
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SceneParseError(f"无法解析 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise SceneParseError(f"JSON 顶层应为对象, 实际为 {type(data).__name__}")
    return data


def _safe_str(value: Any, default: str = "") -> str:
    """字符串字段兜底: None/缺失用默认, 否则强转字符串。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_severity(value: Any) -> str:
    """严重等级字段兜底: 非法/缺失统一回退到 medium。"""
    if isinstance(value, str) and value in _VALID_SEVERITIES:
        return value
    return _DEFAULT_SEVERITY


def _coerce_hazards(value: Any) -> List[Dict[str, str]]:
    """hazards 数组兜底, 逐项规整为 {type, brief}。"""
    if not isinstance(value, list):
        return []
    items = []
    for hazard in value:
        if not isinstance(hazard, dict):
            continue
        items.append(
            {
                "type": _safe_str(hazard.get("type"), "unknown"),
                "brief": _safe_str(hazard.get("brief")),
            }
        )
    return items


@dataclasses.dataclass
class SceneAnalysis:
    """符合 schemas/vlm_output.json 的场景理解结果。"""

    scene_type: str = _DEFAULT_SCENE_TYPE
    overall_severity: str = _DEFAULT_SEVERITY
    hazards: List[Dict[str, str]] = dataclasses.field(default_factory=list)
    environment_note: str = _DEFAULT_NOTE

    def to_dict(self) -> Dict[str, Any]:
        """输出符合 vlm_output schema 的 dict。"""
        return {
            "scene_type": self.scene_type,
            "overall_severity": self.overall_severity,
            "hazards": self.hazards,
            "environment_note": self.environment_note,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SceneAnalysis":
        """从已校验/规整的 dict 构造实例。"""
        return cls(
            scene_type=_safe_str(data.get("scene_type"), _DEFAULT_SCENE_TYPE),
            overall_severity=_coerce_severity(data.get("overall_severity")),
            hazards=_coerce_hazards(data.get("hazards")),
            environment_note=_safe_str(data.get("environment_note"), _DEFAULT_NOTE),
        )


def parse(raw: Union[dict, str]) -> SceneAnalysis:
    """将 VLM 原始输出解析为 SceneAnalysis, 含字段级兜底。"""
    data = _extract_dict(raw)
    return SceneAnalysis.from_dict(data)


def validate(frame: dict) -> List[str]:
    """轻量校验一个 vlm_output 字典是否符合 schema 约束。

    返回问题列表; 空列表表示通过。与 jsonschema 校验互补,
    便于给出逐字段的可读错误。
    """
    issues: List[str] = []
    if not isinstance(frame, dict):
        return ["顶层必须为对象"]

    for field in ("scene_type", "overall_severity", "hazards", "environment_note"):
        if field not in frame:
            issues.append(f"缺少必需字段: {field}")

    if "scene_type" in frame and not isinstance(frame["scene_type"], str):
        issues.append("scene_type 必须为字符串")

    if "overall_severity" in frame:
        if not isinstance(frame["overall_severity"], str):
            issues.append("overall_severity 必须为字符串")
        elif frame["overall_severity"] not in _VALID_SEVERITIES:
            issues.append(
                f"overall_severity 取值非法: {frame['overall_severity']!r}, "
                f"应为 {sorted(_VALID_SEVERITIES)} 之一"
            )

    if "hazards" in frame:
        if not isinstance(frame["hazards"], list):
            issues.append("hazards 必须为数组")
        else:
            for i, hazard in enumerate(frame["hazards"]):
                if not isinstance(hazard, dict):
                    issues.append(f"hazards[{i}] 必须为对象")
                    continue
                if "type" in hazard and not isinstance(hazard["type"], str):
                    issues.append(f"hazards[{i}].type 必须为字符串")

    if "environment_note" in frame and not isinstance(frame["environment_note"], str):
        issues.append("environment_note 必须为字符串")

    return issues