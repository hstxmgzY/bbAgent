"""Tool registry for managing available tools."""

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from mybot.tools.base import BaseTool
from mybot.tools.builtin_tools import bash, edit_file, read_file, write_file

if TYPE_CHECKING:
    from mybot.core.agent import AgentSession


class ToolRegistry:
    """Registry for all available tools."""

    def __init__(self) -> None:
        """Initialize an empty tool registry."""
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_all(self) -> list[BaseTool]:
        """List all registered tools."""
        return list(self._tools.values())

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Get tool schemas for all registered tools."""
        return [tool.get_tool_schema() for tool in self._tools.values()]

    async def execute_tool(
        self, name: str, session: "AgentSession", **kwargs: Any
    ) -> str:
        """Execute a tool by name."""
        tool = self.get(name)
        if tool is None:
            raise ValueError(f"Tool not found: {name}")

        return await tool.execute(session=session, **kwargs)

    @classmethod
    def with_builtins(cls, allowed: Iterable[str] = ()) -> "ToolRegistry":
        """Create a registry containing only explicitly allowed built-in tools."""

        registry = cls()
        builtins = {
            tool.name: tool for tool in (read_file, write_file, edit_file, bash)
        }
        for name in allowed:
            builtin = builtins.get(name)
            if builtin:
                registry.register(builtin)

        return registry
