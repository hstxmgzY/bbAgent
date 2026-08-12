import asyncio

from research_assistant.models import ResearchRequest, SearchResult, ToolResult
from research_assistant.repositories.sqlite import SqliteMemoryRepository
from research_assistant.service import ResearchService


class CountingSearch:
    def __init__(self):
        self.calls = 0

    async def search(self, query, limit=5):
        self.calls += 1
        return ToolResult(
            True,
            [SearchResult("Doc", "https://example.com/rag", "RAG evidence")],
        )


class CountingReader:
    def __init__(self):
        self.calls = 0

    async def read(self, url):
        self.calls += 1
        return ToolResult(
            True,
            {
                "url": url,
                "title": "Doc",
                "text": "RAG uses retrieved evidence and citations. " * 40,
            },
        )


class NoCitationSynthesizer:
    async def synthesize(self, topic, evidence, warnings):
        return "uncited answer"


def test_persistent_freshness_cache_and_refresh_path(tmp_path):
    search = CountingSearch()
    reader = CountingReader()
    service = ResearchService(
        search,
        reader,
        NoCitationSynthesizer(),
        SqliteMemoryRepository(tmp_path / "research.db"),
    )
    request = ResearchRequest(topic="RAG evidence", max_queries=1)

    first = asyncio.run(service.research(request))
    second = asyncio.run(service.research(request))
    refreshed = asyncio.run(
        service.research(
            ResearchRequest(topic="RAG evidence", max_queries=1, refresh=True)
        )
    )

    assert first.status == second.status == refreshed.status == "complete"
    assert search.calls == 3
    assert reader.calls == 2
    assert first.sources[0].document_version_id == second.sources[0].document_version_id
    assert first.run_id and second.run_id and first.run_id != second.run_id
