"""Command line interface."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from research_assistant.assistant import ResearchAssistant


app = typer.Typer(help="Stage 2 research assistant")
console = Console()


@app.command()
def research(
    topic: str = typer.Argument(..., help="Research topic"),
    max_queries: int = typer.Option(3, help="Number of search queries"),
    max_sources: int = typer.Option(6, help="Maximum readable sources"),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Save markdown report"
    ),
) -> None:
    """Search, retrieve, summarize, and cite sources."""
    import asyncio

    assistant = ResearchAssistant()
    report = asyncio.run(
        assistant.research(topic, max_queries=max_queries, max_sources=max_sources)
    )
    markdown = report.to_markdown()
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
        console.print(f"[green]Saved report to {output}[/green]")
    console.print(markdown)


if __name__ == "__main__":
    app()
