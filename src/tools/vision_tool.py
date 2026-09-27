"""
src/tools/vision_tool.py — Vision Tool (P0-16)
===============================================
Agent 的"眼睛": 消费 Edge 已产生的视觉证据(不重新感知)。

数据流:
  Edge person_events
      ↓  VisionAdapter.to_snapshot()
  标准 snapshot
      ↓
  update_snapshot()
      ├── latest_vision.json         (最新一帧)
      └── vision_snapshots.jsonl      (recent, append-only)
      ↓
  get_snapshot(person_id)            单人视觉状态
  get_scene(window_sec)              最近时间窗口场景

约束: 不运行 YOLO / MediaPipe / 状态识别 / DRRM / ranking —— 只消费与存储。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tools.base import Tool  # noqa: E402
from src.tools.vision_adapter import (  # noqa: E402
    to_snapshot,
)

_DEFAULT_SNAPSHOT = "data/latest_vision.json"
_DEFAULT_RECENT = "data/vision_snapshots.jsonl"


class VisionTool(Tool):
    """视觉快照工具: 存储/读取 Edge 视觉证据 (P0-16)。"""

    def __init__(
        self,
        snapshot_path: str = _DEFAULT_SNAPSHOT,
        recent_path: str = _DEFAULT_RECENT,
    ):
        self.snapshot_path = Path(snapshot_path)
        self.recent_path = Path(recent_path)
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Tool 契约
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return "vision_tool"

    @property
    def description(self) -> str:
        return ("查询/存储 Edge 视觉证据: update_snapshot / get_snapshot / "
                "get_scene(window_sec)")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["update_snapshot", "get_snapshot", "get_scene"]},
                "person_events": {"type": "array",
                                  "description": "Edge person_events 列表"},
                "snapshot": {"type": "object", "description": "整帧 snapshot 字典"},
                "person_id": {"type": ["integer", "string"]},
                "window_sec": {"type": "number", "default": 10},
            },
            "required": ["action"],
            "additionalProperties": True,
        }

    def run(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action")
        if not action:
            return {"ok": False, "error": "Missing required parameter: action."}
        try:
            if action == "update_snapshot":
                return self._update_snapshot(kwargs)
            if action == "get_snapshot":
                return self._get_snapshot(kwargs)
            if action == "get_scene":
                return self._get_scene(kwargs)
            return {"ok": False, "error": f"Unsupported vision action: {action}",
                    "action": action}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "action": action}

    # ------------------------------------------------------------------
    # 最近帧缓冲 (append-only JSONL)
    # ------------------------------------------------------------------
    def _append_recent(self, snapshot: dict[str, Any]) -> None:
        with self.recent_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

    def _read_recent(self) -> List[dict[str, Any]]:
        if not self.recent_path.exists():
            return []
        out = []
        for line in self.recent_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 坏行跳过, 不崩溃
        return out

    # ------------------------------------------------------------------
    # update_snapshot: 接收 Edge person_events(或整帧 snapshot) → 落 latest + recent
    # ------------------------------------------------------------------
    def _update_snapshot(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        snapshot = kwargs.get("snapshot")
        person_events = kwargs.get("person_events")

        if snapshot is None and person_events is None:
            return {"ok": False, "action": "update_snapshot",
                    "error": "Missing required parameter: snapshot or person_events."}

        if snapshot is not None:
            if not isinstance(snapshot, dict):
                return {"ok": False, "action": "update_snapshot",
                        "error": "Parameter 'snapshot' must be a dictionary."}
            persons = snapshot.get("persons", [])
            if not isinstance(persons, list):
                return {"ok": False, "action": "update_snapshot",
                        "error": "Parameter 'snapshot.persons' must be a list."}
            timestamp = snapshot.get("timestamp") or datetime.now().isoformat(timespec="seconds")
            # 保留原始 snapshot 附加字段, 统一 persons 字段
            normalized = dict(snapshot)
            normalized["timestamp"] = timestamp
            normalized["persons"] = [
                p for p in persons if isinstance(p, dict) and p.get("person_id") is not None
            ]
        else:
            if not isinstance(person_events, list):
                return {"ok": False, "action": "update_snapshot",
                        "error": "Parameter 'person_events' must be a list."}
            normalized = to_snapshot(person_events)

        # 写 latest_vision.json
        self.snapshot_path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        # 追加 recent 记录
        self._append_recent(normalized)

        return {"ok": True, "action": "update_snapshot",
                "timestamp": normalized["timestamp"],
                "count": len(normalized["persons"])}

    # ------------------------------------------------------------------
    # get_snapshot(person_id)
    # ------------------------------------------------------------------
    def _get_snapshot(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if not self.snapshot_path.exists():
            return {"ok": True, "action": "get_snapshot", "snapshot": None,
                    "message": "No vision snapshot available."}
        content = self.snapshot_path.read_text(encoding="utf-8")
        if not content.strip():
            return {"ok": True, "action": "get_snapshot", "snapshot": None,
                    "message": "Vision snapshot is empty."}
        try:
            snapshot = json.loads(content)
        except json.JSONDecodeError:
            return {"ok": False, "action": "get_snapshot",
                    "error": "Vision snapshot is not valid JSON."}

        person_id = kwargs.get("person_id")
        if person_id is None:
            return {"ok": True, "action": "get_snapshot", "snapshot": snapshot}

        persons = snapshot.get("persons", []) if isinstance(snapshot, dict) else []
        for p in persons:
            if isinstance(p, dict) and str(p.get("person_id")) == str(person_id):
                return {
                    "ok": True,
                    "action": "get_snapshot",
                    "person_id": p.get("person_id"),
                    "state": p.get("state"),
                    "state_conf": p.get("state_conf"),
                    "risk_score": p.get("risk_score"),
                    "priority": p.get("priority"),
                    "bbox": p.get("bbox"),
                    "frame_idx": p.get("frame_idx"),
                    "annotated_frame_b64": p.get("annotated_frame_b64"),
                    "snapshot": snapshot,
                }

        return {"ok": False, "action": "get_snapshot", "person_id": str(person_id),
                "error": f"person not found in snapshot: {person_id}"}

    # ------------------------------------------------------------------
    # get_scene(window_sec)
    # ------------------------------------------------------------------
    def _get_scene(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        window_sec = kwargs.get("window_sec", 10)
        try:
            window_sec = float(window_sec)
        except (TypeError, ValueError):
            return {"ok": False, "action": "get_scene",
                    "error": "Parameter 'window_sec' must be a number."}
        if window_sec < 0:
            return {"ok": False, "action": "get_scene",
                    "error": "Parameter 'window_sec' must be >= 0."}

        now = datetime.now()
        frames: List[dict[str, Any]] = []
        for record in self._read_recent():
            if not isinstance(record, dict):
                continue
            ts = record.get("timestamp")
            try:
                parsed = datetime.fromisoformat(ts)
            except (TypeError, ValueError):
                continue  # 坏时间戳跳过
            age = (now - parsed).total_seconds()
            if 0 <= age <= window_sec:
                frames.append({"timestamp": ts, "persons": record.get("persons", [])})

        return {"ok": True, "action": "get_scene",
                "window_sec": window_sec, "frames": frames}


# ==============================================================
# Standalone test
# ==============================================================
if __name__ == "__main__":
    print("Testing VisionTool...")
    vision_tool = VisionTool()
    print(vision_tool.info())
    print("\n1. Update via person_events:")
    r1 = vision_tool.run(action="update_snapshot",
                         person_events=[{"person_id": 1, "state": "Fall",
                                         "risk_score": 0.6077, "priority": "HIGH"}])
    print(r1)
    print("\n2. Get snapshot(person 1):")
    print(vision_tool.run(action="get_snapshot", person_id=1))
    print("\n3. Get scene(window 10):")
    print(vision_tool.run(action="get_scene", window_sec=10))
    print("\n4. Missing update:")
    print(vision_tool.run(action="update_snapshot"))
    print(f"\nSnapshot file: {vision_tool.snapshot_path}")
    print(f"Recent file: {vision_tool.recent_path}")