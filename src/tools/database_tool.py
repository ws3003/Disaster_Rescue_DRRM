from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


# ==========================================================
# Add project root to Python module search path
# ==========================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ==========================================================
# Import Tool and Database
# ==========================================================

from src.tools.base import Tool
from src.database.db import Database


class DatabaseTool(Tool):
    """
    Tool wrapper for database operations.

    Supported actions:
        - save_event
        - get_recent_events
        - upsert_person
        - save_task
        - save_agent_log
    """

    # ==========================================================
    # Tool metadata
    # ==========================================================

    @property
    def name(self) -> str:
        return "database_tool"

    @property
    def description(self) -> str:
        return (
            "Persist and retrieve disaster "
            "response data from SQLite."
        )

    # ==========================================================
    # Initialization
    # ==========================================================

    def __init__(
        self,
        db_path: str = "data/drrm.db",
    ):
        # 稳定解析到项目根, 不依赖 CWD:
        # - 相对路径(默认 data/drrm.db) → <project_root>/data/drrm.db
        # - 显式绝对路径 → 原样保留
        _p = Path(db_path or "data/drrm.db")
        resolved = str(_p) if _p.is_absolute() else str(PROJECT_ROOT / _p)
        self.db = Database(
            db_path=resolved
        )

    # ==========================================================
    # Main Tool interface
    # ==========================================================

    def run(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:

        action = kwargs.get(
            "action"
        )

        if not action:
            return {
                "ok": False,
                "error": (
                    "Missing required field: action."
                ),
            }

        try:

            # ==================================================
            # 1. save_event
            # ==================================================

            if action == "save_event":

                event_id = self.db.save_event(
                    type=kwargs.get(
                        "type",
                        "unknown",
                    ),
                    severity=kwargs.get(
                        "severity"
                    ),
                    person_id=kwargs.get(
                        "person_id"
                    ),
                    payload=kwargs.get(
                        "payload",
                        {},
                    ),
                )

                return {
                    "ok": True,
                    "action": action,
                    "event_id": event_id,
                }

            # ==================================================
            # 2. get_recent_events
            # ==================================================

            elif action == "get_recent_events":

                hours = kwargs.get(
                    "hours",
                    1,
                )

                events = self.db.get_recent_events(
                    hours=hours
                )

                return {
                    "ok": True,
                    "action": action,
                    "hours": hours,
                    "events": events,
                    "count": len(events),
                }

            # ==================================================
            # 3. upsert_person
            # ==================================================

            elif action == "upsert_person":

                person_id = kwargs.get(
                    "person_id"
                )

                state = kwargs.get(
                    "state"
                )

                risk = kwargs.get(
                    "risk"
                )

                if person_id is None:
                    return {
                        "ok": False,
                        "action": action,
                        "error": (
                            "person_id is required."
                        ),
                    }

                if state is None:
                    return {
                        "ok": False,
                        "action": action,
                        "error": (
                            "state is required."
                        ),
                    }

                if risk is None:
                    return {
                        "ok": False,
                        "action": action,
                        "error": (
                            "risk is required."
                        ),
                    }

                self.db.upsert_person(
                    person_id=int(
                        person_id
                    ),
                    state=str(
                        state
                    ),
                    risk=float(
                        risk
                    ),
                )

                return {
                    "ok": True,
                    "action": action,
                    "person_id": int(
                        person_id
                    ),
                    "state": str(
                        state
                    ),
                    "risk": float(
                        risk
                    ),
                }

            # ==================================================
            # 4. save_task
            # ==================================================

            elif action == "save_task":

                task = kwargs.get(
                    "task"
                )

                if not isinstance(
                    task,
                    dict,
                ):
                    return {
                        "ok": False,
                        "action": action,
                        "error": (
                            "task must be a dictionary."
                        ),
                    }

                task_id = self.db.save_task(
                    task
                )

                return {
                    "ok": True,
                    "action": action,
                    "task_id": task_id,
                }

            # ==================================================
            # 5. save_agent_log
            # ==================================================
            #
            # IMPORTANT:
            #
            # ToolRegistry.execute() already has:
            #
            #     execute(tool_name, **kwargs)
            #
            # Therefore the parameter identifying the Tool
            # being logged is named "logged_tool_name".
            #
            # This prevents:
            #
            #     got multiple values for argument 'tool_name'
            #
            # ==================================================

            elif action == "save_agent_log":

                task_id = kwargs.get(
                    "task_id"
                )

                logged_tool_name = kwargs.get(
                    "logged_tool_name"
                )

                input_data = kwargs.get(
                    "input",
                    {},
                )

                output_data = kwargs.get(
                    "output",
                    {},
                )

                latency_ms = kwargs.get(
                    "latency_ms",
                    0,
                )

                if not task_id:
                    return {
                        "ok": False,
                        "action": action,
                        "error": (
                            "task_id is required."
                        ),
                    }

                if not logged_tool_name:
                    return {
                        "ok": False,
                        "action": action,
                        "error": (
                            "logged_tool_name "
                            "is required."
                        ),
                    }

                self.db.save_agent_log(
                    task_id=task_id,
                    tool_name=logged_tool_name,
                    input=input_data,
                    output=output_data,
                    latency_ms=int(
                        latency_ms
                    ),
                )

                return {
                    "ok": True,
                    "action": action,
                    "task_id": task_id,
                    "tool_name": logged_tool_name,
                    "latency_ms": int(
                        latency_ms
                    ),
                }

            # ==================================================
            # Unknown action
            # ==================================================

            else:

                return {
                    "ok": False,
                    "action": action,
                    "error": (
                        f"Unsupported database "
                        f"action: {action}"
                    ),
                }

        except Exception as exc:

            return {
                "ok": False,
                "action": action,
                "error": (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                ),
            }


# ==============================================================
# Standalone test
# ==============================================================

if __name__ == "__main__":

    print(
        "Testing DatabaseTool..."
    )

    tool = DatabaseTool()

    # ==========================================================
    # 1. Test save_event
    # ==========================================================

    result = tool.run(
        action="save_event",
        type="test_event",
        severity="LOW",
        person_id=1,
        payload={
            "source": "database_tool_test"
        },
    )

    print(
        "\n1. save_event:"
    )

    print(
        result
    )

    assert result["ok"] is True

    # ==========================================================
    # 2. Test get_recent_events
    # ==========================================================

    result = tool.run(
        action="get_recent_events",
        hours=1,
    )

    print(
        "\n2. get_recent_events:"
    )

    print(
        result
    )

    assert result["ok"] is True

    # ==========================================================
    # 3. Test upsert_person
    # ==========================================================

    result = tool.run(
        action="upsert_person",
        person_id=999,
        state="NORMAL",
        risk=0.1,
    )

    print(
        "\n3. upsert_person:"
    )

    print(
        result
    )

    assert result["ok"] is True

    # ==========================================================
    # 4. Test save_task
    # ==========================================================

    result = tool.run(
        action="save_task",
        task={
            "task_id": "DB-TEST-001",
            "trigger_event": "TEST",
            "plan_trace": [],
            "status": "running",
        },
    )

    print(
        "\n4. save_task:"
    )

    print(
        result
    )

    assert result["ok"] is True

    # ==========================================================
    # 5. Test save_agent_log
    # ==========================================================

    result = tool.run(
        action="save_agent_log",
        task_id="DB-TEST-001",
        logged_tool_name="vision_tool",
        input={
            "action": "get_snapshot"
        },
        output={
            "ok": True
        },
        latency_ms=5,
    )

    print(
        "\n5. save_agent_log:"
    )

    print(
        result
    )

    assert result["ok"] is True

    print(
        "\nDatabaseTool test completed successfully."
    )