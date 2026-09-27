from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """
    Base class for all Agent tools.

    Each tool follows the same basic interface:

        input  ->  run(**kwargs)  ->  JSON-like dict output

    The Agent does not need to know the internal implementation
    of a tool. It only needs to know:
        - tool name
        - tool description
        - tool input schema
        - tool.run(...)
    """

    # ----------------------------------------------------------
    # Tool metadata
    # ----------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Unique tool name.

        Example:
            "database_tool"
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def description(self) -> str:
        """
        Human-readable description of the tool.
        """
        raise NotImplementedError

    @property
    def input_schema(self) -> dict[str, Any]:
        """
        Describe the expected input parameters.

        Subclasses can override this property.

        The schema is intentionally lightweight for MVP.
        It can later be converted to OpenAI-compatible
        function/tool schema if needed.
        """
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }

    # ----------------------------------------------------------
    # Tool execution
    # ----------------------------------------------------------

    @abstractmethod
    def run(self, **kwargs: Any) -> dict[str, Any]:
        """
        Execute the tool.

        Returns:
            A JSON-serializable dictionary.

        Example:
            {
                "ok": True,
                "message": "Event saved successfully."
            }
        """
        raise NotImplementedError

    # ----------------------------------------------------------
    # Tool information
    # ----------------------------------------------------------

    def info(self) -> dict[str, Any]:
        """
        Return tool metadata.

        This is useful for:
        - debugging
        - Agent planner
        - ToolRegistry
        - future LLM tool selection
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def __repr__(self) -> str:
        return f"<Tool name='{self.name}'>"


class ToolRegistry:
    """
    Registry for Agent tools.

    Responsibilities:
        1. Register tools
        2. Find tools by name
        3. Execute a registered tool
        4. List available tools

    Example:

        registry = ToolRegistry()

        registry.register(MyTool())

        result = registry.execute(
            "my_tool",
            value=123
        )
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ----------------------------------------------------------
    # Register
    # ----------------------------------------------------------

    def register(self, tool: Tool) -> None:
        """
        Register one tool.

        Raises:
            TypeError:
                If the object is not a Tool.

            ValueError:
                If another tool with the same name
                has already been registered.
        """
        if not isinstance(tool, Tool):
            raise TypeError(
                "Only Tool instances can be registered."
            )

        tool_name = tool.name

        if not tool_name:
            raise ValueError(
                "Tool name cannot be empty."
            )

        if tool_name in self._tools:
            raise ValueError(
                f"Tool '{tool_name}' is already registered."
            )

        self._tools[tool_name] = tool

    # ----------------------------------------------------------
    # Unregister
    # ----------------------------------------------------------

    def unregister(self, tool_name: str) -> None:
        """
        Remove a registered tool.

        Raises:
            KeyError:
                If the tool does not exist.
        """
        if tool_name not in self._tools:
            raise KeyError(
                f"Tool '{tool_name}' is not registered."
            )

        del self._tools[tool_name]

    # ----------------------------------------------------------
    # Get
    # ----------------------------------------------------------

    def get(self, tool_name: str) -> Tool:
        """
        Get a registered tool by name.

        Raises:
            KeyError:
                If the tool does not exist.
        """
        if tool_name not in self._tools:
            raise KeyError(
                f"Tool '{tool_name}' is not registered."
            )

        return self._tools[tool_name]

    # ----------------------------------------------------------
    # Check
    # ----------------------------------------------------------

    def has(self, tool_name: str) -> bool:
        """
        Check whether a tool is registered.
        """
        return tool_name in self._tools

    # ----------------------------------------------------------
    # List
    # ----------------------------------------------------------

    def list_tools(self) -> list[str]:
        """
        Return names of all registered tools.
        """
        return list(self._tools.keys())

    def list_infos(self) -> list[dict[str, Any]]:
        """
        Return metadata for all registered tools.
        """
        return [
            tool.info()
            for tool in self._tools.values()
        ]

    # ----------------------------------------------------------
    # Execute
    # ----------------------------------------------------------

    def execute(
        self,
        tool_name: str,
        **kwargs: Any
    ) -> dict[str, Any]:
        """
        Execute a registered tool.

        Example:

            registry.execute(
                "database_tool",
                action="save_event",
                type="PERSON_HIGH_RISK",
                severity="HIGH"
            )
        """
        tool = self.get(tool_name)

        result = tool.run(**kwargs)

        if not isinstance(result, dict):
            raise TypeError(
                f"Tool '{tool_name}' must return a dict, "
                f"but got {type(result).__name__}."
            )

        return result

    # ----------------------------------------------------------
    # Clear
    # ----------------------------------------------------------

    def clear(self) -> None:
        """
        Remove all registered tools.
        """
        self._tools.clear()

    # ----------------------------------------------------------
    # Debug representation
    # ----------------------------------------------------------

    def __repr__(self) -> str:
        tool_names = ", ".join(self.list_tools())

        return (
            f"<ToolRegistry "
            f"tools=[{tool_names}]>"
        )


# ==============================================================
# Simple test
# ==============================================================

class _DemoTool(Tool):
    """
    Small internal test tool.

    This class is only used when running this file directly.
    It is not part of the actual project tool set.
    """

    @property
    def name(self) -> str:
        return "demo_tool"

    @property
    def description(self) -> str:
        return "A simple demo tool for testing ToolRegistry."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message to return.",
                }
            },
            "required": ["message"],
            "additionalProperties": False,
        }

    def run(self, **kwargs: Any) -> dict[str, Any]:
        message = kwargs.get("message")

        if message is None:
            return {
                "ok": False,
                "error": "message is required.",
            }

        return {
            "ok": True,
            "message": message,
        }


if __name__ == "__main__":
    print("Testing ToolRegistry...")

    registry = ToolRegistry()

    # Register demo tool
    demo_tool = _DemoTool()
    registry.register(demo_tool)

    # Check registration
    print("Registered tools:")
    print(registry.list_tools())

    # Print tool metadata
    print("\nTool information:")
    print(registry.list_infos())

    # Execute tool
    result = registry.execute(
        "demo_tool",
        message="ToolRegistry works!",
    )

    print("\nExecution result:")
    print(result)

    # Check tool
    print("\nHas demo_tool:", registry.has("demo_tool"))

    print("\nToolRegistry test completed successfully.")