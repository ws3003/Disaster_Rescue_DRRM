"""
Agent planner — 轻量规划器(P0)
===============================
职责: 三段式编排: 规则触发 + LLM 辅助 + 工具调用。
本文件为入口骨架, 待实现业务逻辑。
"""


# 轻量规划器(待实现)
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import json
import sys
import uuid


# ==========================================================
# Add project root to Python module search path
# ==========================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ==========================================================
# Import project modules
# ==========================================================

from src.agent.memory import Memory


class Planner:
    """
    Lightweight Agent Planner for Disaster Rescue DRRM.

    Responsibilities:
        1. Receive trigger events from triggers.py
        2. Check Memory cooldown
        3. Apply deterministic safety rules
        4. Apply action whitelist
        5. Generate a structured task plan
        6. Keep each task within the maximum tool-step limit

    IMPORTANT:
        Planner decides WHAT actions should be executed.

        Executor decides HOW those actions are actually executed.

    Planner does NOT:
        - run YOLO
        - run MediaPipe
        - call VLM directly
        - call communication APIs directly
        - write SQLite directly
        - execute rescue operations
    """

    # ==========================================================
    # Allowed actions
    #
    # These names correspond to the PRD task proposal schema.
    # ==========================================================

    ALLOWED_ACTIONS = {
        "get_snapshot",
        "send_alert",
        "save_event",
        "get_recent_events",
        "get_ranking",
        "assess_person",
        "hardware_alarm",
        "query_map",
    }

    # ==========================================================
    # Maximum number of tool actions per task
    # ==========================================================

    MAX_STEPS = 3

    # ==========================================================
    # Trigger types that should receive immediate handling
    # ==========================================================

    IMMEDIATE_TRIGGERS = {
        "HIGH_RISK",
        "HIGH_RISK_ESCALATION",
    }

    STATE_CHANGE_TRIGGERS = {
        "STATE_CHANGE",
    }

    HEARTBEAT_TRIGGERS = {
        "HEARTBEAT",
    }

    MANUAL_TRIGGERS = {
        "MANUAL",
    }

    def __init__(
        self,
        memory: Memory | None = None,
        cooldown_minutes: int = 30,
        max_steps: int = 3,
    ):
        if cooldown_minutes <= 0:
            raise ValueError(
                "cooldown_minutes must be greater than 0."
            )

        if max_steps <= 0:
            raise ValueError(
                "max_steps must be greater than 0."
            )

        self.memory = (
            memory
            if memory is not None
            else Memory()
        )

        self.cooldown_minutes = (
            cooldown_minutes
        )

        # Never allow a task to exceed the
        # PRD-defined maximum.
        self.max_steps = min(
            max_steps,
            self.MAX_STEPS,
        )

    # ==========================================================
    # Main planning interface
    # ==========================================================

    def plan(
        self,
        trigger: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Convert one trigger into a structured Agent task plan.

        Input:
            trigger:
                Output from EventTrigger.

            context:
                Optional additional information, for example:

                {
                    "memory_digest": "...",
                    "scene_analysis": {...},
                    "reasoning_result": {...}
                }

        Output:
            Structured task dictionary.
        """

        if not isinstance(
            trigger,
            dict,
        ):
            raise TypeError(
                "trigger must be a dictionary."
            )

        if context is None:
            context = {}

        if not isinstance(
            context,
            dict,
        ):
            raise TypeError(
                "context must be a dictionary."
            )

        trigger_type = str(
            trigger.get(
                "trigger_type",
                "UNKNOWN",
            )
        )

        person_id = trigger.get(
            "person_id"
        )

        state = trigger.get(
            "state"
        )

        priority = str(
            trigger.get(
                "priority",
                "NORMAL",
            )
        ).upper()

        risk_score = trigger.get(
            "risk_score"
        )

        # ------------------------------------------------------
        # Generate task ID (context.task_id 优先, 供同一次 edge 迭代双路径复用)
        # ------------------------------------------------------

        task_id = (
            context.get("task_id")
            or (context.get("external_context") or {}).get("task_id")
            or self._generate_task_id()
        )

        # ------------------------------------------------------
        # Build task goal
        # ------------------------------------------------------

        goal = self._build_goal(
            trigger=trigger,
        )

        # ------------------------------------------------------
        # Check whether this event should be handled
        # ------------------------------------------------------

        decision = self._check_trigger(
            trigger=trigger,
        )

        if not decision["allowed"]:

            return {
                "ok": True,
                "task_id": task_id,
                "status": "suppressed",
                "trigger_type": trigger_type,
                "person_id": person_id,
                "priority": priority,
                "risk_score": risk_score,
                "goal": goal,
                "reason": decision["reason"],
                "actions": [],
                "plan_trace": [],
                "actions_count": 0,
                "created": datetime.now().isoformat(),
            }

        # ------------------------------------------------------
        # Generate deterministic action plan
        # ------------------------------------------------------

        actions = self._build_actions(
            trigger=trigger,
            context=context,
        )

        # ------------------------------------------------------
        # Enforce whitelist
        # ------------------------------------------------------

        actions = self._filter_allowed_actions(
            actions
        )

        # ------------------------------------------------------
        # Enforce maximum number of steps
        # ------------------------------------------------------

        actions = actions[
            : self.max_steps
        ]

        # ------------------------------------------------------
        # Build plan trace
        # ------------------------------------------------------

        plan_trace = self._build_plan_trace(
            trigger=trigger,
            actions=actions,
            context=context,
        )

        task = {
            "ok": True,
            "task_id": task_id,
            "trigger_type": trigger_type,
            "trigger_event": trigger.get(
                "trigger_type"
            ),
            "person_id": person_id,
            "priority": priority,
            "risk_score": risk_score,
            "goal": goal,
            "status": "planned",
            "created": datetime.now().isoformat(),
            "actions": actions,
            "actions_count": len(
                actions
            ),
            "plan_trace": plan_trace,
            "context": context,
        }

        return task

    # ==========================================================
    # LLM-assisted planning (interface contract)
    # ==========================================================
    #
    # Contract (docs/interface.md, schemas/llm_output.json):
    #
    #     llm_output.task_proposals
    #         |
    #         v
    #     Planner (translate + safety rules)
    #         |
    #         v
    #     agent_task {task_id, trigger, actions[{tool, args}], status}
    #
    # Fallback rule: when task_proposals is missing/empty,
    # planner stays rule-based and produces no LLM task.
    # ==========================================================

    # Contract action name -> whitelisted action name

    PROPOSAL_ACTION_MAP = {
        "alert": "send_alert",
        "notify": "send_alert",
        "log": "save_event",
        "send_alert": "send_alert",
        "save_event": "save_event",
        "get_snapshot": "get_snapshot",
        "get_recent_events": "get_recent_events",
        # P0-收口: 允许 LLM 提案触发只读 rescue 分析(经 whitelist/≤3步约束)
        "get_ranking": "get_ranking",
        "assess_person": "assess_person",
    }

    # Whitelisted action -> executing tool

    ACTION_TOOL_MAP = {
        "get_snapshot": "vision_tool",
        "send_alert": "communication_tool",
        "save_event": "database_tool",
        "get_recent_events": "database_tool",
        "get_ranking": "rescue_tool",
        "assess_person": "rescue_tool",
    }

    def plan_from_llm_output(
        self,
        llm_output: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Translate one llm_output payload into a contract-conformant
        agent_task (schemas/agent_task.json).

        Safety rules still apply (PRD 4.4.1):
            - action whitelist;
            - maximum 3 tool steps;
            - HIGH must alert is enforced by the rule-based path,
              the LLM path only ADDS actions, never suppresses them.

        Returns:
            {
                "ok": true,
                "task_id": ...,
                "trigger": {"source": "llm", "reason": ..., "ts": ...},
                "actions": [{"tool": ..., "args": {...}}],
                "status": "pending",
                ...
            }

            When task_proposals is empty the returned task carries
            "status": "suppressed" and no actions, per the interface
            fallback rule (planner stays rule-based).
        """

        if not isinstance(llm_output, dict):

            return {
                "ok": False,
                "status": "failed",
                "error": (
                    "llm_output must be a dictionary."
                ),
                "actions": [],
            }

        # ------------------------------------------------------
        # Normalize + validate input
        # ------------------------------------------------------

        from src.agent.task_schema import (
            normalize_llm_output,
            validate_llm_output,
        )

        errors = validate_llm_output(llm_output)

        if errors:

            return {
                "ok": False,
                "status": "failed",
                "error": "; ".join(errors),
                "actions": [],
            }

        llm_data = normalize_llm_output(llm_output)

        event_summary = llm_data["event_summary"]
        proposals = llm_data["task_proposals"]

        task_id = (context or {}).get("task_id") or self._generate_task_id()

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        # ------------------------------------------------------
        # Fallback: no proposals -> rule-based path stays
        # authoritative, no LLM task is created.
        # ------------------------------------------------------

        if len(proposals) == 0:

            return {
                "ok": True,
                "task_id": task_id,
                "trigger": {
                    "source": "llm",
                    "reason": (
                        "No task proposals in llm_output; "
                        "rule-based triggering remains "
                        "authoritative."
                    ),
                    "ts": now,
                },
                "actions": [],
                "status": "suppressed",
                "event_summary": event_summary,
                "actions_count": 0,
                "created": now,
            }

        # ------------------------------------------------------
        # Translate proposals into contract actions
        # ------------------------------------------------------

        actions: list[dict[str, Any]] = []

        seen_alert_targets: set[Any] = set()

        for proposal in proposals:

            if not isinstance(proposal, dict):
                continue

            mapped = self._translate_proposal(
                proposal=proposal,
                event_summary=event_summary,
                seen_alert_targets=seen_alert_targets,
            )

            if mapped is not None:
                actions.append(mapped)

        # ------------------------------------------------------
        # Enforce whitelist + maximum steps
        # ------------------------------------------------------

        actions = [
            action
            for action in actions
            if action.get("args", {}).get(
                "action"
            ) in self.ALLOWED_ACTIONS
        ]

        actions = actions[: self.max_steps]

        # ------------------------------------------------------
        # Empty after filtering -> suppressed no-op
        # ------------------------------------------------------

        if len(actions) == 0:

            return {
                "ok": True,
                "task_id": task_id,
                "trigger": {
                    "source": "llm",
                    "reason": (
                        "All task proposals were rejected "
                        "by the action whitelist."
                    ),
                    "ts": now,
                },
                "actions": [],
                "status": "suppressed",
                "event_summary": event_summary,
                "actions_count": 0,
                "created": now,
            }

        # ------------------------------------------------------
        # Contract-conformant agent task
        # ------------------------------------------------------

        return {
            "ok": True,
            "task_id": task_id,
            "trigger": {
                "source": "llm",
                "reason": (
                    event_summary
                    or "LLM task proposals."
                ),
                "ts": now,
            },
            "actions": actions,
            "status": "pending",
            "event_summary": event_summary,
            "rescue_suggestions": llm_data[
                "rescue_suggestions"
            ],
            "actions_count": len(actions),
            "created": now,
            "context": context or {},
        }

    # ==========================================================
    # Proposal translation
    # ==========================================================

    def _translate_proposal(
        self,
        proposal: dict[str, Any],
        event_summary: str,
        seen_alert_targets: set[Any],
    ) -> dict[str, Any] | None:
        """
        Translate one llm_output task proposal into a contract
        action {tool, args}.

        Returns None when the proposal is not translatable.
        """

        raw_action = str(
            proposal.get("action", "")
        ).lower()

        action = self.PROPOSAL_ACTION_MAP.get(
            raw_action
        )

        if action is None:
            return None

        target = proposal.get("target")
        payload = proposal.get("payload")

        if not isinstance(payload, dict):
            payload = {}

        # ------------------------------------------------------
        # send_alert (alert / notify)
        # ------------------------------------------------------

        if action == "send_alert":

            # Deduplicate repeated alerts for the same person.
            if target in seen_alert_targets:
                return None

            seen_alert_targets.add(target)

            priority = str(
                payload.get(
                    "priority",
                    "HIGH",
                )
            ).upper()

            message = (
                payload.get("message")
                or event_summary
                or "High-risk personnel alert."
            )

            state = payload.get("state")

            severity = payload.get(
                "severity",
                priority,
            )

            title = (
                f"[{priority}] "
                f"Person {target} "
                f"{state or 'Alert'}"
            )

            return {
                "tool": "communication_tool",
                "args": {
                    "action": "send_alert",
                    "person_id": target,
                    "severity": severity,
                    "priority": priority,
                    "state": state,
                    "title": title,
                    "message": message,
                    "risk_score": payload.get("risk_score"),
                },
            }

        # ------------------------------------------------------
        # save_event (log)
        # ------------------------------------------------------

        if action == "save_event":

            return {
                "tool": "database_tool",
                "args": {
                    "action": "save_event",
                    "type": "llm_proposal",
                    "severity": str(
                        payload.get(
                            "severity",
                            "NORMAL",
                        )
                    ).upper(),
                    "person_id": target,
                    "payload": {
                        "event_summary": event_summary,
                        **payload,
                    },
                },
            }

        # ------------------------------------------------------
        # Read-only actions pass through with target.
        # ------------------------------------------------------

        return {
            "tool": self.ACTION_TOOL_MAP[action],
            "args": {
                "action": action,
                "person_id": target,
            },
        }

    # ==========================================================
    # Persist agent task (contract artifact)
    # ==========================================================

    def save_agent_task(
        self,
        task: dict[str, Any],
        output_path: str | Path = "data/agent_task.json",
    ) -> bool:
        """
        Write one agent task to disk as the contract artifact
        consumed by the executor / other modules.
        """

        try:

            path = Path(output_path)
            path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            path.write_text(
                json.dumps(
                    task,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )

            return True

        except Exception as exc:

            print(
                "[Planner] save_agent_task failed:",
                exc,
            )

            return False

    # ==========================================================
    # Trigger decision
    # ==========================================================

    def _check_trigger(
        self,
        trigger: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Determine whether a trigger should create a task.

        HIGH:
            Always allowed.

        HIGH_RISK_ESCALATION:
            Always allowed.

        STATE_CHANGE:
            Check Memory cooldown.

        HEARTBEAT:
            Allowed as a scene inspection task.

        MANUAL:
            Allowed.

        Unknown:
            Suppressed.
        """

        trigger_type = str(
            trigger.get(
                "trigger_type",
                "UNKNOWN",
            )
        )

        person_id = trigger.get(
            "person_id"
        )

        state = trigger.get(
            "state"
        )

        risk_score = trigger.get(
            "risk_score"
        )

        # ------------------------------------------------------
        # HIGH is nearly always allowed.
        #
        # Baseline follows the PRD: HIGH must alert and cannot be
        # blocked by LLM. However, repeated alerts for the same
        # person/state within the cooldown window (without a
        # meaningful risk increase) are suppressed, so the cooldown
        # recorded by Memory.record_alert is actually enforced.
        # ------------------------------------------------------

        if trigger_type in self.IMMEDIATE_TRIGGERS:

            # 无 memory 时保持旧语义(无条件放行), 由调用方保证注入
            if self.memory is None:
                return {
                    "allowed": True,
                    "reason": (
                        "HIGH-risk trigger requires "
                        "immediate handling (no memory)."
                    ),
                }

            # 基础 cooldown 窗口: 无上次记录(首次)或已超出窗口 -> 放行。
            # should_cooldown 的 key=(person_id, state), state/person 变化
            # 天然区分,不会被其他人的 cooldown 影响。
            if not self.memory.should_cooldown(
                person_id=person_id,
                state=state,
                minutes=self.cooldown_minutes,
            ):
                return {
                    "allowed": True,
                    "reason": (
                        "HIGH-risk trigger allowed "
                        "(first alert or cooldown expired)."
                    ),
                }

            # cooldown 内: 只有风险明显提升 (risk_delta >= 0.10) 才突破;
            # 微升/持平(同 person+state) 一律抑制, 避免连续帧重复报警。
            # 不依赖 should_break_cooldown(任意微升即放行), 显式用阈值判定。
            last_alert = (self.memory.last_alerts or {}).get(
                (person_id, str(state))
            )
            last_risk = (
                (last_alert or {}).get("risk_score")
                if last_alert else None
            )

            if (
                last_risk is not None
                and risk_score is not None
                and (
                    float(risk_score) - float(last_risk)
                ) >= 0.10
            ):
                return {
                    "allowed": True,
                    "reason": (
                        "RISK delta >= 0.10 inside "
                        "cooldown window, escalation allowed."
                    ),
                }

            return {
                "allowed": False,
                "reason": (
                    "Same person/state inside "
                    f"{self.cooldown_minutes}-min cooldown "
                    "with risk_delta < 0.10."
                ),
            }

        # ------------------------------------------------------
        # State change
        # ------------------------------------------------------

        if trigger_type in self.STATE_CHANGE_TRIGGERS:

            if person_id is None:
                return {
                    "allowed": False,
                    "reason": (
                        "STATE_CHANGE trigger has "
                        "no person_id."
                    ),
                }

            if state is None:
                return {
                    "allowed": False,
                    "reason": (
                        "STATE_CHANGE trigger has "
                        "no state."
                    ),
                }

            # --------------------------------------------------
            # Memory cooldown
            # --------------------------------------------------

            should_alert = self.memory.should_alert(
                person_id=person_id,
                state=state,
                current_risk=risk_score,
                minutes=self.cooldown_minutes,
            )

            if not should_alert:
                return {
                    "allowed": False,
                    "reason": (
                        "Same person/state is inside "
                        f"the {self.cooldown_minutes}-minute "
                        "cooldown window."
                    ),
                }

            return {
                "allowed": True,
                "reason": (
                    "State change confirmed and "
                    "not blocked by Memory cooldown."
                ),
            }

        # ------------------------------------------------------
        # Heartbeat
        # ------------------------------------------------------

        if trigger_type in self.HEARTBEAT_TRIGGERS:

            return {
                "allowed": True,
                "reason": (
                    "Periodic scene heartbeat "
                    "requires inspection."
                ),
            }

        # ------------------------------------------------------
        # Manual trigger
        # ------------------------------------------------------

        if trigger_type in self.MANUAL_TRIGGERS:

            return {
                "allowed": True,
                "reason": (
                    "Manual trigger accepted."
                ),
            }

        # ------------------------------------------------------
        # Unknown trigger
        # ------------------------------------------------------

        return {
            "allowed": False,
            "reason": (
                f"Unsupported trigger type: "
                f"{trigger_type}"
            ),
        }

    # ==========================================================
    # Action planning
    # ==========================================================

    def _build_actions(
        self,
        trigger: dict[str, Any],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Generate deterministic actions.

        The returned list contains dictionaries like:

        {
            "action": "get_snapshot",
            "tool": "vision_tool",
            "input": {...}
        }

        The executor will later use these definitions.
        """

        trigger_type = str(
            trigger.get(
                "trigger_type",
                "UNKNOWN",
            )
        )

        person_id = trigger.get(
            "person_id"
        )

        state = trigger.get(
            "state"
        )

        priority = str(
            trigger.get(
                "priority",
                "NORMAL",
            )
        ).upper()

        risk_score = trigger.get(
            "risk_score"
        )

        actions: list[
            dict[str, Any]
        ] = []

        # ======================================================
        # HIGH / HIGH_RISK_ESCALATION
        # ======================================================

        if trigger_type in self.IMMEDIATE_TRIGGERS:

            # --------------------------------------------------
            # Step 1:
            # Confirm latest visual evidence.
            # --------------------------------------------------

            if person_id is not None:

                actions.append(
                    {
                        "step": len(actions) + 1,
                        "action": "get_snapshot",
                        "tool": "vision_tool",
                        "input": {
                            "action": "get_snapshot",
                            "person_id": person_id,
                        },
                        "reason": (
                            "Confirm the latest "
                            "visual evidence."
                        ),
                    }
                )

            # --------------------------------------------------
            # Step 2:
            # Send alert.
            #
            # HIGH must not be blocked by LLM.
            # --------------------------------------------------

            actions.append(
                {
                    "step": len(actions) + 1,
                    "action": "send_alert",
                    "tool": "communication_tool",
                    "input": {
                        "action": "send_alert",
                        "person_id": person_id,
                        "priority": priority,
                        "state": state,
                        "risk_score": risk_score,
                        "image_path": self._extract_snapshot_path(trigger),
                        "title": (
                            "High-Risk Personnel Alert"
                        ),
                        "message": (
                            self._build_alert_message(
                                trigger
                            )
                        ),
                    },
                    "reason": (
                        "HIGH-risk event requires "
                        "immediate alert."
                    ),
                }
            )

            # --------------------------------------------------
            # Step 3:
            # Save event.
            # --------------------------------------------------

            actions.append(
                {
                    "step": len(actions) + 1,
                    "action": "save_event",
                    "tool": "database_tool",
                    "input": {
                        "action": "save_event",
                        "type": "high_risk_alert",
                        "severity": "HIGH",
                        "person_id": person_id,
                        "payload": {
                            "trigger_type": trigger_type,
                            "state": state,
                            "risk_score": risk_score,
                            "priority": priority,
                            "frame_idx": trigger.get(
                                "frame_idx"
                            ),
                        },
                    },
                    "reason": (
                        "Persist the high-risk "
                        "event for audit."
                    ),
                }
            )

            return actions

        # ======================================================
        # STATE CHANGE
        # ======================================================

        if trigger_type in self.STATE_CHANGE_TRIGGERS:

            # --------------------------------------------------
            # Step 1: visual confirmation
            # --------------------------------------------------

            if person_id is not None:

                actions.append(
                    {
                        "step": len(actions) + 1,
                        "action": "get_snapshot",
                        "tool": "vision_tool",
                        "input": {
                            "action": "get_snapshot",
                            "person_id": person_id,
                        },
                        "reason": (
                            "Confirm the person's "
                            "latest visual state."
                        ),
                    }
                )

            # --------------------------------------------------
            # Step 2: alert
            # --------------------------------------------------

            actions.append(
                {
                    "step": len(actions) + 1,
                    "action": "send_alert",
                    "tool": "communication_tool",
                    "input": {
                        "action": "send_alert",
                        "person_id": person_id,
                        "priority": priority,
                        "state": state,
                        "risk_score": risk_score,
                        "title": (
                            "Personnel State Change Alert"
                        ),
                        "message": (
                            self._build_alert_message(
                                trigger
                            )
                        ),
                    },
                    "reason": (
                        "Confirmed personnel state "
                        "change requires notification."
                    ),
                }
            )

            # --------------------------------------------------
            # Step 3: save event
            # --------------------------------------------------

            actions.append(
                {
                    "step": len(actions) + 1,
                    "action": "save_event",
                    "tool": "database_tool",
                    "input": {
                        "action": "save_event",
                        "type": "state_change",
                        "severity": priority,
                        "person_id": person_id,
                        "payload": {
                            "trigger_type": trigger_type,
                            "previous_state": trigger.get(
                                "previous_state"
                            ),
                            "state": state,
                            "risk_score": risk_score,
                            "frame_idx": trigger.get(
                                "frame_idx"
                            ),
                        },
                    },
                    "reason": (
                        "Persist the confirmed "
                        "state change."
                    ),
                }
            )

            return actions

        # ======================================================
        # HEARTBEAT
        # ======================================================

        if trigger_type in self.HEARTBEAT_TRIGGERS:

            # --------------------------------------------------
            # Step 1: get current scene
            # --------------------------------------------------

            actions.append(
                {
                    "step": 1,
                    "action": "get_recent_events",
                    "tool": "database_tool",
                    "input": {
                        "action": "get_recent_events",
                        "hours": 1,
                    },
                    "reason": (
                        "Review recent events during "
                        "the periodic heartbeat."
                    ),
                }
            )

            # --------------------------------------------------
            # Step 2: save heartbeat event
            # --------------------------------------------------

            actions.append(
                {
                    "step": 2,
                    "action": "save_event",
                    "tool": "database_tool",
                    "input": {
                        "action": "save_event",
                        "type": "heartbeat",
                        "severity": "NORMAL",
                        "person_id": None,
                        "payload": {
                            "frame_idx": trigger.get(
                                "frame_idx"
                            ),
                            "reason": trigger.get(
                                "reason"
                            ),
                        },
                    },
                    "reason": (
                        "Record periodic scene "
                        "inspection."
                    ),
                }
            )

            return actions

        # ======================================================
        # MANUAL
        # ======================================================

        if trigger_type in self.MANUAL_TRIGGERS:

            actions.append(
                {
                    "step": 1,
                    "action": "get_recent_events",
                    "tool": "database_tool",
                    "input": {
                        "action": "get_recent_events",
                        "hours": 1,
                    },
                    "reason": (
                        "Review recent events for "
                        "manual analysis."
                    ),
                }
            )

            actions.append(
                {
                    "step": 2,
                    "action": "save_event",
                    "tool": "database_tool",
                    "input": {
                        "action": "save_event",
                        "type": "manual_trigger",
                        "severity": "NORMAL",
                        "person_id": person_id,
                        "payload": {
                            "reason": trigger.get(
                                "reason"
                            )
                        },
                    },
                    "reason": (
                        "Record manual analysis "
                        "request."
                    ),
                }
            )

            return actions

        return []

    # ==========================================================
    # Action whitelist
    # ==========================================================

    def _filter_allowed_actions(
        self,
        actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Keep only actions in the whitelist.

        This is an important safety boundary.
        """

        filtered: list[
            dict[str, Any]
        ] = []

        for action in actions:

            action_name = action.get(
                "action"
            )

            if action_name not in self.ALLOWED_ACTIONS:
                continue

            filtered.append(
                action
            )

        # Re-number steps after filtering.
        for index, action in enumerate(
            filtered,
            start=1,
        ):
            action["step"] = index

        return filtered

    # ==========================================================
    # Plan trace
    # ==========================================================

    def _build_plan_trace(
        self,
        trigger: dict[str, Any],
        actions: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Build the initial Thought-Action-Observation trace.

        At planning time, Observation is still None.
        executor.py will fill the observation after execution.
        """

        trace: list[
            dict[str, Any]
        ] = []

        for action in actions:

            trace.append(
                {
                    "step": action.get(
                        "step"
                    ),
                    "thought": action.get(
                        "reason"
                    ),
                    "action": (
                        f"{action.get('tool')}."
                        f"{action.get('action')}"
                    ),
                    "input": action.get(
                        "input",
                        {},
                    ),
                    "observation": None,
                }
            )

        return trace

    # ==========================================================
    # Goal generation
    # ==========================================================

    def _build_goal(
        self,
        trigger: dict[str, Any],
    ) -> str:
        """
        Build a human-readable task goal.
        """

        trigger_type = str(
            trigger.get(
                "trigger_type",
                "UNKNOWN",
            )
        )

        person_id = trigger.get(
            "person_id"
        )

        if person_id is not None:

            return (
                f"Handle {trigger_type} "
                f"event for person_{person_id}."
            )

        return (
            f"Handle {trigger_type} event."
        )

    # ==========================================================
    # Alert message
    # ==========================================================

    def _extract_snapshot_path(
        self,
        trigger: dict[str, Any],
    ) -> Optional[str]:
        """从 trigger 中提取现场快照路径 (若有), 供图片报警带图。

        Edge 感知侧可把 snapshot_path 放进 person dict (经 frame_record
        原样透传至 trigger.person)。此处仅做最小字段透传, 找不到返回 None。
        """
        try:
            person = trigger.get("person") or {}
            return person.get("image_path") or person.get("snapshot_path") \
                or trigger.get("image_path")
        except Exception:  # noqa: BLE001
            return None

    def _build_alert_message(
        self,
        trigger: dict[str, Any],
    ) -> str:
        """
        Generate a compact alert message.

        This is only the Planner-level message.
        communication_tool remains responsible for
        actual delivery.
        """

        person_id = trigger.get(
            "person_id",
            "unknown",
        )

        state = trigger.get(
            "state",
            "unknown",
        )

        priority = trigger.get(
            "priority",
            "NORMAL",
        )

        risk_score = trigger.get(
            "risk_score"
        )

        previous_state = trigger.get(
            "previous_state"
        )

        if (
            previous_state is not None
        ):
            state_text = (
                f"{previous_state} -> {state}"
            )
        else:
            state_text = str(
                state
            )

        if isinstance(
            risk_score,
            (int, float),
        ):
            risk_text = f"{risk_score:.2f}"
        else:
            risk_text = "unknown"

        return (
            f"Person {person_id} | "
            f"State: {state_text} | "
            f"Risk: {risk_text} | "
            f"Priority: {priority}"
        )

    # ==========================================================
    # Task ID
    # ==========================================================

    @staticmethod
    def _generate_task_id() -> str:
        """
        Generate a short task ID.
        """

        timestamp = datetime.now().strftime(
            "%Y%m%d-%H%M%S"
        )

        short_uuid = uuid.uuid4().hex[
            :6
        ]

        return (
            f"T-{timestamp}-{short_uuid}"
        )


# ==============================================================
# Standalone test
# ==============================================================

if __name__ == "__main__":

    print(
        "Testing Planner..."
    )

    # ==========================================================
    # Create Memory + Planner
    # ==========================================================

    memory = Memory()

    planner = Planner(
        memory=memory,
        cooldown_minutes=30,
        max_steps=3,
    )

    # ==========================================================
    # Test 1: HIGH-risk trigger
    # ==========================================================

    high_trigger = {
        "trigger_type": "HIGH_RISK",
        "person_id": 1,
        "priority": "HIGH",
        "state": "Fall",
        "risk_score": 0.92,
        "frame_idx": 1204,
        "timestamp": "2026-09-20T11:00:00",
        "reason": (
            "HIGH priority detected."
        ),
    }

    high_plan = planner.plan(
        high_trigger
    )

    print(
        "\n1. HIGH-risk plan:"
    )

    print(
        json.dumps(
            high_plan,
            ensure_ascii=False,
            indent=2,
        )
    )

    # ==========================================================
    # Test 2: Check HIGH plan
    # ==========================================================

    assert high_plan["ok"] is True
    assert (
        high_plan["status"]
        == "planned"
    )

    assert (
        high_plan["actions_count"]
        == 3
    )

    assert (
        high_plan["actions"][0][
            "action"
        ]
        == "get_snapshot"
    )

    assert (
        high_plan["actions"][1][
            "action"
        ]
        == "send_alert"
    )

    assert (
        high_plan["actions"][2][
            "action"
        ]
        == "save_event"
    )

    # ==========================================================
    # Test 3: State change before cooldown
    # ==========================================================

    state_change_trigger = {
        "trigger_type": "STATE_CHANGE",
        "person_id": 2,
        "priority": "LOW",
        "state": "Fall",
        "previous_state": "Standing",
        "risk_score": 0.50,
        "frame_idx": 200,
        "timestamp": "2026-09-20T11:01:00",
        "reason": (
            "State changed from Standing "
            "to Fall and was confirmed "
            "for 3 consecutive frames."
        ),
    }

    state_plan = planner.plan(
        state_change_trigger
    )

    print(
        "\n2. STATE_CHANGE plan:"
    )

    print(
        json.dumps(
            state_plan,
            ensure_ascii=False,
            indent=2,
        )
    )

    assert (
        state_plan["status"]
        == "planned"
    )

    assert (
        state_plan["actions_count"]
        == 3
    )

    # ==========================================================
    # Test 4: Record the alert
    # ==========================================================

    memory.record_alert(
        person_id=2,
        state="Fall",
        risk_score=0.50,
        priority="LOW",
    )

    # ==========================================================
    # Test 5: Same state should be suppressed
    # ==========================================================

    repeated_state_trigger = {
        "trigger_type": "STATE_CHANGE",
        "person_id": 2,
        "priority": "LOW",
        "state": "Fall",
        "previous_state": "Standing",
        "risk_score": 0.50,
        "frame_idx": 300,
        "timestamp": "2026-09-20T11:02:00",
        "reason": (
            "Repeated state-change event."
        ),
    }

    suppressed_plan = planner.plan(
        repeated_state_trigger
    )

    print(
        "\n3. Repeated STATE_CHANGE:"
    )

    print(
        json.dumps(
            suppressed_plan,
            ensure_ascii=False,
            indent=2,
        )
    )

    assert (
        suppressed_plan["status"]
        == "suppressed"
    )

    assert (
        suppressed_plan["actions_count"]
        == 0
    )

    # ==========================================================
    # Test 6: Risk escalation should break cooldown
    # ==========================================================

    escalation_trigger = {
        "trigger_type": "HIGH_RISK_ESCALATION",
        "person_id": 2,
        "priority": "HIGH",
        "state": "Fall",
        "risk_score": 0.90,
        "frame_idx": 400,
        "timestamp": "2026-09-20T11:03:00",
        "reason": (
            "Risk score increased while "
            "priority remained HIGH."
        ),
    }

    escalation_plan = planner.plan(
        escalation_trigger
    )

    print(
        "\n4. Risk escalation:"
    )

    print(
        json.dumps(
            escalation_plan,
            ensure_ascii=False,
            indent=2,
        )
    )

    assert (
        escalation_plan["status"]
        == "planned"
    )

    # ==========================================================
    # Test 7: Heartbeat
    # ==========================================================

    heartbeat_trigger = {
        "trigger_type": "HEARTBEAT",
        "person_id": None,
        "priority": "NORMAL",
        "frame_idx": 500,
        "timestamp": "2026-09-20T11:05:00",
        "reason": (
            "5-minute scene heartbeat."
        ),
    }

    heartbeat_plan = planner.plan(
        heartbeat_trigger
    )

    print(
        "\n5. HEARTBEAT plan:"
    )

    print(
        json.dumps(
            heartbeat_plan,
            ensure_ascii=False,
            indent=2,
        )
    )

    assert (
        heartbeat_plan["status"]
        == "planned"
    )

    assert (
        heartbeat_plan["actions_count"]
        == 2
    )

    # ==========================================================
    # Test 8: Unknown trigger
    # ==========================================================

    unknown_trigger = {
        "trigger_type": "UNKNOWN_EVENT",
        "person_id": 99,
        "state": "Unknown",
        "priority": "NORMAL",
        "risk_score": 0.10,
    }

    unknown_plan = planner.plan(
        unknown_trigger
    )

    print(
        "\n6. Unknown trigger:"
    )

    print(
        json.dumps(
            unknown_plan,
            ensure_ascii=False,
            indent=2,
        )
    )

    assert (
        unknown_plan["status"]
        == "suppressed"
    )

    # ==========================================================
    # Final test
    # ==========================================================

    print(
        "\nPlanner test completed successfully."
    )