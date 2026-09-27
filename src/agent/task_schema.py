"""
Agent task schema validation (B9)
=================================
职责: 校验 agent_task / llm_output 数据是否符合 schemas/ 下的接口契约。

依据 docs/interface.md:
    - 模块间禁止直接依赖代码, 统一以 JSON 数据通信;
    - 消费方对缺失字段做兜底默认, 不崩溃。

实现说明:
    - 优先使用 jsonschema (draft-07) 做严格校验;
    - 未安装 jsonschema 时退化为内置结构校验 (零依赖, 满足 P0 mock 阶段)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ==========================================================
# Project root
# ==========================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SCHEMA_DIR = PROJECT_ROOT / "schemas"

AGENT_TASK_SCHEMA_PATH = SCHEMA_DIR / "agent_task.json"


# ==========================================================
# Optional jsonschema backend
# ==========================================================

try:
    import jsonschema

    _HAS_JSONSCHEMA = True
except ImportError:
    jsonschema = None
    _HAS_JSONSCHEMA = False


# ==========================================================
# Constants (mirror schemas/agent_task.json)
# ==========================================================

AGENT_TASK_REQUIRED_FIELDS = (
    "task_id",
    "trigger",
    "actions",
    "status",
)

AGENT_TASK_STATUS_ENUM = (
    "pending",
    "running",
    "done",
    "failed",
)

LLM_OUTPUT_FIELDS = (
    "event_summary",
    "risk_explanations",
    "rescue_suggestions",
    "task_proposals",
)


# ==========================================================
# Agent task validation
# ==========================================================

def validate_agent_task(
    task: Any,
) -> list[str]:
    """
    Validate one agent task against schemas/agent_task.json.

    Returns:
        List of error strings. Empty list means valid.

    Validation rules (draft-07 equivalent):
        - task must be an object;
        - task_id: required string;
        - trigger: required object;
        - actions: required array of {tool: string, args: object};
        - status: required enum pending/running/done/failed.
    """

    errors: list[str] = []

    # ------------------------------------------------------
    # Strict validation via jsonschema when available
    # ------------------------------------------------------

    if _HAS_JSONSCHEMA:

        try:

            schema = json.loads(
                AGENT_TASK_SCHEMA_PATH.read_text(
                    encoding="utf-8"
                )
            )

            validator = (
                jsonschema.Draft7Validator(
                    schema
                )
            )

            for error in validator.iter_errors(task):
                errors.append(
                    f"{list(error.absolute_path)}: {error.message}"
                )

            return errors

        except FileNotFoundError:

            errors.append(
                "Schema file not found: "
                f"{AGENT_TASK_SCHEMA_PATH}"
            )

            return errors

        except Exception as exc:

            # Never crash the agent loop on validator issues.
            errors.append(
                f"Schema validation error: {exc}"
            )

            return errors

    # ------------------------------------------------------
    # Built-in structural validation (no dependencies)
    # ------------------------------------------------------

    if not isinstance(task, dict):
        return ["Agent task must be a dictionary."]

    for field in AGENT_TASK_REQUIRED_FIELDS:

        if field not in task:
            errors.append(
                f"Missing required field: {field}"
            )

    if errors:
        return errors

    if not isinstance(task["task_id"], str):
        errors.append("task_id must be a string.")

    if not task.get("task_id"):
        errors.append("task_id must not be empty.")

    if not isinstance(task["trigger"], dict):
        errors.append("trigger must be an object.")

    if task.get("status") not in AGENT_TASK_STATUS_ENUM:
        errors.append(
            "status must be one of "
            f"{AGENT_TASK_STATUS_ENUM}."
        )

    actions = task["actions"]

    if not isinstance(actions, list):
        errors.append("actions must be an array.")
        return errors

    for index, action in enumerate(actions):

        if not isinstance(action, dict):
            errors.append(
                f"actions[{index}] must be an object."
            )
            continue

        tool = action.get("tool")

        if not isinstance(tool, str) or not tool:
            errors.append(
                f"actions[{index}].tool must be "
                "a non-empty string."
            )

        args = action.get("args")

        if args is None:
            action["args"] = {}
        elif not isinstance(args, dict):
            errors.append(
                f"actions[{index}].args must be "
                "an object."
            )

    return errors


# ==========================================================
# LLM output validation & fallback normalization
# ==========================================================

def validate_llm_output(
    llm_output: Any,
) -> list[str]:
    """
    Validate one llm_output payload (schemas/llm_output.json).

    Strict enough to catch structural corruption, but the
    consumer-side policy follows interface.md: missing optional
    fields fall back to defaults instead of failing.
    """

    if not isinstance(llm_output, dict):
        return ["llm_output must be a dictionary."]

    errors: list[str] = []

    if "event_summary" in llm_output and not isinstance(
        llm_output["event_summary"],
        str,
    ):
        errors.append("event_summary must be a string.")

    if "task_proposals" in llm_output and not isinstance(
        llm_output["task_proposals"],
        list,
    ):
        errors.append("task_proposals must be an array.")

    if "risk_explanations" in llm_output and not isinstance(
        llm_output["risk_explanations"],
        list,
    ):
        errors.append(
            "risk_explanations must be an array."
        )

    if "rescue_suggestions" in llm_output and not isinstance(
        llm_output["rescue_suggestions"],
        list,
    ):
        errors.append(
            "rescue_suggestions must be an array."
        )

    return errors


def normalize_llm_output(
    llm_output: Any,
) -> dict[str, Any]:
    """
    Apply interface.md fallback rules to one llm_output:

        - task_proposals missing  -> empty list
          (planner then falls back to rule-based triggering);
        - risk_explanations missing -> empty list;
        - rescue_suggestions missing -> empty list;
        - event_summary missing -> empty string.

    Returns a safe copy; the input dict is not modified.
    """

    if not isinstance(llm_output, dict):
        return {
            "event_summary": "",
            "risk_explanations": [],
            "rescue_suggestions": [],
            "task_proposals": [],
        }

    normalized = dict(llm_output)

    for field, default in (
        ("event_summary", ""),
        ("risk_explanations", []),
        ("rescue_suggestions", []),
        ("task_proposals", []),
    ):

        if field not in normalized or normalized[field] is None:
            normalized[field] = default

    return normalized


# ==========================================================
# Standalone test
# ==========================================================

if __name__ == "__main__":

    print("=" * 60)
    print("Testing Agent Task Schema Validation")
    print("=" * 60)

    print(
        f"\nBackend: "
        f"{'jsonschema' if _HAS_JSONSCHEMA else 'built-in'}"
    )

    # ------------------------------------------------------------
    # Test 1: valid agent task
    # ------------------------------------------------------------

    valid_task = {
        "task_id": "task_20260924_001",
        "trigger": {
            "source": "llm",
            "reason": "test",
            "ts": "2026-09-24T10:00:00",
        },
        "actions": [
            {
                "tool": "communication_tool",
                "args": {
                    "action": "send_alert",
                    "title": "Test",
                    "message": "Test message.",
                },
            },
        ],
        "status": "pending",
    }

    errors = validate_agent_task(valid_task)

    print("\n[1] Valid task errors:", errors)
    assert errors == []

    # ------------------------------------------------------------
    # Test 2: invalid agent task
    # ------------------------------------------------------------

    invalid_task = {
        "task_id": "",
        "trigger": "not-a-dict",
        "actions": [{"tool": 123}],
        "status": "unknown",
    }

    errors = validate_agent_task(invalid_task)

    print("\n[2] Invalid task errors:")
    for error in errors:
        print("   -", error)
    assert len(errors) >= 3

    # ------------------------------------------------------------
    # Test 3: missing actions
    # ------------------------------------------------------------

    errors = validate_agent_task(
        {"task_id": "t1", "status": "pending"}
    )

    print("\n[3] Missing fields errors:", errors)
    assert len(errors) >= 2

    # ------------------------------------------------------------
    # Test 4: llm_output fallback
    # ------------------------------------------------------------

    normalized = normalize_llm_output(None)

    print("\n[4] Fallback llm_output:", normalized)
    assert normalized["task_proposals"] == []
    assert normalized["event_summary"] == ""

    # ------------------------------------------------------------
    # Test 5: llm_output validation
    # ------------------------------------------------------------

    errors = validate_llm_output(
        {"event_summary": 123, "task_proposals": "x"}
    )

    print("\n[5] Bad llm_output errors:", errors)
    assert len(errors) == 2

    print("\n" + "=" * 60)
    print("task_schema test completed successfully.")
    print("=" * 60)
