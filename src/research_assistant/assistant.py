"""Backward-compatible facade for the injectable research service."""

from __future__ import annotations

from pathlib import Path

from research_assistant.llm import LiteLLMAnswerSynthesizer
from research_assistant.models import ResearchReport, ResearchRequest
from research_assistant.repositories.sqlite import SqliteMemoryRepository
from research_assistant.service import ResearchService
from research_assistant.tools import SearchTool, WebReadTool


class ResearchAssistant(ResearchService):
    def __init__(self, memory_path: Path | None = None):
        base = Path(__file__).resolve().parents[2]
        legacy_path = memory_path or base / "data" / "memory" / "memory.json"
        database_path = (
            legacy_path
            if legacy_path.suffix in {".db", ".sqlite", ".sqlite3"}
            else legacy_path.with_name("research.db")
        )
        memory = SqliteMemoryRepository(database_path)
        if legacy_path != database_path:
            memory.migrate_legacy_json(legacy_path)
        super().__init__(
            search=SearchTool(),
            reader=WebReadTool(),
            synthesizer=LiteLLMAnswerSynthesizer(),
            repository=memory,
        )
        self.memory = memory
        self.search_tool = self.search
        self.webread_tool = self.reader

    async def research(
        self,
        topic: str,
        max_queries: int = 3,
        max_sources: int = 6,
        top_k: int = 8,
        refresh: bool = False,
    ) -> ResearchReport:
        return await super().research(
            ResearchRequest(
                topic=topic,
                max_queries=max_queries,
                max_sources=max_sources,
                top_k=top_k,
                refresh=refresh,
            )
        )
