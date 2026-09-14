"""Base tool interface and decorator."""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
import inspect
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from mybot.core.agent import AgentSession


@dataclass(frozen=True)
class ToolExecutionContext:
    """Stable runtime identity for one model tool call."""

    session_id: str
    turn_id: str
    tool_call_id: str
    agent_id: str
    source: str


class BaseTool(ABC):
    """Abstract base class for all tools."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for function calling

    @abstractmethod
    async def execute(
        self,
        session: "AgentSession",
        execution_context: ToolExecutionContext | None = None,
        **kwargs: Any,
    ) -> str:
        """Execute the tool."""

    def get_tool_schema(self) -> dict[str, Any]:
        """Get the tool/function schema for LiteLLM."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool(name: str, description: str, parameters: dict[str, Any]) -> Callable:
    """Decorator to register a function as a tool."""

    def decorator(func: Callable) -> "FunctionTool":
        return FunctionTool(name, description, parameters, func)

    return decorator


class FunctionTool(BaseTool):
    """A tool created from a function using the @tool decorator."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: Callable,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self._func = func
        self._accepts_execution_context = (
            "execution_context" in inspect.signature(func).parameters
        )

    async def execute(
        self,
        session: "AgentSession",
        execution_context: ToolExecutionContext | None = None,
        **kwargs: Any,
    ) -> str:
        """Execute the underlying function."""
        if self._accepts_execution_context:
            kwargs["execution_context"] = execution_context
        result = self._func(session=session, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)
