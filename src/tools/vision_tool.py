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


class VisionTool(Tool):
    """
    Vision snapshot tool for the Disaster Rescue DRRM Agent.

    Responsibilities:
        - Receive the latest perception result.
        - Store the latest visual state snapshot.
        - Return the latest visual state to the Agent.
        - Provide a simple interface for future YOLO26n +
          MediaPipe integration.

    This tool does NOT perform object detection itself.

    The actual perception pipeline can later provide data such as:

        {
            "persons": [
                {
                    "person_id": 1,
                    "state": "FALLEN",
                    "risk": 0.95,
                    "bbox": [100, 120, 300, 420],
                    "confidence": 0.91
                }
            ]
        }
    """

    def __init__(self, snapshot_path: str = "data/latest_vision.json"):
        self.snapshot_path = Path(snapshot_path)

        # Make sure the parent directory exists.
        self.snapshot_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

    # ==========================================================
    # Tool metadata
    # ==========================================================

    @property
    def name(self) -> str:
        return "vision_tool"

    @property
    def description(self) -> str:
        return (
            "Read and update the latest disaster scene visual "
            "state snapshot for Agent decision-making."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Vision operation to execute.",
                    "enum": [
                        "update_snapshot",
                        "get_snapshot",
                    ],
                },
                "snapshot": {
                    "type": "object",
                    "description": (
                        "Latest visual perception result. "
                        "Usually produced by YOLO26n and MediaPipe."
                    ),
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
        Execute a vision operation.

        Supported actions:

            update_snapshot
                Save the latest visual perception result.

            get_snapshot
                Read the latest saved visual perception result.
        """

        action = kwargs.get("action")

        if not action:
            return {
                "ok": False,
                "error": "Missing required parameter: action.",
            }

        try:
            if action == "update_snapshot":
                return self._update_snapshot(kwargs)

            if action == "get_snapshot":
                return self._get_snapshot()

            return {
                "ok": False,
                "error": f"Unsupported vision action: {action}",
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "action": action,
            }

    # ==========================================================
    # Update snapshot
    # ==========================================================

    def _update_snapshot(
        self,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Save the latest visual perception result.
        """

        snapshot = kwargs.get("snapshot")

        if snapshot is None:
            return {
                "ok": False,
                "error": "Missing required parameter: snapshot.",
            }

        if not isinstance(snapshot, dict):
            return {
                "ok": False,
                "error": "Parameter 'snapshot' must be a dictionary.",
            }

        # Add timestamp if the perception pipeline did not provide one.
        if "timestamp" not in snapshot:
            snapshot["timestamp"] = datetime.now().isoformat(
                timespec="seconds"
            )

        # Add a default source if it was not specified.
        if "source" not in snapshot:
            snapshot["source"] = "edge_perception"

        # Make sure persons is always a list.
        if "persons" not in snapshot:
            snapshot["persons"] = []

        if not isinstance(snapshot["persons"], list):
            return {
                "ok": False,
                "error": "Parameter 'snapshot.persons' must be a list.",
            }

        # Save snapshot as JSON.
        self.snapshot_path.write_text(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        return {
            "ok": True,
            "action": "update_snapshot",
            "snapshot": snapshot,
        }

    # ==========================================================
    # Get snapshot
    # ==========================================================

    def _get_snapshot(self) -> dict[str, Any]:
        """
        Read the latest visual perception result.
        """

        if not self.snapshot_path.exists():
            return {
                "ok": True,
                "action": "get_snapshot",
                "snapshot": None,
                "message": "No vision snapshot available.",
            }

        content = self.snapshot_path.read_text(
            encoding="utf-8"
        )

        if not content.strip():
            return {
                "ok": True,
                "action": "get_snapshot",
                "snapshot": None,
                "message": "Vision snapshot is empty.",
            }

        snapshot = json.loads(content)

        return {
            "ok": True,
            "action": "get_snapshot",
            "snapshot": snapshot,
        }


# ==============================================================
# Standalone test
# ==============================================================

if __name__ == "__main__":
    print("Testing VisionTool...")

    vision_tool = VisionTool()

    # ----------------------------------------------------------
    # Tool information
    # ----------------------------------------------------------

    print("\nTool information:")
    print(vision_tool.info())

    # ----------------------------------------------------------
    # Test 1: update snapshot
    # ----------------------------------------------------------

    test_snapshot = {
        "scene_id": "TEST_SCENE_001",
        "source": "edge_perception",
        "persons": [
            {
                "person_id": 1,
                "category": "civilian",
                "confidence": 0.92,
                "state": "FALLEN",
                "risk": 0.95,
                "bbox": [100, 120, 300, 420],
            },
            {
                "person_id": 2,
                "category": "rescuer",
                "confidence": 0.89,
                "state": "NORMAL",
                "risk": 0.10,
                "bbox": [420, 100, 600, 400],
            },
        ],
    }

    update_result = vision_tool.run(
        action="update_snapshot",
        snapshot=test_snapshot,
    )

    print("\n1. Update snapshot:")
    print(update_result)

    # ----------------------------------------------------------
    # Test 2: get snapshot
    # ----------------------------------------------------------

    get_result = vision_tool.run(
        action="get_snapshot",
    )

    print("\n2. Get snapshot:")
    print(get_result)

    # ----------------------------------------------------------
    # Test 3: invalid action
    # ----------------------------------------------------------

    invalid_result = vision_tool.run(
        action="invalid_action",
    )

    print("\n3. Invalid action:")
    print(invalid_result)

    # ----------------------------------------------------------
    # Test 4: missing snapshot
    # ----------------------------------------------------------

    missing_result = vision_tool.run(
        action="update_snapshot",
    )

    print("\n4. Missing snapshot:")
    print(missing_result)

    print("\nVisionTool test completed successfully.")
    print(f"Snapshot file: {vision_tool.snapshot_path}")