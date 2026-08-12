"""ResearchService to mybot tool adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mybot.tools.base import BaseTool, tool
from research_assistant.models import ResearchRequest

if TYPE_CHECKING:
    from mybot.core.agent import AgentSession
    from mybot.core.context import SharedContext


def create_research_tool(context: "SharedContext") -> BaseTool:
    @tool(
        name="research",
        description=(
            "Research a topic using retrieved web sources and return a structured "
            "JSON report with answer, evidence, sources, warnings, status, and report_id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic to research"},
                "max_sources": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 6,
                },
                "refresh": {"type": "boolean", "default": False},
            },
            "required": ["topic"],
        },
    )
    async def research(
        topic: str,
        session: "AgentSession",
        max_sources: int = 6,
        refresh: bool = False,
    ) -> str:
        configured_top_k = getattr(
            getattr(getattr(context, "config", None), "research", None),
            "retrieval",
            None,
        )
        report = await context.research_service.research(
            ResearchRequest(
                topic=topic,
                max_sources=max_sources,
                top_k=getattr(configured_top_k, "top_k", 8),
                refresh=refresh,
                session_id=session.session_id,
            )
        )
        return report.to_json()

    return research
