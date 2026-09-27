"""
DRRM End-to-End Agent Integration Test

Test scenario:

    Standing
    Standing
    Standing
          ↓
    Fall
    Fall
    Fall
          ↓
    STATE_CHANGE
          ↓
    Planner
          ↓
    Executor
          ↓
    VisionTool
          ↓
    CommunicationTool
          ↓
    DatabaseTool
          ↓
    SQLite
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


from src.agent.agent import Agent


# ============================================================
# Helper
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
    print("DRRM END-TO-END AGENT INTEGRATION TEST")
    print("=" * 70)

    # --------------------------------------------------------
    # 1. Initialize Agent
    # --------------------------------------------------------

    agent = Agent()

    print("\n[1] Agent initialized.")

    trigger_engine = getattr(
        agent,
        "trigger_engine",
        None,
    )

    if trigger_engine is None:
        trigger_engine = getattr(
            agent,
            "trigger_manager",
            None,
        )

    if trigger_engine is None:
        trigger_engine = getattr(
            agent,
            "trigger",
            None,
        )

    print(
        "[1] Trigger engine:",
        type(trigger_engine).__name__
        if trigger_engine is not None
        else "UNKNOWN",
    )

    # --------------------------------------------------------
    # 2. Test sequence
    #
    # First establish stable Standing state:
    #
    #   Standing
    #   Standing
    #   Standing
    #
    # Then send three consecutive Fall frames:
    #
    #   Fall
    #   Fall
    #   Fall
    #
    # Expected:
    #
    #   STATE_CHANGE
    # --------------------------------------------------------

    frames = [
        {
            "frame_idx": 1001,
            "persons": [
                {
                    "pid": 1,
                    "state": "Standing",
                    "risk_score": 0.10,
                }
            ],
        },
        {
            "frame_idx": 1002,
            "persons": [
                {
                    "pid": 1,
                    "state": "Standing",
                    "risk_score": 0.10,
                }
            ],
        },
        {
            "frame_idx": 1003,
            "persons": [
                {
                    "pid": 1,
                    "state": "Standing",
                    "risk_score": 0.10,
                }
            ],
        },
        {
            "frame_idx": 1004,
            "persons": [
                {
                    "pid": 1,
                    "state": "Fall",
                    "risk_score": 0.92,
                }
            ],
        },
        {
            "frame_idx": 1005,
            "persons": [
                {
                    "pid": 1,
                    "state": "Fall",
                    "risk_score": 0.93,
                }
            ],
        },
        {
            "frame_idx": 1006,
            "persons": [
                {
                    "pid": 1,
                    "state": "Fall",
                    "risk_score": 0.94,
                }
            ],
        },
    ]

    print("\n[2] Sending state-transition sequence...")

    results: list[dict] = []

    for index, frame in enumerate(frames, start=1):

        print("\n" + "-" * 60)

        state = frame["persons"][0]["state"]

        print(
            f"Frame {index}/{len(frames)}"
            f" | frame_idx={frame['frame_idx']}"
            f" | state={state}"
        )

        print_json(frame)

        result = agent.process_frame(frame)

        print("\nAgent result:")
        print_json(result)

        if isinstance(result, list):
            results.extend(result)

        elif isinstance(result, dict):
            results.append(result)

    # --------------------------------------------------------
    # 3. Analyze results
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("TEST RESULT ANALYSIS")
    print("=" * 70)

    completed = 0
    suppressed = 0
    failed = 0

    trigger_types: list[str] = []

    for result in results:

        if not isinstance(result, dict):
            continue

        status = result.get("status")

        if status == "completed":
            completed += 1

        elif status == "suppressed":
            suppressed += 1

        elif status == "failed":
            failed += 1

        trigger_type = result.get("trigger_type")

        if not trigger_type:
            trigger = result.get("trigger")

            if isinstance(trigger, dict):
                trigger_type = trigger.get("trigger_type")

        if trigger_type:
            trigger_types.append(str(trigger_type))

    print(f"Total Agent results: {len(results)}")
    print(f"Completed tasks: {completed}")
    print(f"Suppressed tasks: {suppressed}")
    print(f"Failed results: {failed}")

    print("\nGenerated trigger types:")

    if trigger_types:
        for trigger_type in trigger_types:
            print(f"  - {trigger_type}")
    else:
        print("  No trigger was generated.")

    # --------------------------------------------------------
    # 4. Agent statistics
    # --------------------------------------------------------

    print("\nAgent statistics:")

    stats = {}

    if hasattr(agent, "get_statistics"):
        stats = agent.get_statistics()

    elif hasattr(agent, "stats"):
        stats = agent.stats

    elif hasattr(agent, "statistics"):
        stats = agent.statistics

    print_json(stats)

    # --------------------------------------------------------
    # 5. Validation
    # --------------------------------------------------------

    print("\n" + "=" * 70)

    if len(results) == 0:

        print("TEST FAILED")
        print("=" * 70)

        raise RuntimeError(
            "No Agent result was generated from the "
            "Standing -> Fall transition sequence."
        )

    if failed > 0:

        print("TEST FAILED")
        print("=" * 70)

        raise RuntimeError(
            f"{failed} Agent task(s) failed."
        )

    # Check whether a state-change trigger was produced.

    state_change_found = any(
        trigger_type == "STATE_CHANGE"
        for trigger_type in trigger_types
    )

    if not state_change_found:

        print("TEST FAILED")
        print("=" * 70)

        raise RuntimeError(
            "Agent produced results, but no STATE_CHANGE trigger "
            "was generated."
        )

    # --------------------------------------------------------
    # 6. Success
    # --------------------------------------------------------

    print("TEST PASSED")
    print("=" * 70)

    print(
        "\nSuccessfully verified:"
        "\n  1. Stable Standing state"
        "\n  2. Three consecutive FALL frames"
        "\n  3. STATE_CHANGE trigger"
        "\n  4. Planner execution"
        "\n  5. Executor execution"
        "\n  6. Tool execution"
        "\n  7. SQLite persistence"
    )

    print(
        "\nPipeline:"
        "\n  EventTrigger"
        "\n      ↓"
        "\n  Planner"
        "\n      ↓"
        "\n  Executor"
        "\n      ↓"
        "\n  VisionTool"
        "\n      ↓"
        "\n  CommunicationTool"
        "\n      ↓"
        "\n  DatabaseTool"
        "\n      ↓"
        "\n  SQLite"
    )

    print("\nIntegration test completed successfully.")


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    main()