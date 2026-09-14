import asyncio
from typing import Any, TYPE_CHECKING

from mybot.core.agent_loader import AgentLoader
from mybot.core.commands.registry import CommandRegistry
from mybot.core.cron_loader import CronLoader
from mybot.core.history import HistoryStore
from mybot.core.prompt_builder import PromptBuilder
from mybot.core.routing import RoutingTable
from mybot.core.skill_loader import SkillLoader
from mybot.core.eventbus import EventBus
from mybot.core.dispatch_jobs import DispatchJobRepository, DispatchJobService
from mybot.channel.base import Channel
from mybot.utils.config import Config
from mybot.provider.research import create_research_service
from research_assistant.service import ResearchService

if TYPE_CHECKING:
    from mybot.server.websocket_worker import WebSocketWorker


class ReentrantAsyncLock:
    """Task-reentrant lock used to serialize complete session transactions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is not None and task is self._owner:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("session lock released by a non-owner task")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> "ReentrantAsyncLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


class ReentrantAsyncSemaphore:
    """Task-reentrant semaphore for a shared per-agent concurrency budget."""

    def __init__(self, value: int) -> None:
        self._semaphore = asyncio.Semaphore(value)
        self._depths: dict[asyncio.Task[Any], int] = {}

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("agent semaphore requires an asyncio task")
        if task in self._depths:
            self._depths[task] += 1
            return
        await self._semaphore.acquire()
        self._depths[task] = 1

    def release(self) -> None:
        task = asyncio.current_task()
        if task is None or task not in self._depths:
            raise RuntimeError("agent semaphore released by a non-owner task")
        self._depths[task] -= 1
        if self._depths[task] == 0:
            del self._depths[task]
            self._semaphore.release()

    async def __aenter__(self) -> "ReentrantAsyncSemaphore":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


class SharedContext:
    """Global shared state for the application."""

    config: Config
    history_store: HistoryStore
    agent_loader: AgentLoader
    skill_loader: SkillLoader
    cron_loader: CronLoader
    command_registry: CommandRegistry
    routing_table: RoutingTable
    prompt_builder: PromptBuilder
    channels: list[Channel[Any]]
    eventbus: EventBus
    websocket_worker: "WebSocketWorker | None"
    research_service: ResearchService
    dispatch_repository: DispatchJobRepository
    dispatch_service: DispatchJobService

    def __init__(
        self, config: Config, channels: list[Channel[Any]] | None = None
    ) -> None:
        self.config = config
        self.history_store = HistoryStore.from_config(config)
        self.agent_loader = AgentLoader.from_config(config)
        self.skill_loader = SkillLoader.from_config(config)
        self.cron_loader = CronLoader.from_config(config)
        self.command_registry = CommandRegistry.with_builtins()
        self.routing_table = RoutingTable(self)
        self.prompt_builder = PromptBuilder(self)
        self.research_service = create_research_service(
            config, config.memories_path / "research.json"
        )
        self.dispatch_repository = DispatchJobRepository(
            config.dispatch.path(config.workspace)
        )
        self.dispatch_service = DispatchJobService(self, self.dispatch_repository)

        if channels is not None:
            self.channels = channels
        else:
            self.channels = Channel.from_config(config)

        self.eventbus = EventBus(self)
        self.websocket_worker = None
        self._session_locks: dict[str, ReentrantAsyncLock] = {}
        self._agent_semaphores: dict[str, ReentrantAsyncSemaphore] = {}

    def session_lock(self, session_id: str) -> ReentrantAsyncLock:
        """Return the process-local serialization lock for a parent session."""
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = ReentrantAsyncLock()
            self._session_locks[session_id] = lock
        return lock

    def agent_semaphore(
        self, agent_id: str, max_concurrency: int
    ) -> ReentrantAsyncSemaphore:
        """Share an agent's concurrency budget across every execution worker."""
        semaphore = self._agent_semaphores.get(agent_id)
        if semaphore is None:
            semaphore = ReentrantAsyncSemaphore(max_concurrency)
            self._agent_semaphores[agent_id] = semaphore
        return semaphore
