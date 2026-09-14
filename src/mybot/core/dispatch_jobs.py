"""Persistent job storage and domain rules for subagent dispatch."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mybot.core.events import AgentEventSource
from mybot.utils.def_loader import DefNotFoundError, InvalidDefError

if TYPE_CHECKING:
    from mybot.core.context import SharedContext


JobStatus = Literal[
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
]
CompletionMode = Literal["poll", "notify", "resume_parent"]
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class DispatchJob:
    job_id: str
    idempotency_key: str
    parent_session_id: str
    parent_agent_id: str
    target_agent_id: str
    child_session_id: str
    status: JobStatus
    completion_mode: CompletionMode
    request_json: str
    result_json: str | None
    error_code: str | None
    error_message: str | None
    execution_deadline_at: str
    lease_owner: str | None
    lease_expires_at: str | None
    attempt_count: int
    version: int
    created_at: str
    started_at: str | None
    completed_at: str | None

    @property
    def request(self) -> dict[str, Any]:
        return json.loads(self.request_json)

    @property
    def result(self) -> dict[str, Any] | None:
        return json.loads(self.result_json) if self.result_json else None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class DispatchOutboxRecord:
    event_id: str
    job_id: str
    event_type: str
    payload_json: str
    created_at: str
    published_at: str | None
    attempt_count: int
    next_attempt_at: str

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)


class DispatchJobError(Exception):
    """A stable domain error returned by dispatch tools."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


class DispatchJobRepository:
    """SQLite repository with compare-and-set state transitions."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS dispatch_jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    parent_session_id TEXT NOT NULL,
                    parent_agent_id TEXT NOT NULL,
                    target_agent_id TEXT NOT NULL,
                    child_session_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued', 'running', 'cancel_requested', 'succeeded',
                        'failed', 'cancelled', 'timed_out'
                    )),
                    completion_mode TEXT NOT NULL CHECK(completion_mode IN (
                        'poll', 'notify', 'resume_parent'
                    )),
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    execution_deadline_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(parent_session_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_dispatch_jobs_status_created
                    ON dispatch_jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_dispatch_jobs_parent_created
                    ON dispatch_jobs(parent_session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_dispatch_jobs_target_status
                    ON dispatch_jobs(target_agent_id, status, created_at);

                CREATE TABLE IF NOT EXISTS dispatch_outbox (
                    event_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    UNIQUE(job_id, event_type),
                    FOREIGN KEY(job_id) REFERENCES dispatch_jobs(job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_dispatch_outbox_pending
                    ON dispatch_outbox(published_at, next_attempt_at, created_at);

                CREATE TABLE IF NOT EXISTS dispatch_consumers (
                    consumer TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    consumed_at TEXT NOT NULL,
                    PRIMARY KEY(consumer, event_id)
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _job(row: sqlite3.Row | None) -> DispatchJob | None:
        return DispatchJob(**dict(row)) if row else None

    @staticmethod
    def _outbox(row: sqlite3.Row | None) -> DispatchOutboxRecord | None:
        return DispatchOutboxRecord(**dict(row)) if row else None

    def create_job(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        parent_session_id: str,
        parent_agent_id: str,
        target_agent_id: str,
        child_session_id: str,
        completion_mode: CompletionMode,
        request_json: str,
        execution_deadline_at: str,
        max_pending_per_session: int,
        max_queued_per_agent: int,
    ) -> tuple[DispatchJob, bool]:
        now = to_iso(utc_now())
        with self._lock, self._conn:
            existing = self._conn.execute(
                """SELECT * FROM dispatch_jobs
                   WHERE parent_session_id = ? AND idempotency_key = ?""",
                (parent_session_id, idempotency_key),
            ).fetchone()
            if existing:
                return self._job(existing), False  # type: ignore[return-value]

            pending_count = self._conn.execute(
                """SELECT COUNT(*) FROM dispatch_jobs
                   WHERE parent_session_id = ?
                     AND status IN ('queued', 'running', 'cancel_requested')""",
                (parent_session_id,),
            ).fetchone()[0]
            if pending_count >= max_pending_per_session:
                raise DispatchJobError(
                    "session_capacity_exceeded",
                    "parent session has too many pending jobs",
                    retry_after_seconds=30,
                )

            queued_count = self._conn.execute(
                """SELECT COUNT(*) FROM dispatch_jobs
                   WHERE target_agent_id = ? AND status = 'queued'""",
                (target_agent_id,),
            ).fetchone()[0]
            if queued_count >= max_queued_per_agent:
                raise DispatchJobError(
                    "queue_capacity_exceeded",
                    "target agent queue is full",
                    retry_after_seconds=30,
                )

            cursor = self._conn.execute(
                """INSERT OR IGNORE INTO dispatch_jobs (
                    job_id, idempotency_key, parent_session_id, parent_agent_id,
                    target_agent_id, child_session_id, status, completion_mode,
                    request_json, execution_deadline_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                (
                    job_id,
                    idempotency_key,
                    parent_session_id,
                    parent_agent_id,
                    target_agent_id,
                    child_session_id,
                    completion_mode,
                    request_json,
                    execution_deadline_at,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                existing = self._conn.execute(
                    """SELECT * FROM dispatch_jobs
                       WHERE parent_session_id = ? AND idempotency_key = ?""",
                    (parent_session_id, idempotency_key),
                ).fetchone()
                if existing:
                    return self._job(existing), False  # type: ignore[return-value]
                raise DispatchJobError(
                    "job_conflict", "dispatch job could not be created"
                )
            row = self._conn.execute(
                "SELECT * FROM dispatch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return self._job(row), True  # type: ignore[return-value]

    def get_job(self, job_id: str) -> DispatchJob | None:
        with self._lock:
            return self._job(
                self._conn.execute(
                    "SELECT * FROM dispatch_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            )

    def list_jobs(self, parent_session_id: str, limit: int = 20) -> list[DispatchJob]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM dispatch_jobs WHERE parent_session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (parent_session_id, limit),
            ).fetchall()
            return [self._job(row) for row in rows]  # type: ignore[misc]

    def queued_targets(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT target_agent_id, MIN(created_at) AS first_created
                   FROM dispatch_jobs WHERE status = 'queued'
                   GROUP BY target_agent_id ORDER BY first_created"""
            ).fetchall()
            return [str(row["target_agent_id"]) for row in rows]

    def claim_next(
        self, target_agent_id: str, lease_owner: str, lease_seconds: float
    ) -> DispatchJob | None:
        now = utc_now()
        now_iso = to_iso(now)
        lease_expires = to_iso(now + timedelta(seconds=lease_seconds))
        with self._lock, self._conn:
            row = self._conn.execute(
                """SELECT job_id FROM dispatch_jobs
                   WHERE target_agent_id = ? AND status = 'queued'
                   ORDER BY created_at LIMIT 1""",
                (target_agent_id,),
            ).fetchone()
            if not row:
                return None
            cursor = self._conn.execute(
                """UPDATE dispatch_jobs
                   SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                       attempt_count = attempt_count + 1, version = version + 1,
                       started_at = COALESCE(started_at, ?)
                   WHERE job_id = ? AND status = 'queued'""",
                (lease_owner, lease_expires, now_iso, row["job_id"]),
            )
            if cursor.rowcount != 1:
                return None
            return self.get_job(str(row["job_id"]))

    def renew_lease(self, job_id: str, lease_owner: str, lease_seconds: float) -> bool:
        expires = to_iso(utc_now() + timedelta(seconds=lease_seconds))
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """UPDATE dispatch_jobs SET lease_expires_at = ?, version = version + 1
                   WHERE job_id = ? AND lease_owner = ?
                     AND status IN ('running', 'cancel_requested')""",
                (expires, job_id, lease_owner),
            )
            return cursor.rowcount == 1

    def request_cancel(self, job_id: str, parent_session_id: str) -> DispatchJob:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM dispatch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            job = self._job(row)
            if job is None:
                raise DispatchJobError("job_not_found", "dispatch job was not found")
            if job.parent_session_id != parent_session_id:
                raise DispatchJobError("job_not_found", "dispatch job was not found")
            if job.is_terminal:
                raise DispatchJobError(
                    "already_terminal",
                    "dispatch job is already terminal",
                    status=job.status,
                )
            if job.status == "queued":
                changed = self._finish_in_transaction(
                    job_id,
                    expected=("queued",),
                    status="cancelled",
                    error_code="cancelled",
                    error_message="cancelled before execution",
                )
                if not changed:
                    current = self.get_job(job_id)
                    raise DispatchJobError(
                        "already_terminal",
                        "dispatch job is already terminal",
                        status=current.status if current else "unknown",
                    )
            elif job.status == "running":
                cursor = self._conn.execute(
                    """UPDATE dispatch_jobs
                       SET status = 'cancel_requested', version = version + 1
                       WHERE job_id = ? AND status = 'running'""",
                    (job_id,),
                )
                if cursor.rowcount != 1:
                    current = self.get_job(job_id)
                    raise DispatchJobError(
                        "already_terminal",
                        "dispatch job is already terminal",
                        status=current.status if current else "unknown",
                    )
            return self.get_job(job_id)  # type: ignore[return-value]

    def finish_success(self, job_id: str, lease_owner: str, result_json: str) -> bool:
        return self._finish(
            job_id,
            expected=("running",),
            status="succeeded",
            lease_owner=lease_owner,
            result_json=result_json,
        )

    def finish_failure(
        self,
        job_id: str,
        lease_owner: str,
        error_code: str,
        error_message: str,
        *,
        timed_out: bool = False,
    ) -> bool:
        return self._finish(
            job_id,
            expected=("running",),
            status="timed_out" if timed_out else "failed",
            lease_owner=lease_owner,
            error_code=error_code,
            error_message=error_message,
        )

    def finish_cancelled(self, job_id: str, lease_owner: str) -> bool:
        return self._finish(
            job_id,
            expected=("cancel_requested",),
            status="cancelled",
            lease_owner=lease_owner,
            error_code="cancelled",
            error_message="dispatch job was cancelled",
        )

    def _finish(
        self,
        job_id: str,
        *,
        expected: tuple[str, ...],
        status: JobStatus,
        lease_owner: str | None = None,
        result_json: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        with self._lock, self._conn:
            return self._finish_in_transaction(
                job_id,
                expected=expected,
                status=status,
                result_json=result_json,
                error_code=error_code,
                error_message=error_message,
                lease_owner=lease_owner,
            )

    def _finish_in_transaction(
        self,
        job_id: str,
        *,
        expected: tuple[str, ...],
        status: JobStatus,
        result_json: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        lease_owner: str | None = None,
    ) -> bool:
        placeholders = ",".join("?" for _ in expected)
        completed_at = to_iso(utc_now())
        owner_clause = " AND lease_owner = ?" if lease_owner is not None else ""
        parameters: tuple[Any, ...] = (
            status,
            result_json,
            error_code,
            error_message,
            completed_at,
            job_id,
            *expected,
        )
        if lease_owner is not None:
            parameters += (lease_owner,)
        cursor = self._conn.execute(
            f"""UPDATE dispatch_jobs
                SET status = ?, result_json = ?, error_code = ?, error_message = ?,
                    completed_at = ?, lease_owner = NULL, lease_expires_at = NULL,
                    version = version + 1
                WHERE job_id = ? AND status IN ({placeholders}){owner_clause}""",
            parameters,
        )
        if cursor.rowcount != 1:
            return False
        job = self.get_job(job_id)
        assert job is not None
        event_id = str(uuid.uuid4())
        payload = json.dumps(
            {
                "event_id": event_id,
                "job_id": job.job_id,
                "parent_session_id": job.parent_session_id,
                "child_session_id": job.child_session_id,
                "target_agent_id": job.target_agent_id,
                "status": job.status,
            },
            separators=(",", ":"),
        )
        self._conn.execute(
            """INSERT OR IGNORE INTO dispatch_outbox (
                event_id, job_id, event_type, payload_json, created_at, next_attempt_at
            ) VALUES (?, ?, 'job.completed', ?, ?, ?)""",
            (event_id, job_id, payload, completed_at, completed_at),
        )
        return True

    def expired_running_jobs(self) -> list[DispatchJob]:
        now = to_iso(utc_now())
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM dispatch_jobs
                   WHERE status IN ('running', 'cancel_requested')
                     AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?""",
                (now,),
            ).fetchall()
            return [self._job(row) for row in rows]  # type: ignore[misc]

    def recover_expired(self, job_id: str, retry_safe: bool) -> bool:
        with self._lock, self._conn:
            job = self.get_job(job_id)
            if not job or job.status not in {"running", "cancel_requested"}:
                return False
            if not job.lease_expires_at or job.lease_expires_at > to_iso(utc_now()):
                return False
            if job.status == "cancel_requested":
                return self._finish_in_transaction(
                    job_id,
                    expected=("cancel_requested",),
                    status="cancelled",
                    error_code="cancelled",
                    error_message="cancelled after worker lease expired",
                )
            if retry_safe:
                cursor = self._conn.execute(
                    """UPDATE dispatch_jobs
                       SET status = 'queued', lease_owner = NULL,
                           lease_expires_at = NULL, version = version + 1
                       WHERE job_id = ? AND status = 'running'""",
                    (job_id,),
                )
                return cursor.rowcount == 1
            return self._finish_in_transaction(
                job_id,
                expected=("running",),
                status="failed",
                error_code="worker_lost",
                error_message="worker lease expired; task was not replayed",
            )

    def pending_outbox(self, limit: int = 50) -> list[DispatchOutboxRecord]:
        now = to_iso(utc_now())
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM dispatch_outbox
                   WHERE published_at IS NULL AND next_attempt_at <= ?
                   ORDER BY created_at LIMIT ?""",
                (now, limit),
            ).fetchall()
            return [self._outbox(row) for row in rows]  # type: ignore[misc]

    def mark_outbox_published(self, event_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE dispatch_outbox SET published_at = ? WHERE event_id = ?",
                (to_iso(utc_now()), event_id),
            )

    def mark_outbox_failed(self, event_id: str, delay_seconds: float) -> None:
        next_attempt = to_iso(utc_now() + timedelta(seconds=delay_seconds))
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE dispatch_outbox
                   SET attempt_count = attempt_count + 1, next_attempt_at = ?
                   WHERE event_id = ? AND published_at IS NULL""",
                (next_attempt, event_id),
            )

    def consume_once(self, consumer: str, event_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """INSERT OR IGNORE INTO dispatch_consumers
                   (consumer, event_id, consumed_at) VALUES (?, ?, ?)""",
                (consumer, event_id, to_iso(utc_now())),
            )
            return cursor.rowcount == 1

    def was_consumed(self, consumer: str, event_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM dispatch_consumers
                   WHERE consumer = ? AND event_id = ?""",
                (consumer, event_id),
            ).fetchone()
            return row is not None

    def cleanup_terminal(self, retention_hours: int) -> int:
        cutoff = to_iso(utc_now() - timedelta(hours=retention_hours))
        with self._lock, self._conn:
            self._conn.execute(
                """DELETE FROM dispatch_consumers WHERE event_id IN (
                       SELECT event_id FROM dispatch_outbox
                       WHERE published_at IS NOT NULL AND job_id IN (
                           SELECT job_id FROM dispatch_jobs
                           WHERE completed_at IS NOT NULL AND completed_at < ?
                       )
                   )""",
                (cutoff,),
            )
            self._conn.execute(
                """DELETE FROM dispatch_outbox WHERE published_at IS NOT NULL
                   AND job_id IN (
                       SELECT job_id FROM dispatch_jobs
                       WHERE completed_at IS NOT NULL AND completed_at < ?
                   )""",
                (cutoff,),
            )
            cursor = self._conn.execute(
                """DELETE FROM dispatch_jobs
                   WHERE completed_at IS NOT NULL AND completed_at < ?
                     AND NOT EXISTS (
                         SELECT 1 FROM dispatch_outbox
                         WHERE dispatch_outbox.job_id = dispatch_jobs.job_id
                     )""",
                (cutoff,),
            )
            return cursor.rowcount


class DispatchJobService:
    """Authorization and stable tool-facing behavior for dispatch jobs."""

    def __init__(self, context: "SharedContext", repository: DispatchJobRepository):
        self.context = context
        self.repository = repository
        self._turn_snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        self._submitted_turns: set[tuple[str, str]] = set()
        self._last_poll_at: dict[tuple[str, str], float] = {}

    @staticmethod
    def idempotency_key(parent_session_id: str, tool_call_id: str) -> str:
        value = f"{parent_session_id}:{tool_call_id}".encode()
        return hashlib.sha256(value).hexdigest()

    def submit(
        self,
        *,
        parent_session_id: str,
        parent_agent_id: str,
        target_agent_id: str,
        task: str,
        context: str,
        completion_mode: CompletionMode,
        tool_call_id: str,
        turn_id: str,
    ) -> tuple[DispatchJob, bool]:
        try:
            parent_def = self.context.agent_loader.load(parent_agent_id)
            self.context.agent_loader.load(target_agent_id)
        except (DefNotFoundError, InvalidDefError) as exc:
            raise DispatchJobError("agent_not_found", str(exc)) from None
        if (
            target_agent_id == parent_agent_id
            or target_agent_id not in parent_def.dispatch_to
        ):
            raise DispatchJobError(
                "dispatch_not_authorized",
                f"agent '{target_agent_id}' is not authorized for dispatch",
            )
        if completion_mode not in parent_def.dispatch_completion_modes:
            raise DispatchJobError(
                "completion_mode_not_allowed",
                f"completion mode '{completion_mode}' is not allowed",
            )
        if not task.strip():
            raise DispatchJobError("invalid_request", "task must not be empty")

        config = self.context.config.dispatch
        job_id = str(uuid.uuid4())
        child_session_id = str(uuid.uuid4())
        request_json = json.dumps(
            {"task": task, "context": context},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(request_json.encode("utf-8")) > config.max_request_bytes:
            raise DispatchJobError("request_too_large", "dispatch request is too large")
        execution_seconds = parent_def.dispatch_execution_timeout_seconds
        deadline = to_iso(utc_now() + timedelta(seconds=execution_seconds))
        key = self.idempotency_key(parent_session_id, tool_call_id)
        job, created = self.repository.create_job(
            job_id=job_id,
            idempotency_key=key,
            parent_session_id=parent_session_id,
            parent_agent_id=parent_agent_id,
            target_agent_id=target_agent_id,
            child_session_id=child_session_id,
            completion_mode=completion_mode,
            request_json=request_json,
            execution_deadline_at=deadline,
            max_pending_per_session=config.max_pending_jobs_per_session,
            max_queued_per_agent=config.max_queued_jobs_per_agent,
        )
        if created:
            self.context.history_store.create_session(
                target_agent_id,
                child_session_id,
                AgentEventSource(agent_id=parent_agent_id),
            )
            logger.info(
                "dispatch submitted job_id=%s parent_session_id=%s target_agent=%s mode=%s",
                job.job_id,
                parent_session_id,
                target_agent_id,
                completion_mode,
            )
        self._submitted_turns.add((turn_id, job.job_id))
        if len(self._submitted_turns) > 4096:
            self._submitted_turns.pop()
        return job, created

    def status(
        self, job_id: str, parent_session_id: str, turn_id: str = ""
    ) -> dict[str, Any]:
        key = (turn_id, job_id)
        if turn_id and key in self._turn_snapshots:
            return self._turn_snapshots[key]
        job = self._owned_job(job_id, parent_session_id)
        poll_key = (parent_session_id, job_id)
        now = time.monotonic()
        last_poll = self._last_poll_at.get(poll_key)
        minimum = self.context.config.dispatch.poll_min_interval_seconds
        if not job.is_terminal and last_poll is not None and now - last_poll < minimum:
            raise DispatchJobError(
                "poll_rate_limited",
                "dispatch job was polled too recently",
                retry_after_seconds=max(0.0, minimum - (now - last_poll)),
            )
        self._last_poll_at[poll_key] = now
        result = self.job_result(job)
        if turn_id and key in self._submitted_turns:
            result = {**result, "deferred": True}
        if turn_id:
            self._turn_snapshots[key] = result
            if len(self._turn_snapshots) > 4096:
                self._turn_snapshots.pop(next(iter(self._turn_snapshots)))
        return result

    def cancel(self, job_id: str, parent_session_id: str) -> DispatchJob:
        return self.repository.request_cancel(job_id, parent_session_id)

    def list_jobs(self, parent_session_id: str, limit: int = 20) -> list[DispatchJob]:
        return self.repository.list_jobs(parent_session_id, limit)

    def _owned_job(self, job_id: str, parent_session_id: str) -> DispatchJob:
        job = self.repository.get_job(job_id)
        if not job or job.parent_session_id != parent_session_id:
            raise DispatchJobError("job_not_found", "dispatch job was not found")
        return job

    def job_result(self, job: DispatchJob) -> dict[str, Any]:
        response: dict[str, Any] = {
            "ok": job.status not in {"failed", "cancelled", "timed_out"},
            "job_id": job.job_id,
            "status": job.status,
            "completion_mode": job.completion_mode,
            "child_session_id": job.child_session_id,
            "submitted_at": job.created_at,
        }
        if job.started_at:
            response["started_at"] = job.started_at
        if job.completed_at:
            response["completed_at"] = job.completed_at
        if job.status == "succeeded":
            response["result"] = job.result
        elif job.status in {"failed", "cancelled", "timed_out"}:
            response["error"] = {
                "code": job.error_code or job.status,
                "message": job.error_message or job.status,
            }
        else:
            response["next_poll_after_seconds"] = (
                self.context.config.dispatch.poll_min_interval_seconds
            )
        return response
