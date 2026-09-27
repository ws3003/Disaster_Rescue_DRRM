from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import json
import sys


# ==========================================================
# Add project root to Python module search path
# ==========================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class EventTrigger:
    """
    Event trigger engine for the Disaster Rescue DRRM Agent.

    Responsibilities:
        1. HIGH priority -> immediate trigger
        2. State change -> 3-frame confirmation
        3. Heartbeat -> scene-level inspection every N minutes
        4. Manual trigger -> immediate analysis

    Input:
        One JSONL frame record.

    Output:
        A list of trigger events.
    """

    # ----------------------------------------------------------
    # State transition rules
    # ----------------------------------------------------------

    STATE_CHANGE_TARGETS = {
        "Fall",
        "Lying",
        "Help-Waving",
    }

    STATE_CHANGE_SOURCES = {
        "Standing",
        "Sitting",
    }

    def __init__(
        self,
        heartbeat_minutes: int = 5,
        confirm_frames: int = 3,
        high_risk_must_alert: bool = True,
    ):
        if heartbeat_minutes <= 0:
            raise ValueError(
                "heartbeat_minutes must be greater than 0."
            )

        if confirm_frames <= 0:
            raise ValueError(
                "confirm_frames must be greater than 0."
            )

        self.heartbeat_minutes = heartbeat_minutes
        self.confirm_frames = confirm_frames
        self.high_risk_must_alert = high_risk_must_alert

        # ======================================================
        # HIGH-risk / previous frame information
        # ======================================================

        self.previous_persons: dict[
            int | str,
            dict[str, Any],
        ] = {}

        # ======================================================
        # State confirmation information
        #
        # stable_states:
        #     Last confirmed stable state.
        #
        # candidate_states:
        #     State currently being confirmed.
        #
        # candidate_counts:
        #     Number of consecutive frames for candidate state.
        # ======================================================

        self.stable_states: dict[
            int | str,
            str,
        ] = {}

        self.candidate_states: dict[
            int | str,
            str,
        ] = {}

        self.candidate_counts: dict[
            int | str,
            int,
        ] = {}

        # Last state-change event that was already reported.
        self.last_confirmed_state: dict[
            int | str,
            str,
        ] = {}

        # ======================================================
        # Heartbeat
        # ======================================================

        self.last_heartbeat_time: datetime | None = None

        # ======================================================
        # Frame counter
        # ======================================================

        self.frame_count = 0

    # ==========================================================
    # Main frame processing
    # ==========================================================

    def process_frame(
        self,
        frame_record: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Process one frame record.

        Returns:
            List of generated trigger events.
        """

        if not isinstance(
            frame_record,
            dict,
        ):
            raise TypeError(
                "frame_record must be a dictionary."
            )

        self.frame_count += 1

        frame_ts = self._extract_timestamp(
            frame_record
        )

        persons = frame_record.get(
            "persons",
            [],
        )

        if not isinstance(
            persons,
            list,
        ):
            raise ValueError(
                "frame_record['persons'] must be a list."
            )

        triggers: list[dict[str, Any]] = []

        # ======================================================
        # Person-level processing
        # ======================================================

        for person in persons:

            if not isinstance(
                person,
                dict,
            ):
                continue

            person_id = self._get_person_id(
                person
            )

            if person_id is None:
                continue

            # --------------------------------------------------
            # 1. HIGH-risk trigger
            # --------------------------------------------------

            high_event = self._check_high_risk(
                person=person,
                frame_record=frame_record,
                frame_ts=frame_ts,
            )

            if high_event is not None:
                triggers.append(
                    high_event
                )

            # --------------------------------------------------
            # 2. State-change trigger
            # --------------------------------------------------

            state_event = self._check_state_change(
                person=person,
                frame_record=frame_record,
                frame_ts=frame_ts,
            )

            if state_event is not None:
                triggers.append(
                    state_event
                )

            # --------------------------------------------------
            # 3. Save current information for HIGH-risk logic
            #
            # IMPORTANT:
            # This is deliberately NOT used as the state-change
            # "previous stable state".
            # --------------------------------------------------

            self.previous_persons[
                person_id
            ] = {
                "state": person.get("state"),
                "priority": person.get("priority"),
                "risk_score": self._get_risk_score(
                    person
                ),
            }

        # ======================================================
        # Scene-level heartbeat
        # ======================================================

        heartbeat_event = self._check_heartbeat(
            frame_record=frame_record,
            frame_ts=frame_ts,
        )

        if heartbeat_event is not None:
            triggers.append(
                heartbeat_event
            )

        return triggers

    # ==========================================================
    # HIGH-risk trigger
    # ==========================================================

    def _check_high_risk(
        self,
        person: dict[str, Any],
        frame_record: dict[str, Any],
        frame_ts: datetime | None,
    ) -> dict[str, Any] | None:
        """
        HIGH priority triggers immediately.

        Conditions:
            1. First HIGH detection
            2. LOW/MEDIUM -> HIGH
            3. Risk score increases while HIGH
        """

        if not self.high_risk_must_alert:
            return None

        priority = str(
            person.get("priority", "")
        ).upper()

        if priority != "HIGH":
            return None

        person_id = self._get_person_id(
            person
        )

        if person_id is None:
            return None

        previous = self.previous_persons.get(
            person_id
        )

        current_risk = self._get_risk_score(
            person
        )

        # ------------------------------------------------------
        # First HIGH detection
        # ------------------------------------------------------

        if previous is None:
            return self._build_trigger(
                trigger_type="HIGH_RISK",
                person=person,
                frame_record=frame_record,
                frame_ts=frame_ts,
                reason="HIGH priority detected.",
            )

        previous_priority = str(
            previous.get(
                "priority",
                "",
            )
        ).upper()

        previous_risk = previous.get(
            "risk_score"
        )

        # ------------------------------------------------------
        # LOW/MEDIUM -> HIGH
        # ------------------------------------------------------

        if previous_priority != "HIGH":
            return self._build_trigger(
                trigger_type="HIGH_RISK",
                person=person,
                frame_record=frame_record,
                frame_ts=frame_ts,
                reason="Priority changed to HIGH.",
            )

        # ------------------------------------------------------
        # HIGH -> higher risk
        # ------------------------------------------------------

        if (
            current_risk is not None
            and previous_risk is not None
            and current_risk > previous_risk
        ):
            return self._build_trigger(
                trigger_type="HIGH_RISK_ESCALATION",
                person=person,
                frame_record=frame_record,
                frame_ts=frame_ts,
                reason=(
                    "Risk score increased while "
                    "priority remained HIGH."
                ),
            )

        return None

    # ==========================================================
    # State change confirmation
    # ==========================================================

    def _check_state_change(
        self,
        person: dict[str, Any],
        frame_record: dict[str, Any],
        frame_ts: datetime | None,
    ) -> dict[str, Any] | None:
        """
        Confirm state changes using consecutive frames.

        Example:

            Standing
                ↓
            Fall #1
                ↓
            Fall #2
                ↓
            Fall #3
                ↓
            STATE_CHANGE

        The previous stable state remains Standing during
        the three-frame confirmation period.
        """

        person_id = self._get_person_id(
            person
        )

        if person_id is None:
            return None

        state = person.get(
            "state"
        )

        if state is None:
            return None

        state = str(state)

        # ------------------------------------------------------
        # First observation of this person
        # ------------------------------------------------------

        if person_id not in self.stable_states:
            self.stable_states[
                person_id
            ] = state

            return None

        stable_state = self.stable_states[
            person_id
        ]

        # ------------------------------------------------------
        # Same as stable state:
        #
        # There is no state transition.
        # Reset candidate state.
        # ------------------------------------------------------

        if state == stable_state:

            self.candidate_states.pop(
                person_id,
                None,
            )

            self.candidate_counts.pop(
                person_id,
                None,
            )

            return None

        # ------------------------------------------------------
        # New candidate state
        # ------------------------------------------------------

        current_candidate = (
            self.candidate_states.get(
                person_id
            )
        )

        if current_candidate != state:

            self.candidate_states[
                person_id
            ] = state

            self.candidate_counts[
                person_id
            ] = 1

        else:

            self.candidate_counts[
                person_id
            ] += 1

        candidate_count = (
            self.candidate_counts[
                person_id
            ]
        )

        # ------------------------------------------------------
        # Not enough consecutive frames
        # ------------------------------------------------------

        if candidate_count < self.confirm_frames:
            return None

        # ------------------------------------------------------
        # Check whether this is a valid transition
        # ------------------------------------------------------

        valid_fall_transition = (
            stable_state in self.STATE_CHANGE_SOURCES
            and state in {
                "Fall",
                "Lying",
            }
        )

        valid_help_transition = (
            state == "Help-Waving"
            and stable_state != "Help-Waving"
        )

        if not (
            valid_fall_transition
            or valid_help_transition
        ):
            # This candidate is confirmed but is not a
            # transition that needs an Agent trigger.
            self.stable_states[
                person_id
            ] = state

            self.candidate_states.pop(
                person_id,
                None,
            )

            self.candidate_counts.pop(
                person_id,
                None,
            )

            return None

        # ------------------------------------------------------
        # Prevent duplicate trigger
        # ------------------------------------------------------

        if (
            self.last_confirmed_state.get(
                person_id
            )
            == state
        ):
            return None

        self.last_confirmed_state[
            person_id
        ] = state

        # ------------------------------------------------------
        # State transition confirmed
        # ------------------------------------------------------

        trigger = self._build_trigger(
            trigger_type="STATE_CHANGE",
            person=person,
            frame_record=frame_record,
            frame_ts=frame_ts,
            reason=(
                f"State changed from "
                f"{stable_state} to {state} "
                f"and was confirmed for "
                f"{self.confirm_frames} consecutive frames."
            ),
            extra={
                "previous_state": stable_state,
                "confirmed_state": state,
                "confirm_frames": self.confirm_frames,
            },
        )

        # ------------------------------------------------------
        # New state becomes stable
        # ------------------------------------------------------

        self.stable_states[
            person_id
        ] = state

        self.candidate_states.pop(
            person_id,
            None,
        )

        self.candidate_counts.pop(
            person_id,
            None,
        )

        return trigger

    # ==========================================================
    # Heartbeat
    # ==========================================================

    def _check_heartbeat(
        self,
        frame_record: dict[str, Any],
        frame_ts: datetime | None,
    ) -> dict[str, Any] | None:
        """
        Trigger scene heartbeat every N minutes.

        The first frame only starts the timer.
        """

        if frame_ts is None:
            return None

        # ------------------------------------------------------
        # First frame starts heartbeat timer.
        # ------------------------------------------------------

        if self.last_heartbeat_time is None:
            self.last_heartbeat_time = frame_ts
            return None

        elapsed = (
            frame_ts
            - self.last_heartbeat_time
        )

        if elapsed < timedelta(
            minutes=self.heartbeat_minutes
        ):
            return None

        self.last_heartbeat_time = frame_ts

        return {
            "trigger_type": "HEARTBEAT",
            "person_id": None,
            "priority": "NORMAL",
            "frame_idx": frame_record.get(
                "frame_idx"
            ),
            "timestamp": frame_ts.isoformat(),
            "reason": (
                f"{self.heartbeat_minutes}-minute "
                "scene heartbeat."
            ),
            "frame_record": frame_record,
        }

    # ==========================================================
    # Manual trigger
    # ==========================================================

    def manual_trigger(
        self,
        reason: str = "Manual agent trigger.",
    ) -> dict[str, Any]:
        """
        Create a manual trigger.
        """

        return {
            "trigger_type": "MANUAL",
            "person_id": None,
            "priority": "NORMAL",
            "timestamp": datetime.now().isoformat(
                timespec="seconds"
            ),
            "reason": reason,
        }

    # ==========================================================
    # JSONL processing
    # ==========================================================

    def process_jsonl(
        self,
        jsonl_path: str | Path,
    ) -> list[dict[str, Any]]:
        """
        Process JSONL frame records.
        """

        path = Path(
            jsonl_path
        )

        if not path.exists():
            raise FileNotFoundError(
                f"JSONL file not found: {path}"
            )

        all_triggers: list[
            dict[str, Any]
        ] = []

        with path.open(
            "r",
            encoding="utf-8",
        ) as file:

            for line_number, line in enumerate(
                file,
                start=1,
            ):

                line = line.strip()

                if not line:
                    continue

                try:
                    frame_record = json.loads(
                        line
                    )

                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON at line "
                        f"{line_number}: {exc}"
                    ) from exc

                if not isinstance(
                    frame_record,
                    dict,
                ):
                    raise ValueError(
                        f"JSONL line {line_number} "
                        "must contain an object."
                    )

                triggers = self.process_frame(
                    frame_record
                )

                all_triggers.extend(
                    triggers
                )

        return all_triggers

    # ==========================================================
    # Helpers
    # ==========================================================

    @staticmethod
    def _get_person_id(
        person: dict[str, Any],
    ) -> int | str | None:

        person_id = person.get(
            "person_id"
        )

        if person_id is not None:
            return person_id

        return person.get(
            "pid"
        )

    @staticmethod
    def _get_risk_score(
        person: dict[str, Any],
    ) -> float | None:

        value = person.get(
            "risk_score"
        )

        if value is None:
            value = person.get(
                "risk_score_final"
            )

        if value is None:
            return None

        try:
            return float(value)

        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _extract_timestamp(
        frame_record: dict[str, Any],
    ) -> datetime | None:

        value = frame_record.get(
            "ts"
        )

        if value is None:
            value = frame_record.get(
                "timestamp"
            )

        if value is None:
            return None

        if isinstance(
            value,
            datetime,
        ):
            return value

        if not isinstance(
            value,
            str,
        ):
            return None

        try:
            return datetime.fromisoformat(
                value.replace(
                    "Z",
                    "+00:00",
                )
            )

        except ValueError:
            return None

    def _build_trigger(
        self,
        trigger_type: str,
        person: dict[str, Any],
        frame_record: dict[str, Any],
        frame_ts: datetime | None,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:

        event = {
            "trigger_type": trigger_type,
            "person_id": self._get_person_id(
                person
            ),
            "priority": person.get(
                "priority"
            ),
            "state": person.get(
                "state"
            ),
            "risk_score": self._get_risk_score(
                person
            ),
            "frame_idx": frame_record.get(
                "frame_idx"
            ),
            "timestamp": (
                frame_ts.isoformat()
                if frame_ts is not None
                else None
            ),
            "reason": reason,
            "person": person,
            "frame_record": frame_record,
        }

        if extra:
            event.update(
                extra
            )

        return event


# ==============================================================
# Standalone test
# ==============================================================

if __name__ == "__main__":

    print("Testing EventTrigger...")

    # ==========================================================
    # 1. HIGH trigger
    # ==========================================================

    high_trigger = EventTrigger(
        heartbeat_minutes=5,
        confirm_frames=3,
    )

    high_frame = {
        "frame_idx": 1,
        "ts": "2026-09-20T10:00:00",
        "persons": [
            {
                "pid": 1,
                "class_name": "civilian",
                "state": "Fall",
                "state_conf": 0.93,
                "risk_score": 0.95,
                "priority": "HIGH",
            }
        ],
    }

    print("\n1. HIGH trigger:")
    print(
        high_trigger.process_frame(
            high_frame
        )
    )

    # ==========================================================
    # 2. Repeated HIGH
    # ==========================================================

    repeated_high_frame = {
        "frame_idx": 2,
        "ts": "2026-09-20T10:00:01",
        "persons": [
            {
                "pid": 1,
                "class_name": "civilian",
                "state": "Fall",
                "state_conf": 0.94,
                "risk_score": 0.95,
                "priority": "HIGH",
            }
        ],
    }

    print("\n2. Repeated HIGH:")
    print(
        high_trigger.process_frame(
            repeated_high_frame
        )
    )

    # ==========================================================
    # 3. State change
    # ==========================================================

    state_trigger = EventTrigger(
        heartbeat_minutes=5,
        confirm_frames=3,
    )

    standing_frame = {
        "frame_idx": 10,
        "ts": "2026-09-20T10:10:00",
        "persons": [
            {
                "pid": 2,
                "class_name": "civilian",
                "state": "Standing",
                "risk_score": 0.10,
                "priority": "LOW",
            }
        ],
    }

    print("\n3. Initial Standing frame:")
    print(
        state_trigger.process_frame(
            standing_frame
        )
    )

    # ----------------------------------------------------------
    # Fall frame 1
    # ----------------------------------------------------------

    fall_frame_1 = {
        "frame_idx": 11,
        "ts": "2026-09-20T10:10:01",
        "persons": [
            {
                "pid": 2,
                "class_name": "civilian",
                "state": "Fall",
                "risk_score": 0.50,
                "priority": "LOW",
            }
        ],
    }

    print("\n3.1. Fall confirmation frame 1:")
    print(
        state_trigger.process_frame(
            fall_frame_1
        )
    )

    # ----------------------------------------------------------
    # Fall frame 2
    # ----------------------------------------------------------

    fall_frame_2 = {
        "frame_idx": 12,
        "ts": "2026-09-20T10:10:02",
        "persons": [
            {
                "pid": 2,
                "class_name": "civilian",
                "state": "Fall",
                "risk_score": 0.50,
                "priority": "LOW",
            }
        ],
    }

    print("\n3.2. Fall confirmation frame 2:")
    print(
        state_trigger.process_frame(
            fall_frame_2
        )
    )

    # ----------------------------------------------------------
    # Fall frame 3
    # ----------------------------------------------------------

    fall_frame_3 = {
        "frame_idx": 13,
        "ts": "2026-09-20T10:10:03",
        "persons": [
            {
                "pid": 2,
                "class_name": "civilian",
                "state": "Fall",
                "risk_score": 0.50,
                "priority": "LOW",
            }
        ],
    }

    print("\n3.3. Fall confirmation frame 3:")
    print(
        state_trigger.process_frame(
            fall_frame_3
        )
    )

    # ==========================================================
    # 4. Heartbeat
    # ==========================================================

    heartbeat_trigger = EventTrigger(
        heartbeat_minutes=5,
        confirm_frames=3,
    )

    heartbeat_frame_1 = {
        "frame_idx": 100,
        "ts": "2026-09-20T11:00:00",
        "persons": [],
    }

    heartbeat_frame_2 = {
        "frame_idx": 200,
        "ts": "2026-09-20T11:04:00",
        "persons": [],
    }

    heartbeat_frame_3 = {
        "frame_idx": 300,
        "ts": "2026-09-20T11:05:00",
        "persons": [],
    }

    print("\n4. Heartbeat frame 1:")
    print(
        heartbeat_trigger.process_frame(
            heartbeat_frame_1
        )
    )

    print("\n4. Heartbeat frame 2:")
    print(
        heartbeat_trigger.process_frame(
            heartbeat_frame_2
        )
    )

    print("\n4. Heartbeat frame 3:")
    print(
        heartbeat_trigger.process_frame(
            heartbeat_frame_3
        )
    )

    # ==========================================================
    # 5. Manual trigger
    # ==========================================================

    print("\n5. Manual trigger:")

    print(
        heartbeat_trigger.manual_trigger(
            reason="Demo manual analysis trigger."
        )
    )

    print(
        "\nEventTrigger test completed successfully."
    )