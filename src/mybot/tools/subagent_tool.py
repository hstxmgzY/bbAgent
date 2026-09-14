"""Persistent asynchronous subagent dispatch tools."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any

from mybot.core.dispatch_jobs import (
    CompletionMode,
    DispatchJob,
    DispatchJobError,
    TERMINAL_STATUSES,
)
from mybot.core.events import AgentEventSource, CancelDispatchEvent, DispatchQueuedEvent
from mybot.tools.base import BaseTool, ToolExecutionContext, tool

if TYPE_CHECKING:
    from mybot.core.agent import AgentSession
    from mybot.core.context import SharedContext


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _error(exc: DispatchJobError) -> str:
    return _json({"ok": False, "error": exc.to_dict()})


def _execution_identity(
    session: "AgentSession", execution_context: ToolExecutionContext | None
) -> tuple[str, str]:
    if execution_context is not None:
        return execution_context.tool_call_id, execution_context.turn_id
    value = str(uuid.uuid4())
    return value, value


def _dispatchable_agents(
    current_agent_id: str, context: "SharedContext", dispatch_to: list[str]
) -> tuple[list[str], str]:
    allowed_ids = list(dict.fromkeys(dispatch_to))
    discovered = {agent.id: agent for agent in context.agent_loader.discover_agents()}
    agents = [
        discovered[agent_id]
        for agent_id in allowed_ids
        if agent_id != current_agent_id and agent_id in discovered
    ]
    description = "<available_agents>\n"
    for agent_def in agents:
        description += f'  <agent id="{agent_def.id}">{agent_def.description}</agent>\n'
    description += "</available_agents>"
    return [agent.id for agent in agents], description


async def _submit(
    *,
    current_agent_id: str,
    agent_id: str,
    task: str,
    context_text: str,
    completion_mode: CompletionMode,
    session: "AgentSession",
    execution_context: ToolExecutionContext | None,
) -> tuple[DispatchJob, bool]:
    tool_call_id, turn_id = _execution_identity(session, execution_context)
    job, created = session.shared_context.dispatch_service.submit(
        parent_session_id=session.session_id,
        parent_agent_id=current_agent_id,
        target_agent_id=agent_id,
        task=task,
        context=context_text,
        completion_mode=completion_mode,
        tool_call_id=tool_call_id,
        turn_id=turn_id,
    )
    if created:
        await session.shared_context.eventbus.publish(
            DispatchQueuedEvent(
                session_id=job.child_session_id,
                source=AgentEventSource(agent_id=current_agent_id),
                content="",
                job_id=job.job_id,
            )
        )
    return job, created


def create_subagent_submit_tool(
    current_agent_id: str,
    context: "SharedContext",
    dispatch_to: list[str],
    default_completion_mode: CompletionMode = "poll",
    completion_modes: list[CompletionMode] | None = None,
) -> BaseTool | None:
    dispatchable_ids, agents_desc = _dispatchable_agents(
        current_agent_id, context, dispatch_to
    )
    if not dispatchable_ids:
        return None
    allowed_modes = completion_modes or ["poll"]

    @tool(
        name="subagent_submit",
        description=(
            "Submit a long-running task to an authorized subagent and return "
            f"immediately with a job ID. Do not poll in the same turn.\n{agents_desc}"
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "enum": dispatchable_ids},
                "task": {"type": "string", "minLength": 1},
                "context": {"type": "string"},
                "completion_mode": {
                    "type": "string",
                    "enum": allowed_modes,
                    "default": default_completion_mode,
                },
            },
            "required": ["agent_id", "task"],
        },
    )
    async def subagent_submit(
        agent_id: str,
        task: str,
        session: "AgentSession",
        context: str = "",
        completion_mode: CompletionMode = default_completion_mode,
        execution_context: ToolExecutionContext | None = None,
    ) -> str:
        try:
            if session.source.platform_name == "acp" and completion_mode != "poll":
                raise DispatchJobError(
                    "completion_mode_not_supported",
                    "ACP sessions support poll completion mode only",
                )
            job, _ = await _submit(
                current_agent_id=current_agent_id,
                agent_id=agent_id,
                task=task,
                context_text=context,
                completion_mode=completion_mode,
                session=session,
                execution_context=execution_context,
            )
            return _json(session.shared_context.dispatch_service.job_result(job))
        except DispatchJobError as exc:
            return _error(exc)

    return subagent_submit


def create_subagent_status_tool(context: "SharedContext") -> BaseTool:
    @tool(
        name="subagent_status",
        description=(
            "Read a previously submitted subagent job. Do not repeatedly poll "
            "a running job in the same turn."
        ),
        parameters={
            "type": "object",
            "properties": {"job_id": {"type": "string", "minLength": 1}},
            "required": ["job_id"],
        },
    )
    async def subagent_status(
        job_id: str,
        session: "AgentSession",
        execution_context: ToolExecutionContext | None = None,
    ) -> str:
        try:
            turn_id = execution_context.turn_id if execution_context else ""
            return _json(
                context.dispatch_service.status(job_id, session.session_id, turn_id)
            )
        except DispatchJobError as exc:
            return _error(exc)

    return subagent_status


def create_subagent_cancel_tool(context: "SharedContext") -> BaseTool:
    @tool(
        name="subagent_cancel",
        description="Request cancellation of a subagent job owned by this session.",
        parameters={
            "type": "object",
            "properties": {"job_id": {"type": "string", "minLength": 1}},
            "required": ["job_id"],
        },
    )
    async def subagent_cancel(job_id: str, session: "AgentSession") -> str:
        try:
            job = context.dispatch_service.cancel(job_id, session.session_id)
            if job.status == "cancel_requested":
                await context.eventbus.publish(
                    CancelDispatchEvent(
                        session_id=job.child_session_id,
                        source=AgentEventSource(agent_id=session.agent.agent_def.id),
                        content="cancellation requested",
                        job_id=job.job_id,
                    )
                )
            result = context.dispatch_service.job_result(job)
            result["ok"] = True
            return _json(result)
        except DispatchJobError as exc:
            return _error(exc)

    return subagent_cancel


def create_subagent_dispatch_tool(
    current_agent_id: str,
    context: "SharedContext",
    dispatch_to: list[str],
    timeout_seconds: float | None = None,
    cancel_on_sync_timeout: bool | None = None,
) -> BaseTool | None:
    """Create the short-wait compatibility tool backed by the persistent store."""
    dispatchable_ids, agents_desc = _dispatchable_agents(
        current_agent_id, context, dispatch_to
    )
    if not dispatchable_ids:
        return None
    sync_wait = (
        context.config.dispatch.sync_wait_seconds
        if timeout_seconds is None
        else timeout_seconds
    )
    cancel_on_timeout = (
        context.config.dispatch.cancel_on_sync_timeout
        if cancel_on_sync_timeout is None
        else cancel_on_sync_timeout
    )

    @tool(
        name="subagent_dispatch",
        description=f"Dispatch a short task to an authorized subagent.\n{agents_desc}",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "enum": dispatchable_ids},
                "task": {"type": "string", "minLength": 1},
                "context": {"type": "string"},
            },
            "required": ["agent_id", "task"],
        },
    )
    async def subagent_dispatch(
        agent_id: str,
        task: str,
        session: "AgentSession",
        context: str = "",
        execution_context: ToolExecutionContext | None = None,
    ) -> str:
        try:
            job, _ = await _submit(
                current_agent_id=current_agent_id,
                agent_id=agent_id,
                task=task,
                context_text=context,
                completion_mode="poll",
                session=session,
                execution_context=execution_context,
            )
        except DispatchJobError as exc:
            return _error(exc)

        deadline = asyncio.get_running_loop().time() + sync_wait
        while job.status not in TERMINAL_STATUSES:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.05, remaining))
            job = _get_job(session.shared_context, job.job_id)

        if job.status == "succeeded":
            result = job.result or {}
            return _json(
                {
                    "ok": True,
                    "result": result.get("content", result),
                    "job_id": job.job_id,
                    "session_id": job.child_session_id,
                }
            )
        if job.status in TERMINAL_STATUSES:
            return _json(
                {
                    "ok": False,
                    "error": {
                        "code": job.error_code or "subagent_error",
                        "message": job.error_message or job.status,
                    },
                    "job_id": job.job_id,
                    "session_id": job.child_session_id,
                }
            )
        if cancel_on_timeout:
            try:
                job = session.shared_context.dispatch_service.cancel(
                    job.job_id, session.session_id
                )
                if job.status == "cancel_requested":
                    await session.shared_context.eventbus.publish(
                        CancelDispatchEvent(
                            session_id=job.child_session_id,
                            source=AgentEventSource(agent_id=current_agent_id),
                            content="synchronous dispatch timed out",
                            job_id=job.job_id,
                        )
                    )
            except DispatchJobError:
                pass
            return _json(
                {
                    "ok": False,
                    "error": {
                        "code": "timeout",
                        "message": f"subagent exceeded {sync_wait:g} seconds",
                    },
                    "job_id": job.job_id,
                    "session_id": job.child_session_id,
                }
            )
        return _json(
            {
                "ok": True,
                "accepted": True,
                "job_id": job.job_id,
                "status": job.status,
                "completion_mode": job.completion_mode,
                "session_id": job.child_session_id,
            }
        )

    return subagent_dispatch


def _get_job(context: "SharedContext", job_id: str) -> DispatchJob:
    job = context.dispatch_repository.get_job(job_id)
    if job is None:
        raise RuntimeError(f"dispatch job disappeared: {job_id}")
    return job
