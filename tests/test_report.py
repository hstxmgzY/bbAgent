from research_assistant.models import Chunk, ResearchReport, SourceDocument


def test_report_contains_citations_and_sources():
    source = SourceDocument("S1", "Doc", "https://example.com", "hello")
    evidence = Chunk("S1-C1", "S1", "Doc", "https://example.com", "useful evidence")
    report = ResearchReport("topic", "Answer [S1]", [source], [evidence])

    markdown = report.to_markdown()

    assert "Answer [S1]" in markdown
    assert "[S1] Doc: https://example.com" in markdown
