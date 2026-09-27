"""
DRRM Agent Contract Pipeline Test (llm_output -> agent_task)

Test scenario (docs/interface.md):

    data/mock/llm_output.json
            |
            v
    Planner.plan_from_llm_output()
            |
            v
    data/agent_task.json  (schemas/agent_task.json)
            |
            v
    Executor.execute_agent_task()
            |
            v
    communication_tool (mock alert)
            |
            v
    database_tool (save_event)
            |
            v
    SQLite (events / tasks / agent_logs)

P0 原则: 全程 mock, 不接真实服务。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


# ============================================================
# Project root
# ============================================================

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(
    os.path.join(CURRENT_DIR, "..", "..")
)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


from src.agent.memory import Memory
from src.agent.planner import Planner
from src.agent.executor import Executor
from src.agent.task_schema import validate_agent_task


# ============================================================
# Helpers
# ============================================================

def print_json(data: Any) -> None:
    print(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


# ============================================================
# Main
# ============================================================

def main() -> None:

    print("=" * 70)
    print("DRRM AGENT CONTRACT PIPELINE TEST")
    print("llm_output.json -> Planner -> agent_task.json -> Executor")
    print("=" * 70)

    # --------------------------------------------------------
    # 1. Load mock llm_output (member A contract artifact)
    # --------------------------------------------------------

    llm_output_path = os.path.join(
        PROJECT_ROOT,
        "data",
        "mock",
        "llm_output.json",
    )

    with open(
        llm_output_path,
        "r",
        encoding="utf-8",
    ) as file:
        llm_output = json.load(file)

    print("\n[1] Loaded mock llm_output:")
    print_json(
        {
            "event_summary": llm_output.get(
                "event_summary"
            )[:40]
            + "...",
            "task_proposals": len(
                llm_output.get("task_proposals", [])
            ),
        }
    )

    # --------------------------------------------------------
    # 2. Planner: translate proposals into agent_task
    # --------------------------------------------------------

    memory = Memory()
    planner = Planner(memory=memory)

    task = planner.plan_from_llm_output(llm_output)

    print("\n[2] Planner produced agent_task:")
    print_json(
        {
            "task_id": task.get("task_id"),
            "status": task.get("status"),
            "actions_count": task.get("actions_count"),
            "actions": [
                {
                    "tool": a.get("tool"),
                    "action": a.get("args", {}).get(
                        "action"
                    ),
                    "person_id": a.get("args", {}).get(
                        "person_id"
                    ),
                }
                for a in task.get("actions", [])
            ],
        }
    )

    assert task["ok"] is True
    assert task["status"] == "pending"
    assert task["actions_count"] >= 1
    assert task["actions_count"] <= 3

    # --------------------------------------------------------
    # 3. Schema validation (B9)
    # --------------------------------------------------------

    errors = validate_agent_task(task)

    print("\n[3] Schema validation errors:", errors)

    assert errors == []

    # --------------------------------------------------------
    # 4. Persist the contract artifact
    # --------------------------------------------------------

    artifact_path = os.path.join(
        PROJECT_ROOT,
        "data",
        "agent_task.json",
    )

    saved = planner.save_agent_task(
        task,
        output_path=artifact_path,
    )

    print(
        "\n[4] agent_task.json saved:",
        saved,
        "->",
        artifact_path,
    )

    assert saved is True

    # --------------------------------------------------------
    # 5. Executor: run the contract task
    # --------------------------------------------------------

    executor = Executor()

    result = executor.execute_agent_task(task)

    print("\n[5] Execution result:")
    print_json(
        {
            "ok": result.get("ok"),
            "status": result.get("status"),
            "actions_total": result.get(
                "actions_total"
            ),
            "actions_executed": result.get(
                "actions_executed"
            ),
        }
    )

    assert result["ok"] is True
    assert result["status"] == "done"

    # --------------------------------------------------------
    # 6. Verify tool side effects
    # --------------------------------------------------------

    # 6a. communication_log.jsonl (mock channel)

    log_path = os.path.join(
        PROJECT_ROOT,
        "data",
        "communication_log.jsonl",
    )

    alert_count = 0

    if os.path.exists(log_path):

        with open(
            log_path,
            "r",
            encoding="utf-8",
        ) as file:
            for line in file:
                if line.strip():
                    alert_count += 1

    print(
        "\n[6a] Mock channel log entries:",
        alert_count,
    )

    assert alert_count >= 1

    # 6b. SQLite events

    recent = executor.registry.execute(
        "database_tool",
        action="get_recent_events",
        hours=1,
    )

    print(
        "\n[6b] Recent events in SQLite:",
        len(
            recent.get("events", [])
        ),
    )

    assert recent.get("ok") is True

    # --------------------------------------------------------
    # 7. Fallback: empty proposals -> suppressed no-op
    # --------------------------------------------------------

    fallback_task = planner.plan_from_llm_output(
        {"event_summary": "no proposals"}
    )

    print(
        "\n[7] Fallback (no proposals):",
        fallback_task.get("status"),
    )

    assert fallback_task["ok"] is True
    assert fallback_task["status"] == "suppressed"
    assert fallback_task["actions"] == []

    # Suppressed tasks are planner-internal outcomes and are
    # intentionally NOT sent to the executor (they would fail
    # schema validation, which only accepts pending/running/
    # done/failed).

    # Contract rule: a VALID task with empty actions is a
    # no-op -> status=done, nothing persisted.

    noop = executor.execute_agent_task(
        {
            "task_id": "task_noop_001",
            "trigger": {
                "source": "llm",
                "reason": "empty actions test",
                "ts": "2026-09-24T00:00:00",
            },
            "actions": [],
            "status": "pending",
        }
    )

    print(
        "\n[7b] Empty-actions contract task:",
        noop.get("status"),
    )

    assert noop["status"] == "done"
    assert noop["actions_total"] == 0

    # --------------------------------------------------------
    # Passed
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("TEST PASSED")
    print("=" * 70)

    print(
        "\nContract pipeline verified:\n"
        "  1. llm_output.json loaded (member A mock)\n"
        "  2. Planner translated task_proposals\n"
        "  3. agent_task.json passes schema validation\n"
        "  4. Contract artifact written to data/agent_task.json\n"
        "  5. Executor executed tool actions\n"
        "  6. Mock alert recorded + SQLite persisted\n"
        "  7. Empty-proposal fallback suppressed"
    )


if __name__ == "__main__":
    main()
