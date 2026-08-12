import asyncio

from research_assistant.memory import MemoryStore
from research_assistant.models import ResearchRequest, SearchResult, ToolResult
from research_assistant.service import ResearchService


class FakeSearch:
    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query: str, limit: int = 5) -> ToolResult:
        self.queries.append(query)
        return ToolResult(
            ok=True,
            data=[
                SearchResult(
                    title="RAG",
                    url="https://example.com/rag",
                    snippet="检索增强生成需要引用可靠证据。",
                )
            ],
        )


class FakeReader:
    async def read(self, url: str) -> ToolResult:
        return ToolResult(
            ok=True,
            data={
                "url": url,
                "title": "RAG",
                "text": "检索增强生成需要引用可靠证据。" * 30,
            },
        )


class FakeSynthesizer:
    def __init__(self, answer: str = "检索增强生成应依据证据。[S1]"):
        self.answer = answer
        self.calls = 0

    async def synthesize(self, topic, evidence, warnings) -> str:
        self.calls += 1
        return self.answer


def make_service(tmp_path, answer: str = "检索增强生成应依据证据。[S1]"):
    search = FakeSearch()
    synthesizer = FakeSynthesizer(answer)
    service = ResearchService(
        search=search,
        reader=FakeReader(),
        synthesizer=synthesizer,
        repository=MemoryStore(tmp_path / "memory.json"),
    )
    return service, search, synthesizer


def test_repeated_research_still_executes_search(tmp_path):
    service, search, _ = make_service(tmp_path)
    request = ResearchRequest(topic="检索增强生成", max_queries=3)

    first = asyncio.run(service.research(request))
    second = asyncio.run(service.research(request))

    assert first.status == second.status == "complete"
    assert len(search.queries) == 6
    assert search.queries[:3] == search.queries[3:]


def test_unknown_citation_is_rejected_and_replaced(tmp_path):
    service, _, _ = make_service(tmp_path, answer="不受支持的说法。[S99]")

    report = asyncio.run(
        service.research(ResearchRequest(topic="检索增强生成", max_queries=1))
    )

    assert "[S99]" not in report.answer
    assert "[S1]" in report.answer
    assert any("rejected invalid citations" in warning for warning in report.warnings)


def test_answer_without_citations_uses_cited_fallback(tmp_path):
    service, _, _ = make_service(tmp_path, answer="没有引用的说法。")

    report = asyncio.run(
        service.research(ResearchRequest(topic="检索增强生成", max_queries=1))
    )

    assert "[S1]" in report.answer
    assert any("contained no citations" in warning for warning in report.warnings)
