"""Explicit, bounded subagent dispatch tool."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING

from mybot.core.events import (
    AgentEventSource,
    CancelDispatchEvent,
    DispatchEvent,
    DispatchResultEvent,
)
from mybot.tools.base import BaseTool, tool
from mybot.utils.def_loader import DefNotFoundError

if TYPE_CHECKING:
    from mybot.core.agent import AgentSession
    from mybot.core.context import SharedContext


def create_subagent_dispatch_tool(
    current_agent_id: str,
    context: "SharedContext",
    dispatch_to: list[str],
    timeout_seconds: float = 120.0,
) -> BaseTool | None:
    """Create a dispatch tool restricted to the agent's explicit allowlist."""
    allowed_ids = list(dict.fromkeys(dispatch_to))
    discovered = {agent.id: agent for agent in context.agent_loader.discover_agents()}
    dispatchable_agents = [
        discovered[agent_id]
        for agent_id in allowed_ids
        if agent_id != current_agent_id and agent_id in discovered
    ]
    if not dispatchable_agents:
        return None

    agents_desc = "<available_agents>\n"
    for agent_def in dispatchable_agents:
        agents_desc += f'  <agent id="{agent_def.id}">{agent_def.description}</agent>\n'
    agents_desc += "</available_agents>"
    dispatchable_ids = [agent.id for agent in dispatchable_agents]

    @tool(
        name="subagent_dispatch",
        description=f"Dispatch a task to an authorized subagent.\n{agents_desc}",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "enum": dispatchable_ids,
                    "description": "ID of the agent to dispatch to",
                },
                "task": {
                    "type": "string",
                    "description": "The task for the subagent to perform",
                },
                "context": {
                    "type": "string",
                    "description": "Optional context information for the subagent",
                },
            },
            "required": ["agent_id", "task"],
        },
    )
    async def subagent_dispatch(
        agent_id: str, task: str, session: "AgentSession", context: str = ""
    ) -> str:
        from mybot.core.agent import Agent

        if agent_id not in dispatchable_ids:
            raise ValueError(f"Agent '{agent_id}' is not authorized for dispatch")
        try:
            agent_def = session.shared_context.agent_loader.load(agent_id)
        except DefNotFoundError:
            raise ValueError(f"Agent '{agent_id}' not found") from None

        agent = Agent(agent_def, session.shared_context)
        agent_session = agent.new_session(AgentEventSource(agent_id=current_agent_id))
        child_session_id = agent_session.session_id
        job_id = str(uuid.uuid4())
        user_message = task if not context else f"{task}\n\nContext:\n{context}"
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[DispatchResultEvent] = loop.create_future()

        async def handle_result(event: DispatchResultEvent) -> None:
            if (
                event.session_id == child_session_id
                and event.job_id == job_id
                and not result_future.done()
            ):
                result_future.set_result(event)

        async def cancel_child(reason: str) -> None:
            await session.shared_context.eventbus.publish(
                CancelDispatchEvent(
                    session_id=child_session_id,
                    source=AgentEventSource(agent_id=current_agent_id),
                    content=reason,
                    job_id=job_id,
                )
            )

        session.shared_context.eventbus.subscribe(DispatchResultEvent, handle_result)
        try:
            await session.shared_context.eventbus.publish(
                DispatchEvent(
                    session_id=child_session_id,
                    source=AgentEventSource(agent_id=current_agent_id),
                    content=user_message,
                    timestamp=time.time(),
                    parent_session_id=session.session_id,
                    job_id=job_id,
                )
            )
            try:
                result_event = await asyncio.wait_for(
                    result_future, timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await cancel_child("dispatch timed out")
                return json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "timeout",
                            "message": f"subagent exceeded {timeout_seconds:g} seconds",
                        },
                        "job_id": job_id,
                        "session_id": child_session_id,
                    }
                )
            except asyncio.CancelledError:
                await cancel_child("parent task was cancelled")
                raise
        finally:
            session.shared_context.eventbus.unsubscribe(handle_result)

        if result_event.error:
            return json.dumps(
                {
                    "ok": False,
                    "error": {"code": "subagent_error", "message": result_event.error},
                    "job_id": job_id,
                    "session_id": child_session_id,
                }
            )
        return json.dumps(
            {
                "ok": True,
                "result": result_event.content,
                "job_id": job_id,
                "session_id": child_session_id,
            }
        )

    return subagent_dispatch
