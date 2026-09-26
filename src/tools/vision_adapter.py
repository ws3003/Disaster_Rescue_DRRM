"""
src/tools/vision_adapter.py — Vision Adapter (P0-16)
=====================================================
职责: 把 Raspberry Pi Edge 已产出的 person_events 转换为 B 侧 Vision Tool
统一消费的标准 snapshot。只做**数据格式转换**, 不运行任何视觉算法。

   Edge person_events
        ↓                to_snapshot()
   standard snapshot { timestamp, persons:[...] }

明确不做: YOLO / MediaPipe / Detector / PoseEstimator / StateClassifier /
ranking 重写 / DRRM 重算 / VLM / LLM —— 视觉感知永远属于 Edge。

字段规范: 未提供的字段统一置 null, 绝不伪造:
  person_id / state / state_conf / risk_score / priority /
  bbox / frame_idx / annotated_frame_b64
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 标准 snapshot per-person 字段声明 (未提供 → null / 默认)
_FIELDS = (
    "person_id",
    "state",
    "state_conf",
    "risk_score",
    "priority",
    "bbox",
    "frame_idx",
    "annotated_frame_b64",
)

_DEFAULTS = {
    "person_id": None,
    "state": None,
    "state_conf": None,
    "risk_score": None,
    "priority": None,
    "bbox": None,
    "frame_idx": None,
    "annotated_frame_b64": None,
}


def normalize_person(ev: Dict[str, Any]) -> Dict[str, Any]:
    """把一条 Edge person_event 规范为 standard snapshot 人员项。"""
    out: Dict[str, Any] = {}
    for field in _FIELDS:
        value = ev.get(field)
        if value is None:
            value = _DEFAULTS[field]
            if field == "priority":
                value = None
            if field == "state_conf":
                value = None
        out[field] = value
    # priority 统一大写 (若有)
    if out["priority"] is not None:
        out["priority"] = str(out["priority"]).upper()
    return out


def to_snapshot(
    person_events: List[Dict[str, Any]],
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """把 Edge person_events 列表转换为标准 snapshot 字典。"""
    if timestamp is None:
        timestamp = datetime.now().isoformat(timespec="seconds")
    persons: List[Dict[str, Any]] = []
    for ev in person_events:
        if not isinstance(ev, dict):
            continue
        if ev.get("person_id") is None:
            continue  # 无人员编号则不进入 snapshot
        persons.append(normalize_person(ev))
    return {"timestamp": timestamp, "persons": persons}