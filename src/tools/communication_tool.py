from __future__ import annotations

from pathlib import Path
import json
import sys
from datetime import datetime
from typing import Any


# ==========================================================
# Add project root to Python module search path
# ==========================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from src.tools.base import Tool


class CommunicationTool(Tool):
    """
    Communication tool for the Disaster Rescue DRRM Agent.

    MVP responsibilities:
        - Create structured rescue alerts.
        - Simulate sending alerts.
        - Save communication records locally.
        - Provide a unified interface for future
          WeCom / other communication platform integration.

    This version does NOT send real external messages yet.
    """

    def __init__(
        self,
        log_path: str = "data/communication_log.jsonl",
    ):
        self.log_path = Path(log_path)

        self.log_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

    # ==========================================================
    # Tool metadata
    # ==========================================================

    @property
    def name(self) -> str:
        return "communication_tool"

    @property
    def description(self) -> str:
        return (
            "Create and send structured disaster rescue alerts. "
            "The MVP uses local logging and can later be connected "
            "to WeCom or another communication service."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "Communication operation to execute."
                    ),
                    "enum": [
                        "send_alert",
                        "get_recent_alerts",
                    ],
                },
                "severity": {
                    "type": "string",
                    "description": (
                        "Alert severity such as HIGH, MEDIUM, LOW."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Alert title.",
                },
                "message": {
                    "type": "string",
                    "description": "Alert message.",
                },
                "person_id": {
                    "type": "integer",
                    "description": (
                        "Person associated with the alert."
                    ),
                },
                "event_id": {
                    "type": "string",
                    "description": (
                        "Related disaster event ID."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Related Agent task ID."
                    ),
                },
                "image_path": {
                    "type": "string",
                    "description": (
                        "Optional path of the scene image."
                    ),
                },
                "extra": {
                    "type": "object",
                    "description": (
                        "Additional communication information."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of recent alerts to return."
                    ),
                    "default": 10,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    # ==========================================================
    # Main execution
    # ==========================================================

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """
        Execute a communication operation.

        Supported actions:

            send_alert
                Create and record a rescue alert.

            get_recent_alerts
                Read recent locally recorded alerts.
        """

        action = kwargs.get("action")

        if not action:
            return {
                "ok": False,
                "error": "Missing required parameter: action.",
            }

        try:
            if action == "send_alert":
                return self._send_alert(kwargs)

            if action == "get_recent_alerts":
                return self._get_recent_alerts(kwargs)

            return {
                "ok": False,
                "error": f"Unsupported communication action: {action}",
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "action": action,
            }

    # ==========================================================
    # Send alert
    # ==========================================================

    def _send_alert(
        self,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Create and locally record one rescue alert.

        This is currently a simulated communication action.
        """

        severity = kwargs.get("severity", "HIGH")
        title = kwargs.get("title")
        message = kwargs.get("message")

        if not title:
            return {
                "ok": False,
                "error": "Missing required parameter: title.",
            }

        if not message:
            return {
                "ok": False,
                "error": "Missing required parameter: message.",
            }

        person_id = kwargs.get("person_id")
        event_id = kwargs.get("event_id")
        task_id = kwargs.get("task_id")
        image_path = kwargs.get("image_path")
        extra = kwargs.get("extra", {})

        if not isinstance(extra, dict):
            return {
                "ok": False,
                "error": "Parameter 'extra' must be a dictionary.",
            }

        timestamp = datetime.now().isoformat(
            timespec="seconds"
        )

        alert = {
            "timestamp": timestamp,
            "severity": severity,
            "title": title,
            "message": message,
            "person_id": person_id,
            "event_id": event_id,
            "task_id": task_id,
            "image_path": image_path,
            "extra": extra,
            "status": "SENT",
            "channel": "LOCAL_MOCK",
        }

        self._append_log(alert)

        return {
            "ok": True,
            "action": "send_alert",
            "status": "SENT",
            "channel": "LOCAL_MOCK",
            "alert": alert,
        }

    # ==========================================================
    # Get recent alerts
    # ==========================================================

    def _get_recent_alerts(
        self,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Read recently recorded communication alerts.
        """

        limit = kwargs.get("limit", 10)

        if not isinstance(limit, int) or limit <= 0:
            return {
                "ok": False,
                "error": (
                    "Parameter 'limit' must be "
                    "a positive integer."
                ),
            }

        if not self.log_path.exists():
            return {
                "ok": True,
                "action": "get_recent_alerts",
                "count": 0,
                "alerts": [],
            }

        alerts: list[dict[str, Any]] = []

        lines = self.log_path.read_text(
            encoding="utf-8"
        ).splitlines()

        # Read from the newest records.
        for line in reversed(lines):
            if not line.strip():
                continue

            try:
                alert = json.loads(line)
            except json.JSONDecodeError:
                continue

            alerts.append(alert)

            if len(alerts) >= limit:
                break

        return {
            "ok": True,
            "action": "get_recent_alerts",
            "count": len(alerts),
            "alerts": alerts,
        }

    # ==========================================================
    # Local log
    # ==========================================================

    def _append_log(
        self,
        alert: dict[str, Any],
    ) -> None:
        """
        Append one alert to the local JSONL log.
        """

        with self.log_path.open(
            "a",
            encoding="utf-8",
        ) as file:
            file.write(
                json.dumps(
                    alert,
                    ensure_ascii=False,
                )
                + "\n"
            )


# ==============================================================
# Standalone test
# ==============================================================

if __name__ == "__main__":
    print("Testing CommunicationTool...")

    communication_tool = CommunicationTool()

    # ----------------------------------------------------------
    # Tool information
    # ----------------------------------------------------------

    print("\nTool information:")
    print(communication_tool.info())

    # ----------------------------------------------------------
    # Test 1: send HIGH alert
    # ----------------------------------------------------------

    alert_result = communication_tool.run(
        action="send_alert",
        severity="HIGH",
        title="High-Risk Person Detected",
        message=(
            "A fallen civilian has been detected. "
            "Immediate rescue is recommended."
        ),
        person_id=1,
        event_id="E-TEST-001",
        task_id="T-TEST-001",
        image_path="data/test_scene.jpg",
        extra={
            "state": "FALLEN",
            "risk": 0.95,
            "source": "edge_perception",
        },
    )

    print("\n1. Send alert:")
    print(alert_result)

    # ----------------------------------------------------------
    # Test 2: get recent alerts
    # ----------------------------------------------------------

    recent_result = communication_tool.run(
        action="get_recent_alerts",
        limit=10,
    )

    print("\n2. Recent alerts:")
    print(recent_result)

    # ----------------------------------------------------------
    # Test 3: invalid action
    # ----------------------------------------------------------

    invalid_result = communication_tool.run(
        action="invalid_action",
    )

    print("\n3. Invalid action:")
    print(invalid_result)

    # ----------------------------------------------------------
    # Test 4: missing title
    # ----------------------------------------------------------

    missing_result = communication_tool.run(
        action="send_alert",
        severity="HIGH",
        message="Test message",
    )

    print("\n4. Missing title:")
    print(missing_result)

    print("\nCommunicationTool test completed successfully.")
    print(f"Communication log: {communication_tool.log_path}")