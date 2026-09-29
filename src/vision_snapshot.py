"""src/vision_snapshot.py — Vision Snapshot 数据层 (V4.1 Phase7)。

让 Assistant 读取灾害现场人员状态(V4.0 感知 person_events 的持久化层)。

- write_persons: 原子写最新人员快照(如测试可直接调用)
- read_latest  : 读取最新快照; 文件不存在/损坏返回 {}

结构:
    { "ts": "...", "persons": [ {"id":1,"state":"Fall","risk":"HIGH",...} ] }

本模块只做存储, 不修改 state/ranking/detector/pose/edge/agent。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_DEFAULT_PATH = "data/vision_persons.json"


def _norm_person(p: Any) -> Dict[str, Any]:
    if not isinstance(p, dict):
        return {}
    out = {
        "id": p.get("id", p.get("person_id")),
        "state": p.get("state"),
        "risk": p.get("risk", p.get("priority")),
        "priority": p.get("priority"),
        "risk_score": p.get("risk_score"),
        "bbox": p.get("bbox"),
    }
    # 丢弃 None 字段, 保持精简
    return {k: v for k, v in out.items() if v is not None}


def write_persons(persons: List[Any], ts: Optional[str] = None,
                  path: Optional[str] = None) -> Dict[str, Any]:
    """把最新人员列表原子写入快照。返回 {ok}。"""
    try:
        out_path = Path(path or _DEFAULT_PATH)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        clean = []
        for p in persons or []:
            n = _norm_person(p)
            if n.get("id") is not None:
                clean.append(n)
        payload = {
            "ts": ts or datetime.now().isoformat(timespec="seconds"),
            "persons": clean,
        }
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, out_path)  # 原子替换
        return {"ok": True, "count": len(clean)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def read_latest(path: Optional[str] = None) -> Dict[str, Any]:
    """读取最新视况快照; 文件不存在/解析失败返回 {}。"""
    p = Path(path or _DEFAULT_PATH)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except Exception:  # noqa: BLE001
        return {}


if __name__ == "__main__":
    print(write_persons([{"id": 1, "state": "Fall", "priority": "HIGH", "risk_score": 0.92},
                          {"id": 2, "state": "Standing", "priority": "LOW"}]))
    print(read_latest())