import uuid
import json
import logging
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from mybot.core.context_guard import ContextGuard
from mybot.core.session_state import SessionState
from mybot.core.events import EventSource
from mybot.provider.llm import LLMProvider
from mybot.tools.registry import ToolRegistry
from mybot.tools.skill_tool import create_skill_tool
from mybot.tools.websearch_tool import create_websearch_tool
from mybot.tools.webread_tool import create_webread_tool
from mybot.tools.post_message_tool import create_post_message_tool
from mybot.tools.research_tool import create_research_tool
from mybot.tools.subagent_tool import create_subagent_dispatch_tool
from mybot.tools.subagent_tool import (
    create_subagent_cancel_tool,
    create_subagent_status_tool,
    create_subagent_submit_tool,
)
from mybot.tools.base import ToolExecutionContext

from litellm.types.completion import (
    ChatCompletionMessageParam as Message,
    ChatCompletionMessageToolCallParam,
)

if TYPE_CHECKING:
    from mybot.core.context import SharedContext
    from mybot.core.agent_loader import AgentDef
    from mybot.provider.llm import LLMToolCall


class Agent:
    """A configured agent that creates and manages conversation sessions."""

    def __init__(self, agent_def: "AgentDef", context: "SharedContext") -> None:
        self.agent_def = agent_def
        self.context = context
        self.llm = LLMProvider.from_config(agent_def.llm)

    def _build_tools(self, include_post_message: bool) -> ToolRegistry:
        """Build a ToolRegistry with tools appropriate for the session."""
        enabled = set(self.agent_def.tools)
        registry = ToolRegistry.with_builtins(enabled)

        # Register skill tool if allowed
        if "skill" in enabled and self.agent_def.allow_skills:
            skill_tool = create_skill_tool(self.context.skill_loader)
            if skill_tool:
                registry.register(skill_tool)

        if "websearch" in enabled:
            websearch_tool = create_websearch_tool(self.context)
            if websearch_tool:
                registry.register(websearch_tool)

        if "webread" in enabled:
            webread_tool = create_webread_tool(self.context)
            if webread_tool:
                registry.register(webread_tool)

        if "research" in enabled:
            registry.register(create_research_tool(self.context))

        if include_post_message and "post_message" in enabled:
            post_tool = create_post_message_tool(self.context)
            if post_tool:
                registry.register(post_tool)

        # Register subagent dispatch tool
        if "subagent_dispatch" in enabled and self.agent_def.dispatch_to:
            subagent_tool = create_subagent_dispatch_tool(
                self.agent_def.id,
                self.context,
                dispatch_to=self.agent_def.dispatch_to,
                timeout_seconds=self.agent_def.dispatch_timeout_seconds,
                cancel_on_sync_timeout=(
                    self.context.config.dispatch.cancel_on_sync_timeout
                ),
            )
            if subagent_tool:
                registry.register(subagent_tool)

        if "subagent_submit" in enabled and self.agent_def.dispatch_to:
            submit_tool = create_subagent_submit_tool(
                self.agent_def.id,
                self.context,
                dispatch_to=self.agent_def.dispatch_to,
                default_completion_mode=self.agent_def.default_dispatch_completion_mode,
                completion_modes=self.agent_def.dispatch_completion_modes,
            )
            if submit_tool:
                registry.register(submit_tool)

        if "subagent_status" in enabled:
            registry.register(create_subagent_status_tool(self.context))

        if "subagent_cancel" in enabled:
            registry.register(create_subagent_cancel_tool(self.context))

        return registry

    def _get_token_threshold(self) -> int:
        """Get token threshold based on model's context window."""
        # Default to 80% of 200k context
        return 160000

    def new_session(
        self,
        source: EventSource,
        session_id: str | None = None,
    ) -> "AgentSession":
        """Create a new conversation session."""
        session_id = session_id or str(uuid.uuid4())

        include_post_message = source.is_cron
        tools = self._build_tools(include_post_message)

        # Create context guard for this session
        context_guard = ContextGuard(
            shared_context=self.context,
            token_threshold=self._get_token_threshold(),
        )

        state = SessionState(
            session_id=session_id,
            agent=self,
            messages=[],
            source=source,
            shared_context=self.context,
        )

        session = AgentSession(
            agent=self,
            state=state,
            context_guard=context_guard,
            tools=tools,
        )

        self.context.history_store.create_session(self.agent_def.id, session_id, source)
        return session

    def resume_session(self, session_id: str) -> "AgentSession":
        """Load an existing conversation session."""
        session_query = [
            session
            for session in self.context.history_store.list_sessions()
            if session.id == session_id
        ]
        if not session_query:
            raise ValueError(f"Session not found: {session_id}")

        session_info = session_query[0]
        source = session_info.get_source()
        include_post_message = source.is_cron

        # Get all messages (no max_history limit)
        history_messages = self.context.history_store.get_messages(session_id)

        # Convert HistoryMessage to litellm Message format
        messages: list[Message] = [msg.to_message() for msg in history_messages]

        # Build tools for resumed session
        tools = self._build_tools(include_post_message)

        # Create context guard
        context_guard = ContextGuard(
            shared_context=self.context,
            token_threshold=self._get_token_threshold(),
        )

        # Create SessionState with loaded messages
        state = SessionState(
            session_id=session_info.id,
            agent=self,
            messages=messages,
            source=source,
            shared_context=self.context,
        )

        return AgentSession(
            agent=self,
            state=state,
            context_guard=context_guard,
            tools=tools,
        )


@dataclass
class AgentSession:
    """Chat orchestrator - operates on swappable SessionState."""

    agent: Agent
    state: SessionState
    context_guard: ContextGuard
    tools: ToolRegistry
    started_at: datetime = field(default_factory=datetime.now)

    @property
    def session_id(self) -> str:
        """Delegate to state."""
        return self.state.session_id

    @property
    def source(self) -> "EventSource":
        return self.state.source

    @property
    def shared_context(self) -> "SharedContext":
        """Delegate to state."""
        return self.state.shared_context

    async def chat(self, message: str) -> str:
        """Send a message to the LLM and get a response."""
        gate = self.shared_context.agent_semaphore(
            self.agent.agent_def.id, self.agent.agent_def.max_concurrency
        )
        async with gate:
            async with self.shared_context.session_lock(self.session_id):
                user_msg: Message = {"role": "user", "content": message}
                self.state.add_message(user_msg)
                return await self._run_turn(str(uuid.uuid4()))

    async def resume_with_dispatch_result(
        self, *, event_id: str, job_id: str, result: str
    ) -> str:
        """Run a new parent turn from an auditable synthetic tool result."""
        gate = self.shared_context.agent_semaphore(
            self.agent.agent_def.id, self.agent.agent_def.max_concurrency
        )
        async with gate:
            async with self.shared_context.session_lock(self.session_id):
                tool_call_id = f"dispatch-result-{event_id}"
                metadata = {"event_id": event_id, "job_id": job_id, "synthetic": True}
                history = self.shared_context.history_store.get_messages(
                    self.session_id
                )
                for message in reversed(history):
                    if (
                        message.metadata.get("event_id") == event_id
                        and message.metadata.get("resume_completed") is True
                    ):
                        return message.content
                if not any(
                    message.metadata.get("event_id") == event_id
                    and message.metadata.get("synthetic") is True
                    for message in history
                ):
                    assistant_msg: Message = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": "subagent_result_ready",
                                    "arguments": json.dumps({"job_id": job_id}),
                                },
                            }
                        ],
                    }
                    tool_msg: Message = {
                        "role": "tool",
                        "content": result,
                        "tool_call_id": tool_call_id,
                    }
                    self.state.add_message(assistant_msg, metadata=metadata)
                    self.state.add_message(tool_msg, metadata=metadata)
                return await self._run_turn(
                    str(uuid.uuid4()),
                    response_metadata={
                        "event_id": event_id,
                        "job_id": job_id,
                        "resume_completed": True,
                    },
                )

    async def _run_turn(
        self,
        turn_id: str,
        response_metadata: dict[str, object] | None = None,
    ) -> str:
        """Run one serialized model turn after its initiating messages are stored."""

        tool_schemas = self.tools.get_tool_schemas()
        logger = logging.getLogger(__name__)
        tool_rounds = 0

        while True:
            self.state = await self.context_guard.check_and_compact(self.state)
            messages = self.state.build_messages()
            content, tool_calls, stop_reason = await self.agent.llm.chat(
                messages, tool_schemas
            )

            tool_call_dicts: list[ChatCompletionMessageToolCallParam] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in tool_calls
            ]
            assistant_msg: Message = {
                "role": "assistant",
                "content": content,
            }
            if tool_call_dicts:
                assistant_msg["tool_calls"] = tool_call_dicts

            final_response = stop_reason != "tool_calls"
            self.state.add_message(
                assistant_msg,
                metadata=response_metadata if final_response else None,
            )

            if stop_reason == "tool_calls":
                tool_rounds += 1
                if (
                    tool_rounds
                    > self.shared_context.config.dispatch.max_tool_rounds_per_turn
                ):
                    for tool_call in tool_calls:
                        self.state.add_message(
                            {
                                "role": "tool",
                                "content": json.dumps(
                                    {
                                        "ok": False,
                                        "error": {
                                            "code": "tool_round_limit",
                                            "message": "maximum tool rounds reached",
                                        },
                                    }
                                ),
                                "tool_call_id": tool_call.id,
                            }
                        )
                    return "Maximum tool rounds reached for this turn."
                await self._handle_tool_calls(tool_calls, turn_id)
                continue

            if stop_reason == "length":
                logger.warning(
                    "LLM response truncated (max_tokens reached), "
                    "returning partial response"
                )

            if stop_reason == "content_filter":
                logger.warning("LLM response filtered by content filter")
                return content if content else "I'm unable to respond to that request."

            break

        return content

    async def _handle_tool_calls(
        self,
        tool_calls: list["LLMToolCall"],
        turn_id: str,
    ) -> None:
        """Handle tool calls from the LLM response."""
        tool_call_results = await asyncio.gather(
            *[self._execute_tool_call(tool_call, turn_id) for tool_call in tool_calls]
        )

        for tool_call, result in zip(tool_calls, tool_call_results):
            tool_msg: Message = {
                "role": "tool",
                "content": result,
                "tool_call_id": tool_call.id,
            }
            self.state.add_message(tool_msg)

    async def _execute_tool_call(
        self,
        tool_call: "LLMToolCall",
        turn_id: str,
    ) -> str:
        """Execute a single tool call."""
        # Extract key arguments
        try:
            args = json.loads(tool_call.arguments)
        except json.JSONDecodeError:
            args = {}

        try:
            execution_context = ToolExecutionContext(
                session_id=self.session_id,
                turn_id=turn_id,
                tool_call_id=tool_call.id,
                agent_id=self.agent.agent_def.id,
                source=str(self.source),
            )
            result = await self.tools.execute_tool(
                tool_call.name,
                session=self,
                execution_context=execution_context,
                **args,
            )
        except Exception as e:
            result = f"Error executing tool: {e}"

        return result
