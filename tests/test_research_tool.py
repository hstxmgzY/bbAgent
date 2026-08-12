import asyncio
import json
from types import SimpleNamespace

from mybot.tools.registry import ToolRegistry
from mybot.tools.research_tool import create_research_tool
from research_assistant.models import ResearchReport, SourceDocument


class FakeResearchService:
    async def research(self, request):
        source = SourceDocument("S1", "Doc", "https://example.com", "evidence")
        return ResearchReport(request.topic, "Answer [S1]", [source])


def test_research_tool_returns_structured_json():
    context = SimpleNamespace(research_service=FakeResearchService())
    session = SimpleNamespace(session_id="parent-session")
    registry = ToolRegistry()
    registry.register(create_research_tool(context))

    raw = asyncio.run(
        registry.execute_tool(
            "research", session=session, topic="RAG", max_sources=4, refresh=True
        )
    )
    result = json.loads(raw)

    assert result["topic"] == "RAG"
    assert result["answer"] == "Answer [S1]"
    assert result["sources"][0]["source_id"] == "S1"
    assert result["report_id"]
