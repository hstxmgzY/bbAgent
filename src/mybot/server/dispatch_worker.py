"""Workers for persistent dispatch execution and completion delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from mybot.core.agent import Agent
from mybot.core.dispatch_jobs import DispatchJob
from mybot.core.events import (
    AgentEventSource,
    CancelDispatchEvent,
    DispatchQueuedEvent,
    JobCompletedEvent,
    OutboundEvent,
)
from mybot.utils.def_loader import DefNotFoundError, InvalidDefError

from .worker import SubscriberWorker, Worker

if TYPE_CHECKING:
    from mybot.core.context import SharedContext

logger = logging.getLogger(__name__)


class DispatchJobWorker(Worker):
    """Claims persisted jobs and executes child sessions."""

    def __init__(self, context: "SharedContext") -> None:
        super().__init__(context)
        self.worker_id = f"dispatch-{uuid.uuid4()}"
        self._wake = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running_by_agent: dict[str, int] = {}
        self._scan_count = 0
        context.eventbus.subscribe(DispatchQueuedEvent, self.wake)
        context.eventbus.subscribe(CancelDispatchEvent, self.cancel)

    async def wake(self, event: DispatchQueuedEvent) -> None:
        self._wake.set()

    async def cancel(self, event: CancelDispatchEvent) -> None:
        task = self._tasks.get(event.job_id)
        if task and not task.done():
            task.cancel()
        self._wake.set()

    async def run(self) -> None:
        self.logger.info("DispatchJobWorker started: %s", self.worker_id)
        await self._recover_expired()
        try:
            while True:
                await self._claim_available()
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=self.context.config.dispatch.scan_interval_seconds,
                    )
                except TimeoutError:
                    pass
                self._wake.clear()
                await self._recover_expired()
                self._scan_count += 1
                if self._scan_count % 3600 == 0:
                    self.context.dispatch_repository.cleanup_terminal(
                        self.context.config.dispatch.job_retention_hours
                    )
        except asyncio.CancelledError:
            for task in list(self._tasks.values()):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            raise

    async def _claim_available(self) -> None:
        for target_id in self.context.dispatch_repository.queued_targets():
            try:
                agent_def = self.context.agent_loader.load(target_id)
            except (DefNotFoundError, InvalidDefError) as exc:
                job = self.context.dispatch_repository.claim_next(
                    target_id,
                    self.worker_id,
                    self.context.config.dispatch.lease_seconds,
                )
                if job is not None:
                    self.context.dispatch_repository.finish_failure(
                        job.job_id,
                        self.worker_id,
                        "agent_not_found",
                        str(exc),
                    )
                continue
            available = agent_def.max_concurrency - self._running_by_agent.get(
                target_id, 0
            )
            for _ in range(max(0, available)):
                job = self.context.dispatch_repository.claim_next(
                    target_id,
                    self.worker_id,
                    self.context.config.dispatch.lease_seconds,
                )
                if job is None:
                    break
                self.logger.info(
                    "dispatch claimed job_id=%s target_agent=%s attempt=%d",
                    job.job_id,
                    job.target_agent_id,
                    job.attempt_count,
                )
                self._start_job(job)

    def _start_job(self, job: DispatchJob) -> None:
        self._running_by_agent[job.target_agent_id] = (
            self._running_by_agent.get(job.target_agent_id, 0) + 1
        )
        task = asyncio.create_task(self._execute(job))
        self._tasks[job.job_id] = task

        def cleanup(completed: asyncio.Task[None]) -> None:
            self._tasks.pop(job.job_id, None)
            remaining = self._running_by_agent.get(job.target_agent_id, 1) - 1
            if remaining:
                self._running_by_agent[job.target_agent_id] = remaining
            else:
                self._running_by_agent.pop(job.target_agent_id, None)
            self._wake.set()

        task.add_done_callback(cleanup)

    async def _execute(self, job: DispatchJob) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(job.job_id))
        try:
            current = self.context.dispatch_repository.get_job(job.job_id)
            if current is None or current.status != "running":
                return
            agent_def = self.context.agent_loader.load(job.target_agent_id)
            agent = Agent(agent_def, self.context)
            try:
                session = agent.resume_session(job.child_session_id)
            except ValueError:
                session = agent.new_session(
                    AgentEventSource(agent_id=job.parent_agent_id),
                    session_id=job.child_session_id,
                )
            request = job.request
            message = request["task"]
            if request.get("context"):
                message += f"\n\nContext:\n{request['context']}"
            deadline = datetime.fromisoformat(job.execution_deadline_at)
            timeout = max(
                0.0, (deadline - datetime.now(deadline.tzinfo)).total_seconds()
            )
            semaphore = self.context.agent_semaphore(
                agent_def.id, agent_def.max_concurrency
            )
            acquired = False
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
                acquired = True
                timeout = max(
                    0.0,
                    (deadline - datetime.now(deadline.tzinfo)).total_seconds(),
                )
                response = await asyncio.wait_for(
                    session.chat(message), timeout=timeout
                )
            finally:
                if acquired:
                    semaphore.release()
            result = self._bounded_result(response)
            if not self.context.dispatch_repository.finish_success(
                job.job_id, self.worker_id, result
            ):
                current = self.context.dispatch_repository.get_job(job.job_id)
                if current and current.status == "cancel_requested":
                    self.context.dispatch_repository.finish_cancelled(
                        job.job_id, self.worker_id
                    )
            else:
                self.logger.info(
                    "dispatch terminal job_id=%s status=succeeded", job.job_id
                )
        except asyncio.TimeoutError:
            self.context.dispatch_repository.finish_failure(
                job.job_id,
                self.worker_id,
                "execution_timeout",
                "subagent execution deadline elapsed",
                timed_out=True,
            )
        except asyncio.CancelledError:
            current = self.context.dispatch_repository.get_job(job.job_id)
            if current and current.status == "cancel_requested":
                self.context.dispatch_repository.finish_cancelled(
                    job.job_id, self.worker_id
                )
                return
            raise
        except Exception as exc:
            self.context.dispatch_repository.finish_failure(
                job.job_id,
                self.worker_id,
                "subagent_error",
                str(exc),
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    def _bounded_result(self, response: str) -> str:
        limit = self.context.config.dispatch.max_result_bytes
        encoded = response.encode("utf-8")
        if len(encoded) <= limit:
            payload = {"content": response}
        else:
            truncated = encoded[: max(0, limit - 128)].decode("utf-8", errors="ignore")
            payload = {
                "content": truncated,
                "truncated": True,
                "original_bytes": len(encoded),
            }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > limit and not payload.get("truncated"):
            payload["truncated"] = True
            payload["original_bytes"] = len(encoded)
        while len(serialized.encode("utf-8")) > limit and payload["content"]:
            payload["content"] = str(payload["content"])[:-64]
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return serialized

    async def _heartbeat(self, job_id: str) -> None:
        interval = max(0.05, self.context.config.dispatch.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = self.context.dispatch_repository.renew_lease(
                job_id, self.worker_id, self.context.config.dispatch.lease_seconds
            )
            if not renewed:
                return

    async def _recover_expired(self) -> None:
        for job in self.context.dispatch_repository.expired_running_jobs():
            retry_safe = False
            try:
                retry_safe = self.context.agent_loader.load(
                    job.target_agent_id
                ).retry_safe
            except (DefNotFoundError, InvalidDefError):
                pass
            recovered = self.context.dispatch_repository.recover_expired(
                job.job_id, retry_safe=retry_safe
            )
            if recovered:
                self.logger.info(
                    "dispatch recovery job_id=%s action=%s",
                    job.job_id,
                    "requeued" if retry_safe else "terminal",
                )


class CompletionOutboxWorker(Worker):
    """Publishes durable completion records as best-effort EventBus events."""

    async def run(self) -> None:
        self.logger.info("CompletionOutboxWorker started")
        while True:
            try:
                await self._publish_pending()
            except Exception:
                self.logger.exception("completion outbox scan failed")
            await asyncio.sleep(self.context.config.dispatch.scan_interval_seconds)

    async def _publish_pending(self) -> None:
        config = self.context.config.dispatch
        for record in self.context.dispatch_repository.pending_outbox():
            if record.attempt_count >= config.outbox_max_attempts:
                continue
            payload = record.payload
            await self.context.eventbus.publish(
                JobCompletedEvent(
                    session_id=payload["parent_session_id"],
                    source=AgentEventSource(payload["target_agent_id"]),
                    content="",
                    event_id=record.event_id,
                    job_id=record.job_id,
                    child_session_id=payload["child_session_id"],
                    status=payload["status"],
                    result_ref=f"dispatch-job:{record.job_id}",
                )
            )
            delay = min(60.0, 2 ** min(record.attempt_count, 6))
            self.context.dispatch_repository.mark_outbox_failed(record.event_id, delay)


class CompletionRouter(SubscriberWorker):
    """Routes terminal jobs to poll, direct notify, or a new parent turn."""

    CONSUMER = "completion-router"

    def __init__(self, context: "SharedContext") -> None:
        super().__init__(context)
        context.eventbus.subscribe(JobCompletedEvent, self.handle_event)

    async def handle_event(self, event: JobCompletedEvent) -> None:
        repository = self.context.dispatch_repository
        if repository.was_consumed(self.CONSUMER, event.event_id):
            repository.mark_outbox_published(event.event_id)
            return
        job = repository.get_job(event.job_id)
        if job is None or not job.is_terminal:
            return

        if job.completion_mode == "notify":
            await self.context.eventbus.publish(
                OutboundEvent(
                    session_id=job.parent_session_id,
                    source=AgentEventSource(job.target_agent_id),
                    content=self._notification(job),
                    event_id=event.event_id,
                )
            )
        elif job.completion_mode == "resume_parent":
            await self._resume_parent(job, event)

        repository.consume_once(self.CONSUMER, event.event_id)
        repository.mark_outbox_published(event.event_id)
        self.logger.info(
            "dispatch completion routed event_id=%s job_id=%s mode=%s",
            event.event_id,
            event.job_id,
            job.completion_mode,
        )

    def _notification(self, job: DispatchJob) -> str:
        if job.status == "succeeded":
            result = job.result or {}
            content = str(result.get("content", ""))
            suffix = "\n\n[Result truncated]" if result.get("truncated") else ""
            return f"Subagent job {job.job_id} completed.\n\n{content}{suffix}"
        return (
            f"Subagent job {job.job_id} ended with status {job.status}: "
            f"{job.error_message or job.error_code or job.status}"
        )

    async def _resume_parent(self, job: DispatchJob, event: JobCompletedEvent) -> None:
        agent_def = self.context.agent_loader.load(job.parent_agent_id)
        gate = self.context.agent_semaphore(agent_def.id, agent_def.max_concurrency)
        async with gate:
            async with self.context.session_lock(job.parent_session_id):
                session_info = self.context.history_store.get_session_info(
                    job.parent_session_id
                )
                if session_info is None:
                    raise ValueError(
                        f"parent session not found: {job.parent_session_id}"
                    )
                session = Agent(agent_def, self.context).resume_session(
                    job.parent_session_id
                )
                tool_result = json.dumps(
                    self.context.dispatch_service.job_result(job), ensure_ascii=False
                )
                response = await session.resume_with_dispatch_result(
                    event_id=event.event_id,
                    job_id=job.job_id,
                    result=tool_result,
                )
        await self.context.eventbus.publish(
            OutboundEvent(
                session_id=job.parent_session_id,
                source=AgentEventSource(job.parent_agent_id),
                content=response,
                event_id=event.event_id,
            )
        )
