from typing import Any, TYPE_CHECKING

from mybot.core.agent_loader import AgentLoader
from mybot.core.commands.registry import CommandRegistry
from mybot.core.cron_loader import CronLoader
from mybot.core.history import HistoryStore
from mybot.core.prompt_builder import PromptBuilder
from mybot.core.routing import RoutingTable
from mybot.core.skill_loader import SkillLoader
from mybot.core.eventbus import EventBus
from mybot.channel.base import Channel
from mybot.utils.config import Config
from mybot.provider.research import create_research_service
from research_assistant.service import ResearchService

if TYPE_CHECKING:
    from mybot.server.websocket_worker import WebSocketWorker


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

        if channels is not None:
            self.channels = channels
        else:
            self.channels = Channel.from_config(config)

        self.eventbus = EventBus(self)
        self.websocket_worker = None
