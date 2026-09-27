"""
src/agent/input_adapter.py — 生产输入适配 (P0-19/21/22)
=======================================================
把 Edge / 感知层已产出的 person_events 标准列表转换为 Agent / Trigger
消费的 frame_record。

  Edge person_events ([{person_id, state, risk_score, priority, ...}])
        ↓  person_events_to_frame_record()
  frame_record = {timestamp, persons:[...]}

约束: 本模块只做**数据格式适配**——不运行 YOLO / MediaPipe / 状态识别 /
ranking / DRRM / VLM / LLM, 不访问数据库, 不触发 Agent。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Edge person_event 必需键 (轻量校验: 缺 person_id 的记录丢弃)
_REQUIRED = ("person_id",)


def person_events_to_frame_record(
    person_events: List[Dict[str, Any]],
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """把 Edge person_events 列表转换为 Agent 可消费的 frame_record。

    - 保留每条事件的全部字段 (person_id/state/risk_score/priority 及其它已有字段);
    - 缺失 person_id 的记录跳过;
    - 补充 frame 级 timestamp。
    """
    persons: List[Dict[str, Any]] = []
    for ev in person_events or []:
        if not isinstance(ev, dict):
            continue
        if any(key not in ev for key in _REQUIRED):
            continue
        persons.append(dict(ev))  # 保留原字段, 不伪造

    if timestamp is None:
        timestamp = datetime.now().isoformat(timespec="seconds")

    frame_record: Dict[str, Any] = {
        "timestamp": timestamp,
        "persons": persons,
    }
    return frame_record