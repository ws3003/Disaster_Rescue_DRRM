"""
src/agent/agent.py

Disaster Rescue DRRM Agent
--------------------------

Main orchestration layer:

    Trigger
       ↓
     Agent
       ↓
    Memory
       ↓
    Planner
       ↓
    Executor
       ↓
     Tools
       ↓
    SQLite
"""

from __future__ import annotations

import sys
import json
import inspect
from pathlib import Path
from datetime import datetime
from typing import Any


# ============================================================
# Project root compatibility
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Project imports
# ============================================================

from src.agent import triggers as triggers_module
from src.agent.memory import Memory
from src.agent.planner import Planner
from src.agent.executor import Executor


# ============================================================
# Agent
# ============================================================

class Agent:
    """
    Main orchestration layer of the DRRM system.

    Agent coordinates:

        Trigger
           ↓
        Memory
           ↓
        Planner
           ↓
        Executor
           ↓
        Tools
           ↓
        SQLite

    Agent does not directly execute individual tools.
    """

    def __init__(
        self,
        db_path: str | None = None,
        config: dict[str, Any] | None = None,
    ):

        cfg = config or {}
        if db_path is None:
            db_path = cfg.get("db", {}).get("path", "data/drrm.db")
        self.db_path = db_path

        agent_cfg = cfg.get("agent", {}) or {}

        # --------------------------------------------------------
        # Trigger module
        # --------------------------------------------------------

        trigger_cfg = agent_cfg.get("trigger", {}) or {}
        self.trigger_manager = self._create_trigger_manager(trigger=trigger_cfg)

        # --------------------------------------------------------
        # Memory
        # --------------------------------------------------------

        memory_cfg = agent_cfg.get("memory", {}) or {}
        memory_db = None
        if config is not None:
            from src.database.db import Database
            memory_db = Database(db_path=db_path)  # 复用 config.db.path
        self.memory = Memory(
            cooldown_minutes=memory_cfg.get("cooldown_minutes", 30),
            db=memory_db,
        )

        # --------------------------------------------------------
        # Planner
        # --------------------------------------------------------

        planner_cfg = agent_cfg.get("planner", {}) or {}
        self.planner = Planner(
            memory=self.memory,
            cooldown_minutes=planner_cfg.get("cooldown_minutes", 30),
            max_steps=planner_cfg.get("max_steps", 3),
        )

        # --------------------------------------------------------
        # Executor
        # --------------------------------------------------------

        tools_config = {
            "vision": cfg.get("vision", {}) or {},
            "communication": cfg.get("communication", {}) or {},
            "rescue": cfg.get("rescue", {}) or {},
        } if config is not None else None

        self.executor = Executor(
            db_path=db_path,
            tools_config=tools_config,
        )

        # --------------------------------------------------------
        # Runtime statistics
        # --------------------------------------------------------

        self.stats = {
            "triggers_received": 0,
            "plans_created": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "tasks_suppressed": 0,
        }

    # ============================================================
    # Trigger module compatibility
    # ============================================================

    def _create_trigger_manager(self, trigger: dict[str, Any] | None = None):
        """
        Create the trigger engine from the existing triggers.py.

        The current project may use a different class name inside
        triggers.py. Therefore Agent does not require the class to
        be specifically named TriggerManager.

        trigger: 可选配置 dict(confirm_frames/…), 传给兼容的 Trigger 构造器;
                 不支持这些参数的类会优雅回退到无参构造。
        """
        trigger_kwargs = dict(trigger or {})

        # --------------------------------------------------------
        # 1. Prefer commonly used trigger class names
        # --------------------------------------------------------

        preferred_names = [
            "TriggerManager",
            "TriggerEngine",
            "TriggerSystem",
            "TriggerProcessor",
            "TriggerHandler",
        ]

        for class_name in preferred_names:

            cls = getattr(
                triggers_module,
                class_name,
                None,
            )

            if cls is None:
                continue

            if not inspect.isclass(cls):
                continue

            try:
                return cls(**trigger_kwargs)
            except TypeError:
                try:
                    return cls()
                except TypeError:
                    try:
                        return cls(
                            memory=self.memory
                        )
                    except Exception:
                        continue

        # --------------------------------------------------------
        # 2. Automatically search trigger-related classes
        # --------------------------------------------------------

        for name, cls in inspect.getmembers(
            triggers_module,
            inspect.isclass,
        ):

            if cls.__module__ != triggers_module.__name__:
                continue

            lower_name = name.lower()

            if "trigger" not in lower_name:
                continue

            try:
                return cls(**trigger_kwargs)
            except Exception:
                try:
                    return cls()
                except Exception:
                    continue

        # --------------------------------------------------------
        # 3. No compatible trigger class
        # --------------------------------------------------------

        print(
            "[Agent] Warning: no trigger manager class "
            "was found in triggers.py."
        )

        return None

    # ============================================================
    # Main trigger processing
    # ============================================================

    def process_trigger(
        self,
        trigger: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Process one trigger:

            Trigger
                ↓
            Memory
                ↓
            Planner
                ↓
            Executor
        """

        if not isinstance(
            trigger,
            dict,
        ):

            return {
                "ok": False,
                "stage": "agent",
                "error": (
                    "Trigger must be a dictionary."
                ),
            }

        self.stats[
            "triggers_received"
        ] += 1

        # --------------------------------------------------------
        # Normalize
        # --------------------------------------------------------

        normalized_trigger = (
            self._normalize_trigger(
                trigger
            )
        )

        # --------------------------------------------------------
        # Memory
        # --------------------------------------------------------

        self._remember_trigger(
            normalized_trigger
        )

        # --------------------------------------------------------
        # Context
        # --------------------------------------------------------

        planner_context = (
            self._build_context(
                trigger=normalized_trigger,
                context=context,
            )
        )

        # --------------------------------------------------------
        # Planner
        # --------------------------------------------------------

        plan = self.planner.plan(
            normalized_trigger,
            context=planner_context,
        )

        if not isinstance(
            plan,
            dict,
        ):

            return {
                "ok": False,
                "stage": "planner",
                "error": (
                    "Planner returned "
                    "an invalid result."
                ),
                "trigger": normalized_trigger,
            }

        self.stats[
            "plans_created"
        ] += 1

        # --------------------------------------------------------
        # Suppressed
        # --------------------------------------------------------

        if plan.get(
            "status"
        ) == "suppressed":

            self.stats[
                "tasks_suppressed"
            ] += 1

            return {
                "ok": True,
                "stage": "planner",
                "status": "suppressed",
                "task_id": plan.get(
                    "task_id"
                ),
                "trigger_type": plan.get(
                    "trigger_type"
                ),
                "person_id": plan.get(
                    "person_id"
                ),
                "priority": plan.get(
                    "priority"
                ),
                "reason": plan.get(
                    "reason"
                ),
                "plan": plan,
            }

        # --------------------------------------------------------
        # Validate actions
        # --------------------------------------------------------

        actions = plan.get(
            "actions",
            [],
        )

        if not isinstance(
            actions,
            list,
        ):

            return {
                "ok": False,
                "stage": "planner",
                "error": (
                    "Planner actions "
                    "must be a list."
                ),
                "plan": plan,
            }

        # --------------------------------------------------------
        # No action
        # --------------------------------------------------------

        if len(actions) == 0:

            return {
                "ok": True,
                "stage": "planner",
                "status": "no_action",
                "task_id": plan.get(
                    "task_id"
                ),
                "trigger_type": plan.get(
                    "trigger_type"
                ),
                "plan": plan,
            }

        # --------------------------------------------------------
        # Executor
        # --------------------------------------------------------

        execution_result = (
            self.executor.execute(
                plan
            )
        )

        if not isinstance(
            execution_result,
            dict,
        ):

            return {
                "ok": False,
                "stage": "executor",
                "error": (
                    "Executor returned "
                    "an invalid result."
                ),
                "plan": plan,
            }

        # --------------------------------------------------------
        # Statistics
        # --------------------------------------------------------

        status = execution_result.get(
            "status"
        )

        if status == "completed":

            self.stats[
                "tasks_completed"
            ] += 1

        elif status == "failed":

            self.stats[
                "tasks_failed"
            ] += 1

        # 决策3: 仅在报警真正成功(communication_tool 返回 ok)时记 cooldown;
        # 通信失败不会调用 record_alert
        if isinstance(execution_result, dict):
            for _r in (execution_result.get("results", []) or []):
                if isinstance(_r, dict) and _r.get("tool") == "communication_tool" \
                        and _r.get("ok"):
                    self.memory.record_alert(
                        person_id=normalized_trigger.get("person_id"),
                        state=normalized_trigger.get("state"),
                        risk_score=normalized_trigger.get("risk_score"),
                        priority=normalized_trigger.get("priority"),
                    )
                    break

        # --------------------------------------------------------
        # Final result
        # --------------------------------------------------------

        return {
            "ok": bool(
                execution_result.get(
                    "ok",
                    False,
                )
            ),
            "stage": "completed",
            "status": status,
            "task_id": execution_result.get(
                "task_id"
            ),
            "trigger_type": execution_result.get(
                "trigger_type"
            ),
            "person_id": execution_result.get(
                "person_id"
            ),
            "priority": execution_result.get(
                "priority"
            ),
            "actions_total": execution_result.get(
                "actions_total"
            ),
            "actions_executed": execution_result.get(
                "actions_executed"
            ),
            "plan": plan,
            "execution": execution_result,
        }

    # ============================================================
    # Frame processing
    # ============================================================

    def process_frame(
        self,
        frame_record: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Process one structured perception frame.
        """

        if not isinstance(
            frame_record,
            dict,
        ):

            return [
                {
                    "ok": False,
                    "stage": "agent",
                    "error": (
                        "frame_record must "
                        "be a dictionary."
                    ),
                }
            ]

        # --------------------------------------------------------
        # Memory
        # --------------------------------------------------------

        self._remember_frame(
            frame_record
        )

        # --------------------------------------------------------
        # Trigger generation
        # --------------------------------------------------------

        trigger_result = (
            self._generate_triggers_from_frame(
                frame_record
            )
        )

        if trigger_result is None:

            return [
                {
                    "ok": False,
                    "stage": "trigger_manager",
                    "error": (
                        "The existing triggers.py "
                        "does not expose a supported "
                        "frame-processing method."
                    ),
                }
            ]

        # --------------------------------------------------------
        # Normalize result
        # --------------------------------------------------------

        if isinstance(
            trigger_result,
            dict,
        ):

            triggers = [
                trigger_result
            ]

        elif isinstance(
            trigger_result,
            list,
        ):

            triggers = trigger_result

        else:

            return [
                {
                    "ok": False,
                    "stage": "trigger_manager",
                    "error": (
                        "Unsupported trigger result type."
                    ),
                }
            ]

        # --------------------------------------------------------
        # Process each trigger
        # --------------------------------------------------------

        results = []

        for trigger in triggers:

            if not isinstance(
                trigger,
                dict,
            ):
                continue

            result = self.process_trigger(
                trigger=trigger,
                context=context,
            )

            results.append(
                result
            )

        return results

    # ============================================================
    # Try existing trigger API
    # ============================================================

    def _generate_triggers_from_frame(
        self,
        frame_record: dict[str, Any],
    ) -> Any:
        """
        Try the existing API exposed by triggers.py.

        This method intentionally supports several common names
        without changing triggers.py.
        """

        manager = (
            self.trigger_manager
        )

        if manager is None:
            return None

        # --------------------------------------------------------
        # Preferred
        # --------------------------------------------------------

        for method_name in [
            "process_frame",
            "check_frame",
            "update",
            "process",
            "handle_frame",
            "evaluate",
        ]:

            method = getattr(
                manager,
                method_name,
                None,
            )

            if method is None:
                continue

            if not callable(
                method
            ):
                continue

            try:

                return method(
                    frame_record
                )

            except TypeError:

                try:

                    return method(
                        frame=frame_record
                    )

                except Exception:
                    continue

            except Exception as exc:

                print(
                    "[Agent] Trigger warning:",
                    exc,
                )

                return None

        return None

    # ============================================================
    # Manual trigger
    # ============================================================

    def run_manual(
        self,
        person_id: int | None = None,
        reason: str = "Manual Agent test.",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate a MANUAL trigger.
        """

        trigger = {
            "trigger_type": "MANUAL",
            "trigger_event": "MANUAL",
            "person_id": person_id,
            "priority": "NORMAL",
            "risk_score": None,
            "frame_idx": None,
            "state": None,
            "reason": reason,
        }

        return self.process_trigger(
            trigger=trigger,
            context=context,
        )

    # ============================================================
    # Heartbeat
    # ============================================================

    def run_heartbeat(
        self,
        frame_idx: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate a HEARTBEAT trigger.
        """

        trigger = {
            "trigger_type": "HEARTBEAT",
            "trigger_event": "HEARTBEAT",
            "person_id": None,
            "priority": "NORMAL",
            "risk_score": None,
            "frame_idx": frame_idx,
            "state": None,
            "reason": "Periodic scene heartbeat.",
        }

        return self.process_trigger(
            trigger=trigger,
            context=context,
        )

    # ============================================================
    # JSONL
    # ============================================================

    def process_jsonl(
        self,
        jsonl_path: str | Path,
        max_frames: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Process JSONL perception records.
        """

        path = Path(
            jsonl_path
        )

        if not path.exists():

            return [
                {
                    "ok": False,
                    "stage": "jsonl",
                    "error": (
                        f"File not found: {path}"
                    ),
                }
            ]

        results = []

        frame_count = 0

        with path.open(
            "r",
            encoding="utf-8",
        ) as file:

            for line in file:

                line = line.strip()

                if not line:
                    continue

                try:

                    frame_record = json.loads(
                        line
                    )

                except json.JSONDecodeError as exc:

                    results.append(
                        {
                            "ok": False,
                            "stage": "jsonl",
                            "error": (
                                f"Invalid JSON: {exc}"
                            ),
                        }
                    )

                    continue

                frame_results = (
                    self.process_frame(
                        frame_record
                    )
                )

                results.extend(
                    frame_results
                )

                frame_count += 1

                if (
                    max_frames is not None
                    and frame_count >= max_frames
                ):

                    break

        return results

    # ============================================================
    # Memory
    # ============================================================

    def _remember_frame(
        self,
        frame_record: dict[str, Any],
    ) -> None:

        try:

            self.memory.remember_frame(
                frame_record
            )

        except Exception as exc:

            print(
                f"[Agent] Memory warning: {exc}"
            )

    def _remember_trigger(
        self,
        trigger: dict[str, Any],
    ) -> None:

        frame_record = {
            "frame_idx": trigger.get(
                "frame_idx"
            ),
            "person_id": trigger.get(
                "person_id"
            ),
            "state": trigger.get(
                "state"
            ),
            "risk_score": trigger.get(
                "risk_score"
            ),
            "priority": trigger.get(
                "priority"
            ),
            "trigger_type": trigger.get(
                "trigger_type"
            ),
            "timestamp": datetime.now().isoformat(),
        }

        self._remember_frame(
            frame_record
        )

    # ============================================================
    # Context
    # ============================================================

    def build_scene_context(
        self,
    ) -> dict[str, Any]:

        try:

            digest = self.memory.digest(
                window_minutes=30
            )

        except Exception:

            digest = ""

        return {
            "memory_digest": digest,
            "generated_at": (
                datetime.now().isoformat()
            ),
        }

    def _build_context(
        self,
        trigger: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:

        base_context = (
            self.build_scene_context()
        )

        base_context[
            "trigger"
        ] = trigger

        if context:

            base_context[
                "external_context"
            ] = context

        return base_context

    # ============================================================
    # Trigger normalization
    # ============================================================

    def _normalize_trigger(
        self,
        trigger: dict[str, Any],
    ) -> dict[str, Any]:

        normalized = dict(
            trigger
        )

        # --------------------------------------------------------
        # trigger_type
        # --------------------------------------------------------

        if not normalized.get(
            "trigger_type"
        ):

            normalized[
                "trigger_type"
            ] = normalized.get(
                "type",
                normalized.get(
                    "event_type",
                    "UNKNOWN_EVENT",
                ),
            )

        # --------------------------------------------------------
        # trigger_event
        # --------------------------------------------------------

        if not normalized.get(
            "trigger_event"
        ):

            normalized[
                "trigger_event"
            ] = normalized.get(
                "event",
                normalized[
                    "trigger_type"
                ],
            )

        # --------------------------------------------------------
        # person_id
        # --------------------------------------------------------

        if (
            "person_id"
            not in normalized
        ):

            normalized[
                "person_id"
            ] = normalized.get(
                "pid"
            )

        # --------------------------------------------------------
        # risk_score
        # --------------------------------------------------------

        if (
            "risk_score"
            not in normalized
        ):

            normalized[
                "risk_score"
            ] = normalized.get(
                "risk"
            )

        # --------------------------------------------------------
        # priority
        # --------------------------------------------------------

        if not normalized.get(
            "priority"
        ):

            risk_score = normalized.get(
                "risk_score"
            )

            if (
                isinstance(
                    risk_score,
                    (int, float),
                )
                and risk_score >= 0.8
            ):

                normalized[
                    "priority"
                ] = "HIGH"

            elif (
                isinstance(
                    risk_score,
                    (int, float),
                )
                and risk_score >= 0.5
            ):

                normalized[
                    "priority"
                ] = "MEDIUM"

            else:

                normalized[
                    "priority"
                ] = "NORMAL"

        return normalized

    # ============================================================
    # Statistics
    # ============================================================

    def get_stats(
        self,
    ) -> dict[str, Any]:

        return dict(
            self.stats
        )


# ================================================================
# Standalone test
# ================================================================

if __name__ == "__main__":

    print("=" * 60)
    print("Testing DRRM Agent")
    print("=" * 60)

    # ------------------------------------------------------------
    # Create Agent
    # ------------------------------------------------------------

    agent = Agent(
        db_path="data/drrm.db"
    )

    # ------------------------------------------------------------
    # Show detected trigger class
    # ------------------------------------------------------------

    if agent.trigger_manager is not None:

        print(
            "\n[Agent] Trigger module:",
            type(
                agent.trigger_manager
            ).__name__,
        )

    else:

        print(
            "\n[Agent] Trigger module: "
            "not automatically detected"
        )

    # ------------------------------------------------------------
    # Test 1: HIGH_RISK
    # ------------------------------------------------------------

    print(
        "\n[1] HIGH_RISK trigger"
    )

    high_trigger = {
        "trigger_type": "HIGH_RISK",
        "trigger_event": "HIGH_RISK",
        "person_id": 1,
        "state": "Fall",
        "risk_score": 0.92,
        "priority": "HIGH",
        "frame_idx": 1204,
    }

    result = agent.process_trigger(
        high_trigger
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
    )

    if not result.get(
        "ok"
    ):

        raise RuntimeError(
            "HIGH_RISK Agent test failed."
        )

    # ------------------------------------------------------------
    # Test 2: HEARTBEAT
    # ------------------------------------------------------------

    print(
        "\n[2] HEARTBEAT trigger"
    )

    heartbeat_result = (
        agent.run_heartbeat(
            frame_idx=500
        )
    )

    print(
        json.dumps(
            heartbeat_result,
            indent=2,
            ensure_ascii=False,
        )
    )

    if not heartbeat_result.get(
        "ok"
    ):

        raise RuntimeError(
            "HEARTBEAT Agent test failed."
        )

    # ------------------------------------------------------------
    # Test 3: MANUAL
    # ------------------------------------------------------------

    print(
        "\n[3] MANUAL trigger"
    )

    manual_result = (
        agent.run_manual(
            person_id=1,
            reason="Manual integration test.",
        )
    )

    print(
        json.dumps(
            manual_result,
            indent=2,
            ensure_ascii=False,
        )
    )

    if not manual_result.get(
        "ok"
    ):

        raise RuntimeError(
            "MANUAL Agent test failed."
        )

    # ------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------

    print(
        "\n[4] Agent statistics"
    )

    print(
        json.dumps(
            agent.get_stats(),
            indent=2,
            ensure_ascii=False,
        )
    )

    print(
        "\n" + "=" * 60
    )

    print(
        "Agent integration test completed successfully."
    )

    print(
        "=" * 60
    )