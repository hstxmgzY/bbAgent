"""Build bounded planning context from scoped experience memory."""

from __future__ import annotations

from research_assistant.models import MemoryHit, MemoryScope
from research_assistant.ports import MemoryRepository


class MemoryContextBuilder:
    def __init__(self, repository: MemoryRepository, max_related_runs: int = 5) -> None:
        self.repository = repository
        self.max_related_runs = max(0, max_related_runs)

    async def build(self, topic: str, scope: MemoryScope) -> list[MemoryHit]:
        if self.max_related_runs == 0:
            return []
        return await self.repository.find_related_runs(
            topic, scope, limit=self.max_related_runs
        )
