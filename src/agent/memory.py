"""
Agent memory — 历史记忆(P0)
============================
职责: remember/digest/cooldown 记忆管理, 经 database/db.py 落 SQLite。
本文件为入口骨架, 待实现业务逻辑。
"""


# 记忆管理(待实现)

from __future__ import annotations

from collections import defaultdict, deque
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


class Memory:
    """
    Short-term memory for the Disaster Rescue DRRM Agent.

    Responsibilities:
        1. Remember recent frame records
        2. Keep recent state/risk history for each person
        3. Generate a compact scene digest
        4. Prevent repeated alerts within a cooldown window
        5. Allow risk escalation to break the cooldown

    MVP design:
        - In-memory storage
        - No extra database table required
        - Can later be extended to person_states / risk_history
    """

    def __init__(
        self,
        max_frames: int = 300,
        max_person_history: int = 100,
    ):
        if max_frames <= 0:
            raise ValueError(
                "max_frames must be greater than 0."
            )

        if max_person_history <= 0:
            raise ValueError(
                "max_person_history must be greater than 0."
            )

        # ------------------------------------------------------
        # Recent complete frame records
        # ------------------------------------------------------

        self.frames: deque[
            dict[str, Any]
        ] = deque(
            maxlen=max_frames
        )

        # ------------------------------------------------------
        # Per-person state/risk history
        #
        # {
        #     person_id: [
        #         {
        #             "ts": ...,
        #             "state": ...,
        #             "risk_score": ...,
        #             "priority": ...
        #         }
        #     ]
        # }
        # ------------------------------------------------------

        self.person_history: dict[
            int | str,
            deque[dict[str, Any]],
        ] = defaultdict(
            lambda: deque(
                maxlen=max_person_history
            )
        )

        # ------------------------------------------------------
        # Last alert information
        #
        # Used by should_cooldown().
        # ------------------------------------------------------

        self.last_alerts: dict[
            tuple[int | str, str],
            dict[str, Any],
        ] = {}

    # ==========================================================
    # 1. Remember frame
    # ==========================================================

    def remember_frame(
        self,
        frame_record: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Store one frame record in short-term memory.

        Expected input:

        {
            "frame_idx": 100,
            "ts": "2026-09-20T10:00:00",
            "persons": [
                {
                    "pid": 1,
                    "state": "Standing",
                    "risk_score": 0.10,
                    "priority": "LOW"
                }
            ]
        }

        Returns:
            A small status dictionary.
        """

        if not isinstance(
            frame_record,
            dict,
        ):
            raise TypeError(
                "frame_record must be a dictionary."
            )

        timestamp = self._extract_timestamp(
            frame_record
        )

        stored_frame = dict(
            frame_record
        )

        # Normalize timestamp.
        if timestamp is not None:
            stored_frame["ts"] = (
                timestamp.isoformat()
            )

        self.frames.append(
            stored_frame
        )

        persons = frame_record.get(
            "persons",
            [],
        )

        if not isinstance(
            persons,
            list,
        ):
            persons = []

        remembered_persons = 0

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

            history_item = {
                "ts": (
                    timestamp.isoformat()
                    if timestamp is not None
                    else None
                ),
                "frame_idx": frame_record.get(
                    "frame_idx"
                ),
                "state": person.get(
                    "state"
                ),
                "risk_score": self._get_risk_score(
                    person
                ),
                "priority": person.get(
                    "priority"
                ),
                "state_conf": person.get(
                    "state_conf"
                ),
            }

            self.person_history[
                person_id
            ].append(
                history_item
            )

            remembered_persons += 1

        return {
            "ok": True,
            "action": "remember_frame",
            "frame_idx": frame_record.get(
                "frame_idx"
            ),
            "persons_remembered": (
                remembered_persons
            ),
            "total_frames": len(
                self.frames
            ),
        }

    # ==========================================================
    # 2. Scene digest
    # ==========================================================

    def digest(
        self,
        window_minutes: int = 30,
    ) -> str:
        """
        Generate a compact textual summary of recent scene memory.

        The digest is intended for:
            - Agent
            - LLM context
            - debugging
            - audit / replay

        It does NOT call any LLM.
        """

        if window_minutes <= 0:
            raise ValueError(
                "window_minutes must be greater than 0."
            )

        now = datetime.now()

        # ------------------------------------------------------
        # Find recent frames
        # ------------------------------------------------------

        recent_frames: list[
            dict[str, Any]
        ] = []

        for frame in self.frames:

            ts = self._extract_timestamp(
                frame
            )

            if ts is None:
                continue

            # Remove timezone information only for comparison
            # with datetime.now().
            if ts.tzinfo is not None:
                ts = ts.replace(
                    tzinfo=None
                )

            if (
                now - ts
                <= timedelta(
                    minutes=window_minutes
                )
            ):
                recent_frames.append(
                    frame
                )

        # ------------------------------------------------------
        # If timestamps are synthetic / historical, use the
        # latest stored frames rather than returning nothing.
        # ------------------------------------------------------

        if not recent_frames and self.frames:
            recent_frames = list(
                self.frames
            )[-20:]

        # ------------------------------------------------------
        # Build person summary
        # ------------------------------------------------------

        person_summaries: list[str] = []

        for person_id, history in sorted(
            self.person_history.items(),
            key=lambda item: str(item[0]),
        ):

            if not history:
                continue

            latest = history[-1]

            state = latest.get(
                "state",
                "UNKNOWN",
            )

            risk = latest.get(
                "risk_score"
            )

            priority = latest.get(
                "priority",
                "UNKNOWN",
            )

            # --------------------------------------------------
            # State changes in the recent history
            # --------------------------------------------------

            state_changes: list[str] = []

            previous_state = None

            for item in history:

                item_ts = item.get(
                    "ts"
                )

                item_state = item.get(
                    "state"
                )

                if (
                    previous_state is not None
                    and item_state != previous_state
                ):
                    state_changes.append(
                        f"{previous_state}->{item_state}"
                    )

                previous_state = item_state

            change_text = (
                ", ".join(
                    state_changes[-5:]
                )
                if state_changes
                else "none"
            )

            risk_text = (
                f"{risk:.2f}"
                if isinstance(
                    risk,
                    (int, float),
                )
                else "unknown"
            )

            person_summaries.append(
                f"Person {person_id}: "
                f"state={state}, "
                f"risk={risk_text}, "
                f"priority={priority}, "
                f"changes={change_text}"
            )

        # ------------------------------------------------------
        # Frame statistics
        # ------------------------------------------------------

        frame_count = len(
            recent_frames
        )

        latest_frame_idx = None

        if recent_frames:
            latest_frame_idx = (
                recent_frames[-1].get(
                    "frame_idx"
                )
            )

        # ------------------------------------------------------
        # Final digest
        # ------------------------------------------------------

        lines = [
            "DRRM Scene Memory Digest",
            f"Window: last {window_minutes} minutes",
            f"Recent frames: {frame_count}",
            f"Latest frame: {latest_frame_idx}",
        ]

        if person_summaries:
            lines.append(
                "Persons:"
            )

            lines.extend(
                f"- {item}"
                for item in person_summaries
            )

        else:
            lines.append(
                "Persons: none"
            )

        return "\n".join(
            lines
        )

    # ==========================================================
    # 3. Cooldown check
    # ==========================================================

    def should_cooldown(
        self,
        person_id: int | str,
        state: str,
        minutes: int = 30,
    ) -> bool:
        """
        Check whether an alert for the same person/state
        is still inside the cooldown window.

        Returns:
            True  -> alert should be suppressed
            False -> alert can be sent

        IMPORTANT:
            This function only checks the cooldown time.

            Risk escalation is handled by
            should_break_cooldown().
        """

        if minutes <= 0:
            raise ValueError(
                "minutes must be greater than 0."
            )

        key = (
            person_id,
            str(state),
        )

        last_alert = self.last_alerts.get(
            key
        )

        if last_alert is None:
            return False

        last_time = last_alert.get(
            "ts"
        )

        if not isinstance(
            last_time,
            datetime,
        ):
            return False

        elapsed = (
            datetime.now()
            - last_time
        )

        return elapsed < timedelta(
            minutes=minutes
        )

    # ==========================================================
    # 4. Record an alert
    # ==========================================================

    def record_alert(
        self,
        person_id: int | str,
        state: str,
        risk_score: float | None = None,
        priority: str | None = None,
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Record that an alert has actually been sent.

        This is separated from should_cooldown() so that:

            check -> send -> record

        is controlled by the Agent / executor.
        """

        if timestamp is None:
            timestamp = datetime.now()

        key = (
            person_id,
            str(state),
        )

        self.last_alerts[
            key
        ] = {
            "ts": timestamp,
            "risk_score": risk_score,
            "priority": priority,
        }

        return {
            "ok": True,
            "action": "record_alert",
            "person_id": person_id,
            "state": state,
            "risk_score": risk_score,
            "priority": priority,
            "timestamp": timestamp.isoformat(),
        }

    # ==========================================================
    # 5. Risk escalation breaks cooldown
    # ==========================================================

    def should_break_cooldown(
        self,
        person_id: int | str,
        state: str,
        current_risk: float | None,
    ) -> bool:
        """
        Determine whether a risk escalation should break
        the existing cooldown.

        Example:

            Previous alert:
                risk = 0.60

            Current event:
                risk = 0.85

            Result:
                True

        If risk does not increase:
                False
        """

        if current_risk is None:
            return False

        key = (
            person_id,
            str(state),
        )

        last_alert = self.last_alerts.get(
            key
        )

        if last_alert is None:
            return False

        previous_risk = last_alert.get(
            "risk_score"
        )

        if previous_risk is None:
            return False

        try:
            return float(
                current_risk
            ) > float(
                previous_risk
            )

        except (
            TypeError,
            ValueError,
        ):
            return False

    # ==========================================================
    # 6. Combined alert decision
    # ==========================================================

    def should_alert(
        self,
        person_id: int | str,
        state: str,
        current_risk: float | None = None,
        minutes: int = 30,
    ) -> bool:
        """
        Final alert decision.

        Logic:

            No previous alert
                    ↓
                  SEND

            Previous alert
                    ↓
            Is cooldown active?
               /          \
             No            Yes
             ↓              ↓
            SEND       Risk increased?
                         /       \
                       Yes        No
                       ↓           ↓
                     SEND       BLOCK
        """

        cooldown = self.should_cooldown(
            person_id=person_id,
            state=state,
            minutes=minutes,
        )

        if not cooldown:
            return True

        # ------------------------------------------------------
        # Cooldown is active.
        # Check risk escalation.
        # ------------------------------------------------------

        if self.should_break_cooldown(
            person_id=person_id,
            state=state,
            current_risk=current_risk,
        ):
            return True

        return False

    # ==========================================================
    # 7. Get person history
    # ==========================================================

    def get_person_history(
        self,
        person_id: int | str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Return recent history for one person.
        """

        if limit <= 0:
            return []

        history = self.person_history.get(
            person_id
        )

        if history is None:
            return []

        return list(
            history
        )[-limit:]

    # ==========================================================
    # 8. Clear memory
    # ==========================================================

    def clear(self) -> None:
        """
        Clear all in-memory records.
        """

        self.frames.clear()
        self.person_history.clear()
        self.last_alerts.clear()

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
        record: dict[str, Any],
    ) -> datetime | None:

        value = record.get(
            "ts"
        )

        if value is None:
            value = record.get(
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


# ==============================================================
# Standalone test
# ==============================================================

if __name__ == "__main__":

    print(
        "Testing Memory..."
    )

    memory = Memory()

    # ==========================================================
    # Test 1: remember_frame
    # ==========================================================

    frame_1 = {
        "frame_idx": 1,
        "ts": "2026-09-20T10:00:00",
        "persons": [
            {
                "pid": 1,
                "class_name": "civilian",
                "state": "Standing",
                "state_conf": 0.95,
                "risk_score": 0.10,
                "priority": "LOW",
            }
        ],
    }

    result_1 = memory.remember_frame(
        frame_1
    )

    print("\n1. Remember frame:")
    print(result_1)

    # ==========================================================
    # Test 2: another frame
    # ==========================================================

    frame_2 = {
        "frame_idx": 2,
        "ts": "2026-09-20T10:01:00",
        "persons": [
            {
                "pid": 1,
                "class_name": "civilian",
                "state": "Sitting",
                "state_conf": 0.91,
                "risk_score": 0.25,
                "priority": "LOW",
            },
            {
                "pid": 2,
                "class_name": "rescuer",
                "state": "Standing",
                "state_conf": 0.97,
                "risk_score": 0.05,
                "priority": "LOW",
            },
        ],
    }

    result_2 = memory.remember_frame(
        frame_2
    )

    print("\n2. Remember second frame:")
    print(result_2)

    # ==========================================================
    # Test 3: Fall frame
    # ==========================================================

    frame_3 = {
        "frame_idx": 3,
        "ts": "2026-09-20T10:02:00",
        "persons": [
            {
                "pid": 1,
                "class_name": "civilian",
                "state": "Fall",
                "state_conf": 0.94,
                "risk_score": 0.90,
                "priority": "HIGH",
            }
        ],
    }

    result_3 = memory.remember_frame(
        frame_3
    )

    print("\n3. Remember Fall frame:")
    print(result_3)

    # ==========================================================
    # Test 4: digest
    # ==========================================================

    digest_result = memory.digest(
        window_minutes=30
    )

    print("\n4. Scene digest:")
    print(
        digest_result
    )

    # ==========================================================
    # Test 5: cooldown before alert
    # ==========================================================

    cooldown_before = memory.should_cooldown(
        person_id=1,
        state="Fall",
        minutes=30,
    )

    print("\n5. Cooldown before first alert:")
    print(
        cooldown_before
    )

    # ==========================================================
    # Test 6: record first alert
    # ==========================================================

    record_result = memory.record_alert(
        person_id=1,
        state="Fall",
        risk_score=0.90,
        priority="HIGH",
    )

    print("\n6. Record first alert:")
    print(
        record_result
    )

    # ==========================================================
    # Test 7: cooldown after alert
    # ==========================================================

    cooldown_after = memory.should_cooldown(
        person_id=1,
        state="Fall",
        minutes=30,
    )

    print("\n7. Cooldown after alert:")
    print(
        cooldown_after
    )

    # ==========================================================
    # Test 8: same risk should be blocked
    # ==========================================================

    should_alert_same = memory.should_alert(
        person_id=1,
        state="Fall",
        current_risk=0.90,
        minutes=30,
    )

    print(
        "\n8. Same risk during cooldown:"
    )

    print(
        should_alert_same
    )

    # ==========================================================
    # Test 9: risk escalation breaks cooldown
    # ==========================================================

    should_alert_escalation = memory.should_alert(
        person_id=1,
        state="Fall",
        current_risk=0.98,
        minutes=30,
    )

    print(
        "\n9. Risk escalation during cooldown:"
    )

    print(
        should_alert_escalation
    )

    # ==========================================================
    # Test 10: person history
    # ==========================================================

    history = memory.get_person_history(
        person_id=1
    )

    print(
        "\n10. Person 1 history:"
    )

    print(
        json.dumps(
            history,
            ensure_ascii=False,
            indent=2,
        )
    )

    # ==========================================================
    # Final assertions
    # ==========================================================

    assert result_1["ok"] is True
    assert result_2["ok"] is True
    assert result_3["ok"] is True

    assert len(
        memory.frames
    ) == 3

    assert (
        cooldown_before
        is False
    )

    assert (
        cooldown_after
        is True
    )

    assert (
        should_alert_same
        is False
    )

    assert (
        should_alert_escalation
        is True
    )

    assert len(
        history
    ) == 3

    print(
        "\nMemory test completed successfully."
    )