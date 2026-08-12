import asyncio

from research_assistant.claims import CitationEvaluator
from research_assistant.models import Chunk, ClaimVerdict


def test_claim_evaluator_scores_coverage_support_and_unknown_citations():
    evaluator = CitationEvaluator()
    evidence = [
        Chunk(
            "C1",
            "S1",
            "Paper",
            "https://example.com",
            "A 方法在 2025 年发布，并在数据集 B 上提升了 12%。",
        )
    ]
    evaluation = asyncio.run(
        evaluator.evaluate(
            "A 方法在 2025 年发布，并在数据集 B 上提升了 12%。[S1]",
            evidence,
            {"S1"},
        )
    )

    assert evaluation.quality.claim_coverage == 1.0
    assert evaluation.quality.claim_support_rate == 1.0
    assert evaluation.quality.citation_precision == 1.0
    assert evaluation.quality.evaluated_claims == 2

    unknown = asyncio.run(evaluator.evaluate("未知说法。[S99]", evidence, {"S1"}))
    assert unknown.unknown_citation_ids == {"S99"}
    assert unknown.has_hard_failure


def test_numeric_mismatch_is_marked_contradicted():
    evaluator = CitationEvaluator()
    evidence = [
        Chunk(
            "C1",
            "S1",
            "Paper",
            "https://example.com",
            "模型在数据集 B 上提升了 10%。",
        )
    ]
    result = asyncio.run(
        evaluator.evaluate("模型在数据集 B 上提升了 12%。[S1]", evidence, {"S1"})
    )

    assert result.assessments[0].verdict == ClaimVerdict.CONTRADICTED
    assert result.quality.contradiction_rate == 1.0
