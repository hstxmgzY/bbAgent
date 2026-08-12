"""Deterministic claim extraction, citation binding, and quality scoring."""

from __future__ import annotations

import re

from research_assistant.citations import CITATION_RE
from research_assistant.embeddings import tokenize
from research_assistant.models import (
    AtomicClaim,
    Chunk,
    CitationEvaluation,
    ClaimAssessment,
    ClaimVerdict,
    ResearchQuality,
)
from research_assistant.ports import ClaimVerifier


CLAIM_SPLIT_RE = re.compile(r"(?:\r?\n)+|(?<=[。！？!?])\s*")
ATOMIC_SPLIT_RE = re.compile(r"[；;]|(?:，|,)\s*(?:并且?|而且|同时)")
NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?%?")
NON_VERIFIABLE_PREFIXES = (
    "#",
    "注意",
    "说明",
    "下面是",
    "当前没有",
    "所以我",
    "建议",
    "总结",
)


def extract_atomic_claims(answer: str) -> list[AtomicClaim]:
    claims: list[AtomicClaim] = []
    last_group_start = 0
    for raw_part in CLAIM_SPLIT_RE.split(answer):
        part = raw_part.strip()
        if not part:
            continue
        part = re.sub(r"^[-*]\s+", "", part).strip()
        citation_ids = list(dict.fromkeys(CITATION_RE.findall(part)))
        text = CITATION_RE.sub("", part).strip(" \t.-")
        if not text:
            if citation_ids and claims:
                for claim in claims[last_group_start:]:
                    claim.citation_ids = list(
                        dict.fromkeys([*claim.citation_ids, *citation_ids])
                    )
            continue
        last_group_start = len(claims)
        atomic_parts = [
            item.strip() for item in ATOMIC_SPLIT_RE.split(text) if item.strip()
        ]
        group_non_verifiable = text.startswith(NON_VERIFIABLE_PREFIXES)
        for atomic_text in atomic_parts:
            verifiable = not group_non_verifiable and not atomic_text.startswith(
                NON_VERIFIABLE_PREFIXES
            )
            importance = 2 if _is_important(atomic_text) else 1
            claims.append(
                AtomicClaim(
                    claim_id=f"C{len(claims) + 1}",
                    text=atomic_text,
                    citation_ids=citation_ids.copy(),
                    importance=importance,
                    verifiable=verifiable,
                )
            )
    return claims


class RuleBasedClaimVerifier:
    version = "rules-v1"

    async def verify(
        self, claim: AtomicClaim, evidence: list[Chunk]
    ) -> tuple[str, float, list[str]]:
        if not evidence:
            return ClaimVerdict.NOT_ENOUGH_INFORMATION.value, 1.0, []
        claim_text = _normalize_text(claim.text)
        claim_tokens = set(tokenize(claim.text))
        claim_numbers = set(NUMBER_RE.findall(claim.text))
        best_overlap = 0.0
        best_chunk: Chunk | None = None
        evidence_numbers: set[str] = set()
        for chunk in evidence:
            evidence_text = _normalize_text(chunk.text)
            if claim_text and claim_text in evidence_text:
                return ClaimVerdict.ENTAILED.value, 0.99, [chunk.chunk_id]
            chunk_tokens = set(tokenize(chunk.text))
            overlap = (
                len(claim_tokens & chunk_tokens) / len(claim_tokens)
                if claim_tokens
                else 0.0
            )
            evidence_numbers.update(NUMBER_RE.findall(chunk.text))
            if overlap > best_overlap:
                best_overlap = overlap
                best_chunk = chunk

        chunk_ids = [best_chunk.chunk_id] if best_chunk else []
        if claim_numbers and not claim_numbers.issubset(evidence_numbers):
            if evidence_numbers and best_overlap >= 0.3:
                return ClaimVerdict.CONTRADICTED.value, 0.85, chunk_ids
            return ClaimVerdict.NOT_ENOUGH_INFORMATION.value, 0.85, chunk_ids
        if best_overlap >= 0.62:
            return ClaimVerdict.ENTAILED.value, min(0.98, best_overlap), chunk_ids
        if best_overlap >= 0.3:
            return ClaimVerdict.PARTIAL.value, best_overlap, chunk_ids
        return ClaimVerdict.NOT_ENOUGH_INFORMATION.value, 1.0 - best_overlap, chunk_ids


class CitationEvaluator:
    def __init__(self, verifier: ClaimVerifier | None = None) -> None:
        self.verifier = verifier or RuleBasedClaimVerifier()

    @property
    def version(self) -> str:
        return self.verifier.version

    async def evaluate(
        self,
        answer: str,
        evidence: list[Chunk],
        valid_source_ids: set[str],
    ) -> CitationEvaluation:
        claims = extract_atomic_claims(answer)
        unknown = {
            citation_id
            for claim in claims
            for citation_id in claim.citation_ids
            if citation_id not in valid_source_ids
        }
        assessments: list[ClaimAssessment] = []
        for claim in claims:
            if not claim.verifiable:
                assessments.append(
                    ClaimAssessment(
                        claim=claim,
                        verdict=ClaimVerdict.NON_VERIFIABLE,
                        confidence=1.0,
                        verifier_version=self.version,
                    )
                )
                continue
            legal_citations = [
                source_id
                for source_id in claim.citation_ids
                if source_id in valid_source_ids
            ]
            if not legal_citations or len(legal_citations) != len(claim.citation_ids):
                assessments.append(
                    ClaimAssessment(
                        claim=claim,
                        verdict=ClaimVerdict.NOT_ENOUGH_INFORMATION,
                        confidence=1.0,
                        verifier_version=self.version,
                    )
                )
                continue
            bound_evidence = [
                chunk for chunk in evidence if chunk.source_id in legal_citations
            ]
            verdict, confidence, chunk_ids = await self.verifier.verify(
                claim, bound_evidence
            )
            assessments.append(
                ClaimAssessment(
                    claim=claim,
                    verdict=ClaimVerdict(verdict),
                    confidence=max(0.0, min(1.0, confidence)),
                    evidence_chunk_ids=chunk_ids,
                    verifier_version=self.version,
                )
            )
        quality = _score(assessments, unknown)
        return CitationEvaluation(
            assessments=assessments,
            quality=quality,
            unknown_citation_ids=unknown,
        )


def _score(
    assessments: list[ClaimAssessment], unknown_ids: set[str]
) -> ResearchQuality:
    verifiable = [item for item in assessments if item.claim.verifiable]
    total_weight = sum(item.claim.importance for item in verifiable)
    legally_cited = [
        item
        for item in verifiable
        if item.claim.citation_ids
        and not any(source_id in unknown_ids for source_id in item.claim.citation_ids)
    ]
    cited_weight = sum(item.claim.importance for item in legally_cited)
    supported_weight = sum(
        item.claim.importance * (1.0 if item.verdict == ClaimVerdict.ENTAILED else 0.5)
        for item in legally_cited
        if item.verdict in {ClaimVerdict.ENTAILED, ClaimVerdict.PARTIAL}
    )
    contradicted_weight = sum(
        item.claim.importance
        for item in verifiable
        if item.verdict == ClaimVerdict.CONTRADICTED
    )
    all_citations = {
        source_id for item in assessments for source_id in item.claim.citation_ids
    }
    supported_citations = {
        source_id
        for item in legally_cited
        if item.verdict == ClaimVerdict.ENTAILED
        for source_id in item.claim.citation_ids
    }
    unsupported = [
        item.claim.text for item in verifiable if item.verdict != ClaimVerdict.ENTAILED
    ]
    return ResearchQuality(
        claim_coverage=(cited_weight / total_weight if total_weight else 1.0),
        claim_support_rate=(supported_weight / cited_weight if cited_weight else 0.0),
        citation_precision=(
            len(supported_citations) / len(all_citations) if all_citations else 0.0
        ),
        contradiction_rate=(
            contradicted_weight / total_weight if total_weight else 0.0
        ),
        evaluated_claims=len(verifiable),
        unsupported_claims=unsupported,
        evaluator_version=(
            assessments[0].verifier_version if assessments else "rules-v1"
        ),
    )


def _is_important(text: str) -> bool:
    lowered = text.casefold()
    return bool(NUMBER_RE.search(text)) or any(
        marker in lowered
        for marker in (
            "because",
            "causes",
            "higher",
            "lower",
            "提升",
            "降低",
            "导致",
            "由于",
        )
    )


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold().strip("。.!！?")
