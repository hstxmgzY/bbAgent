import asyncio
import json

import pytest

from mybot.core.agent import Agent, AgentSession
from mybot.core.context import SharedContext
from mybot.core.dispatch_jobs import DispatchJobError
from mybot.core.events import (
    CliEventSource,
    InboundEvent,
    JobCompletedEvent,
    OutboundEvent,
    WebSocketEventSource,
)
from mybot.server.agent_worker import AgentWorker
from mybot.server.dispatch_worker import (
    CompletionOutboxWorker,
    CompletionRouter,
    DispatchJobWorker,
)
from mybot.server.websocket_worker import WebSocketWorker
from mybot.tools.base import ToolExecutionContext
from mybot.tools.subagent_tool import create_subagent_submit_tool
from mybot.utils.config import Config, DispatchConfig, LLMConfig


def make_context(
    tmp_path, *, completion_modes="[poll, notify, resume_parent]", poll_interval=0
):
    agents_path = tmp_path / "agents"
    definitions = {
        "assistant": f"""---
name: Assistant
description: parent
tools: [subagent_submit, subagent_status, subagent_cancel]
dispatch_to: [researcher]
dispatch_completion_modes: {completion_modes}
default_dispatch_completion_mode: poll
dispatch_execution_timeout_seconds: 10
max_concurrency: 2
---
Parent.
""",
        "researcher": """---
name: Researcher
description: child
tools: []
retry_safe: true
max_concurrency: 1
---
Child.
""",
    }
    for agent_id, content in definitions.items():
        directory = agents_path / agent_id
        directory.mkdir(parents=True)
        (directory / "AGENT.md").write_text(content, encoding="utf-8")
    config = Config(
        workspace=tmp_path,
        llm=LLMConfig(provider="openai", model="test", api_key="secret"),
        default_agent="assistant",
        dispatch=DispatchConfig(
            scan_interval_seconds=0.01,
            lease_seconds=2,
            sync_wait_seconds=0.01,
            poll_min_interval_seconds=poll_interval,
        ),
    )
    return SharedContext(config, channels=[])


def submit(context, session, *, call_id="call-1", mode="poll"):
    tool = create_subagent_submit_tool(
        "assistant",
        context,
        ["researcher"],
        completion_modes=["poll", "notify", "resume_parent"],
    )
    assert tool is not None
    execution = ToolExecutionContext(
        session_id=session.session_id,
        turn_id="turn-1",
        tool_call_id=call_id,
        agent_id="assistant",
        source=str(session.source),
    )
    raw = asyncio.run(
        tool.execute(
            session,
            execution_context=execution,
            agent_id="researcher",
            task="do work",
            completion_mode=mode,
        )
    )
    return json.loads(raw)


def test_submit_is_idempotent_by_parent_session_and_tool_call(tmp_path):
    context = make_context(tmp_path)
    session = Agent(context.agent_loader.load("assistant"), context).new_session(
        CliEventSource()
    )

    first = submit(context, session)
    second = submit(context, session)

    assert first["job_id"] == second["job_id"]
    assert len(context.dispatch_repository.list_jobs(session.session_id)) == 1


def test_claim_and_cancel_completion_race_has_one_terminal_outbox(tmp_path):
    context = make_context(tmp_path)
    session = Agent(context.agent_loader.load("assistant"), context).new_session(
        CliEventSource()
    )
    result = submit(context, session)
    repository = context.dispatch_repository
    job = repository.claim_next("researcher", "worker-1", 10)
    assert job is not None

    cancelled = repository.request_cancel(job.job_id, session.session_id)
    assert cancelled.status == "cancel_requested"
    assert (
        repository.finish_success(job.job_id, "worker-1", '{"content":"late"}') is False
    )
    assert repository.finish_cancelled(job.job_id, "worker-1") is True
    assert repository.finish_cancelled(job.job_id, "worker-1") is False
    assert repository.get_job(job.job_id).status == "cancelled"
    assert len(repository.pending_outbox()) == 1


def test_status_and_cancel_enforce_parent_session_ownership(tmp_path):
    context = make_context(tmp_path)
    agent = Agent(context.agent_loader.load("assistant"), context)
    owner = agent.new_session(CliEventSource())
    other = agent.new_session(CliEventSource())
    result = submit(context, owner)

    with pytest.raises(DispatchJobError, match="not found"):
        context.dispatch_service.status(result["job_id"], other.session_id)
    with pytest.raises(DispatchJobError, match="not found"):
        context.dispatch_service.cancel(result["job_id"], other.session_id)


def test_status_snapshot_and_poll_rate_limit(tmp_path):
    context = make_context(tmp_path, poll_interval=10)
    session = Agent(context.agent_loader.load("assistant"), context).new_session(
        CliEventSource()
    )
    result = submit(context, session)

    first = context.dispatch_service.status(
        result["job_id"], session.session_id, "turn-1"
    )
    repeated = context.dispatch_service.status(
        result["job_id"], session.session_id, "turn-1"
    )
    assert first == repeated
    assert first["deferred"] is True
    with pytest.raises(DispatchJobError) as error:
        context.dispatch_service.status(result["job_id"], session.session_id, "turn-2")
    assert error.value.code == "poll_rate_limited"


def test_dispatch_worker_persists_child_result(tmp_path, monkeypatch):
    async def fake_chat(self, message):
        return f"finished: {message}"

    monkeypatch.setattr(AgentSession, "chat", fake_chat)
    context = make_context(tmp_path)
    session = Agent(context.agent_loader.load("assistant"), context).new_session(
        CliEventSource()
    )
    result = submit(context, session)

    async def run_worker():
        worker = DispatchJobWorker(context)
        worker.start()
        try:
            for _ in range(100):
                job = context.dispatch_repository.get_job(result["job_id"])
                if job and job.is_terminal:
                    return job
                await asyncio.sleep(0.01)
            raise AssertionError("job did not finish")
        finally:
            await worker.stop()

    job = asyncio.run(run_worker())
    assert job.status == "succeeded"
    assert job.result == {"content": "finished: do work"}


def test_notify_routes_to_parent_session_with_event_id(tmp_path):
    context = make_context(tmp_path)
    session = Agent(context.agent_loader.load("assistant"), context).new_session(
        CliEventSource()
    )
    result = submit(context, session, mode="notify")
    repository = context.dispatch_repository
    claimed = repository.claim_next("researcher", "worker-1", 10)
    assert claimed is not None
    assert repository.finish_success(claimed.job_id, "worker-1", '{"content":"answer"}')
    record = repository.pending_outbox()[0]
    event = JobCompletedEvent(
        session_id=session.session_id,
        source=CliEventSource(),
        content="",
        event_id=record.event_id,
        job_id=claimed.job_id,
        child_session_id=claimed.child_session_id,
        status="succeeded",
    )

    asyncio.run(CompletionRouter(context).handle_event(event))
    outbound = context.eventbus._queue.get_nowait()
    # The submit wake-up precedes the completion notification in this direct test.
    while not isinstance(outbound, OutboundEvent):
        outbound = context.eventbus._queue.get_nowait()
    assert outbound.session_id == session.session_id
    assert outbound.event_id == record.event_id
    assert "answer" in outbound.content
    assert repository.pending_outbox() == []


def test_same_parent_session_load_and_turn_are_serialized(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    agent_def = context.agent_loader.load("assistant")
    session = Agent(agent_def, context).new_session(CliEventSource())
    observed_message_counts = []

    async def fake_run_turn(self, turn_id):
        observed_message_counts.append(len(self.state.messages))
        await asyncio.sleep(0.02)
        return "ok"

    monkeypatch.setattr(AgentSession, "_run_turn", fake_run_turn)
    worker = AgentWorker(context)
    first = InboundEvent(
        session_id=session.session_id, source=CliEventSource(), content="first"
    )
    second = InboundEvent(
        session_id=session.session_id, source=CliEventSource(), content="second"
    )

    async def run_both():
        await asyncio.gather(
            worker.exec_session(first, agent_def),
            worker.exec_session(second, agent_def),
        )

    asyncio.run(run_both())

    assert observed_message_counts == [1, 2]


def test_websocket_outbound_is_source_scoped(tmp_path):
    class FakeSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    context = make_context(tmp_path)
    agent_def = context.agent_loader.load("assistant")
    session_a = Agent(agent_def, context).new_session(WebSocketEventSource("a"))
    session_b = Agent(agent_def, context).new_session(WebSocketEventSource("b"))
    socket_a = FakeSocket()
    socket_b = FakeSocket()
    worker = WebSocketWorker(context)
    worker.clients.update({socket_a, socket_b})
    worker._bind_client(socket_a, "platform-ws:a")
    worker._bind_client(socket_b, "platform-ws:b")

    asyncio.run(
        worker.handle_event(
            OutboundEvent(
                session_id=session_a.session_id,
                source=WebSocketEventSource("a"),
                content="private",
            )
        )
    )

    assert [message["content"] for message in socket_a.messages] == ["private"]
    assert socket_b.messages == []
    assert session_b.session_id != session_a.session_id

    completion = OutboundEvent(
        session_id=session_a.session_id,
        source=WebSocketEventSource("a"),
        content="completion",
        event_id="event-1",
    )
    asyncio.run(worker.handle_event(completion))
    assert [message["content"] for message in socket_a.messages] == ["private"]

    worker.authenticated_clients.add(socket_a)
    asyncio.run(worker.handle_event(completion))
    assert [message["content"] for message in socket_a.messages] == [
        "private",
        "completion",
    ]


def test_resume_parent_is_persistently_idempotent(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    session = Agent(context.agent_loader.load("assistant"), context).new_session(
        CliEventSource()
    )
    result = submit(context, session, mode="resume_parent")
    repository = context.dispatch_repository
    claimed = repository.claim_next("researcher", "worker-1", 10)
    assert claimed is not None
    assert repository.finish_success(claimed.job_id, "worker-1", '{"content":"data"}')
    record = repository.pending_outbox()[0]
    event = JobCompletedEvent(
        session_id=session.session_id,
        source=CliEventSource(),
        content="",
        event_id=record.event_id,
        job_id=result["job_id"],
        child_session_id=claimed.child_session_id,
        status="succeeded",
    )
    model_calls = 0

    async def fake_run_turn(self, turn_id, response_metadata=None):
        nonlocal model_calls
        model_calls += 1
        message = {"role": "assistant", "content": "parent answer"}
        self.state.add_message(message, metadata=response_metadata)
        return "parent answer"

    monkeypatch.setattr(AgentSession, "_run_turn", fake_run_turn)
    router = CompletionRouter(context)

    async def deliver_twice():
        await router.handle_event(event)
        # Simulate another outbox delivery after the process forgot in-memory state.
        await router.handle_event(event)

    asyncio.run(deliver_twice())

    history = context.history_store.get_messages(session.session_id)
    synthetic = [m for m in history if m.metadata.get("synthetic")]
    completed = [m for m in history if m.metadata.get("resume_completed")]
    assert model_calls == 1
    assert len(synthetic) == 2
    assert len(completed) == 1


def test_agent_concurrency_gate_is_shared_between_workers(tmp_path):
    context = make_context(tmp_path)
    first = context.agent_semaphore("researcher", 1)
    second = context.agent_semaphore("researcher", 99)
    assert first is second


def test_submit_to_notify_pipeline_end_to_end(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    parent = Agent(context.agent_loader.load("assistant"), context).new_session(
        CliEventSource()
    )
    tool = create_subagent_submit_tool(
        "assistant",
        context,
        ["researcher"],
        completion_modes=["poll", "notify", "resume_parent"],
    )
    assert tool is not None
    notifications = []

    async def fake_run_turn(self, turn_id, response_metadata=None):
        self.state.add_message(
            {"role": "assistant", "content": "child result"},
            metadata=response_metadata,
        )
        return "child result"

    async def capture(event):
        if event.event_id:
            notifications.append(event)

    monkeypatch.setattr(AgentSession, "_run_turn", fake_run_turn)
    context.eventbus.subscribe(OutboundEvent, capture)

    async def run_pipeline():
        router = CompletionRouter(context)
        workers = [
            context.eventbus,
            DispatchJobWorker(context),
            CompletionOutboxWorker(context),
            router,
        ]
        for worker in workers:
            worker.start()
        try:
            raw = await tool.execute(
                parent,
                execution_context=ToolExecutionContext(
                    session_id=parent.session_id,
                    turn_id="turn-e2e",
                    tool_call_id="call-e2e",
                    agent_id="assistant",
                    source=str(parent.source),
                ),
                agent_id="researcher",
                task="long task",
                completion_mode="notify",
            )
            accepted = json.loads(raw)
            assert accepted["status"] == "queued"
            for _ in range(300):
                if notifications:
                    return accepted
                await asyncio.sleep(0.01)
            raise AssertionError("completion notification was not delivered")
        finally:
            for worker in reversed(workers):
                await worker.stop()

    accepted = asyncio.run(run_pipeline())
    job = context.dispatch_repository.get_job(accepted["job_id"])
    assert job.status == "succeeded"
    assert notifications[0].session_id == parent.session_id
    assert "child result" in notifications[0].content
