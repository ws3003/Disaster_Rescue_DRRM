"""src/tools/context_tool.py — V4.4 灾害现场上下文查询(只读 SQLite)。

定位: 面向救援人员的"现场情况"一问一答数据源。只读 data/drrm.db, 不建新库。

数据来源(复用 src/database/db.py 的 Database):
  - persons 表       → 人员(person_id/state/risk)
  - tasks 表         → 最新任务(latest_task)
  - events 表        → 最新报警(latest_alert) 与 人员 priority(从 payload JSON 恢复)
  - agent_logs 表    → 预留(当前未用, 保持读接口)

接口:
  get_current_scene_context(db_path=None, db=None) -> {
      persons: [{person_id, state, risk_score, priority}],
      latest_task: dict|None,
      latest_alert: dict|None,
  }
数据库不存在/为空 → 返回 {persons:[], latest_task:None, latest_alert:None}(不抛异常)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _default_db_path() -> str:
    return os.environ.get("DRRM_DB", "data/drrm.db")


def _recent_priority_map(db: Any, hours: int = 24) -> Dict[str, str]:
    """从最近事件 payload 恢复每位人员最近 priority(persons 表无此字段)。"""
    out: Dict[str, str] = {}
    try:
        events = db.get_recent_events(hours=hours)
    except Exception:  # noqa: BLE001
        return out
    for ev in events or []:
        pid = ev.get("person_id")
        if pid is None or pid in out:
            continue
        payload = ev.get("payload")
        if isinstance(payload, dict) and payload.get("priority"):
            out[str(pid)] = str(payload["priority"])
    return out


def _latest_task(db: Any) -> Optional[dict]:
    try:
        conn = db._connect()
        try:
            rows = conn.execute(
                "SELECT task_id, trigger_event, plan_trace, status, created, finished "
                "FROM tasks ORDER BY created DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        return dict(rows[0])
    except Exception:  # noqa: BLE001 — 表缺失/空库
        return None


def _latest_alert(db: Any) -> Optional[dict]:
    import json
    try:
        conn = db._connect()
        try:
            rows = conn.execute(
                "SELECT event_id, ts, type, severity, person_id, payload "
                "FROM events WHERE type = 'memory_alert' ORDER BY ts DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        row = dict(rows[0])
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                row["payload"] = json.loads(payload)
            except json.JSONDecodeError:
                row["payload"] = {}
        return row
    except Exception:  # noqa: BLE001
        return None


_ALERT_EVENT_TYPES = ("memory_alert", "high_risk_alert")


def _persons_from_events(db: Any, hours: int = 24) -> List[dict]:
    """persons 表为空时, 从 events(memory_alert/high_risk_alert)恢复人员。

    每个 person_id 取最近一次事件, 从 payload 恢复 state/risk_score/priority。
    """
    import json
    person_rows: Dict[tuple, dict] = {}  # person_id -> {state,risk,priority}
    try:
        conn = db._connect()
        try:
            rows = conn.execute(
                "SELECT event_id, ts, type, severity, person_id, payload "
                "FROM events WHERE type IN (?, ?) ORDER BY ts DESC",
                (_ALERT_EVENT_TYPES[0], _ALERT_EVENT_TYPES[1]),
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — 表缺失/空
        return []

    for row in rows:
        pid = row["person_id"]
        if pid is None or pid in person_rows:
            continue
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        state = payload.get("state")
        risk = payload.get("risk_score")
        priority = payload.get("priority")
        if state is None and risk is None:
            continue  # 无可用人员证据
        person_rows[pid] = {
            "state": state,
            "risk_score": risk,
            "priority": priority,
        }
    out: List[dict] = []
    for pid, info in person_rows.items():
        out.append({
            "person_id": pid,
            "state": info["state"],
            "risk_score": info["risk_score"],
            "priority": info["priority"],
        })
    return out


def get_current_scene_context(
    db_path: Optional[str] = None,
    db: Optional[Any] = None,
) -> Dict[str, Any]:
    """返回当前灾害现场上下文(只读 SQLite)。任何异常 → 空健康兜底。"""
    empty = {"persons": [], "latest_task": None, "latest_alert": None}
    if db is None:
        try:
            from src.database.db import Database
            db = Database(db_path or _default_db_path())
        except Exception:  # noqa: BLE001 — DB 不可用
            return empty

    # --- persons ---
    persons: List[dict] = []
    try:
        conn = db._connect()
        try:
            rows = conn.execute(
                "SELECT person_id, latest_state, latest_risk "
                "FROM persons ORDER BY last_seen DESC"
            ).fetchall()
        finally:
            conn.close()
        pri_map = _recent_priority_map(db)
        for r in rows:
            persons.append({
                "person_id": r["person_id"],
                "state": r["latest_state"],
                "risk_score": r["latest_risk"],
                "priority": pri_map.get(str(r["person_id"])),
            })
    except Exception:  # noqa: BLE001 — 表缺失等
        persons = []

    # persons 表为空 → fallback 从 events 恢复人员(memory_alert / high_risk_alert)
    if not persons:
        persons = _persons_from_events(db)

    return {
        "persons": persons,
        "latest_task": _latest_task(db),
        "latest_alert": _latest_alert(db),
    }


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(get_current_scene_context(), ensure_ascii=False, indent=2, default=str))