"""Shared domain models for the research workflow."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass
class ToolResult:
    ok: bool
    data: object | None = None
    error: str | None = None


@dataclass
class SourceDocument:
    source_id: str
    title: str
    url: str
    text: str
    document_version_id: str | None = None
    persistent_source_id: str | None = None
    content_hash: str | None = None
    fetched_at: datetime | None = None
    expires_at: datetime | None = None
    language: str = "unknown"
    scope_partition: str = "global"


@dataclass
class DocumentVersion:
    document_version_id: str
    source_id: str
    canonical_url: str
    title: str
    text: str
    content_hash: str
    fetched_at: datetime
    expires_at: datetime
    language: str = "unknown"
    parser_version: str = "webread-v1"
    scope_partition: str = "global"
    http_etag: str | None = None
    http_last_modified: str | None = None

    def to_source_document(self, citation_id: str | None = None) -> SourceDocument:
        return SourceDocument(
            source_id=citation_id or self.source_id,
            title=self.title,
            url=self.canonical_url,
            text=self.text,
            document_version_id=self.document_version_id,
            persistent_source_id=self.source_id,
            content_hash=self.content_hash,
            fetched_at=self.fetched_at,
            expires_at=self.expires_at,
            language=self.language,
            scope_partition=self.scope_partition,
        )


@dataclass
class Chunk:
    chunk_id: str
    source_id: str
    title: str
    url: str
    text: str
    score: float = 0.0
    document_version_id: str | None = None
    chunk_index: int = 0
    content_hash: str | None = None
    fetched_at: datetime | None = None
    expires_at: datetime | None = None
    language: str = "unknown"
    scope_partition: str = "global"
    embedding_model: str | None = None
    chunker_version: str = "token-window-v1"


@dataclass
class RetrievedChunk(Chunk):
    """A chunk returned by an EvidenceIndex."""


@dataclass
class IndexedChunk:
    chunk: Chunk
    vector: list[float]
    document_version_id: str
    persistent_source_id: str
    embedding_model: str
    scope_partition: str
    fetched_at: datetime
    expires_at: datetime
    content_hash: str
    chunker_version: str = "token-window-v1"


@dataclass(frozen=True)
class MemoryScope:
    session_id: str | None = None
    user_scope: str | None = None

    @property
    def run_partition(self) -> str:
        if self.session_id:
            return f"session:{self.session_id}"
        if self.user_scope:
            return f"user:{self.user_scope}"
        return "global"

    @property
    def visible_partitions(self) -> tuple[str, ...]:
        partitions = ["global"]
        if self.user_scope:
            partitions.append(f"user:{self.user_scope}")
        if self.session_id:
            partitions.append(f"session:{self.session_id}")
        return tuple(partitions)


@dataclass(frozen=True)
class ResearchRequest:
    topic: str
    max_queries: int = 3
    max_sources: int = 6
    top_k: int = 8
    refresh: bool = False
    session_id: str | None = None
    user_scope: str | None = None

    @property
    def memory_scope(self) -> MemoryScope:
        return MemoryScope(session_id=self.session_id, user_scope=self.user_scope)


@dataclass
class ResearchRun:
    run_id: str
    topic: str
    scope_partition: str
    status: str = "running"
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None


@dataclass(frozen=True)
class ResearchQuery:
    query: str
    intent: str = "overview"
    derived_from: str = "current_topic"


@dataclass
class MemoryHit:
    run_id: str
    topic: str
    queries: list[ResearchQuery] = field(default_factory=list)
    score: float = 0.0


@dataclass(frozen=True)
class QueryRewriteRequest:
    topic: str
    max_queries: int
    related_runs: tuple[MemoryHit, ...] = ()


@dataclass(frozen=True)
class RetrievalRequest:
    query_vector: list[float]
    embedding_model: str
    scope: MemoryScope
    candidate_k: int = 30
    top_k: int = 8
    max_per_source: int = 2
    min_score: float | None = None
    now: datetime = field(default_factory=utc_now)
    chunker_version: str = "token-window-v1"


class ClaimVerdict(str, Enum):
    ENTAILED = "entailed"
    PARTIAL = "partial"
    CONTRADICTED = "contradicted"
    NOT_ENOUGH_INFORMATION = "not_enough_information"
    NON_VERIFIABLE = "non_verifiable"


@dataclass
class AtomicClaim:
    claim_id: str
    text: str
    citation_ids: list[str] = field(default_factory=list)
    importance: int = 1
    verifiable: bool = True


@dataclass
class ClaimAssessment:
    claim: AtomicClaim
    verdict: ClaimVerdict
    confidence: float
    evidence_chunk_ids: list[str] = field(default_factory=list)
    verifier_version: str = "rules-v1"


@dataclass
class ResearchQuality:
    claim_coverage: float = 0.0
    claim_support_rate: float = 0.0
    citation_precision: float = 0.0
    contradiction_rate: float = 0.0
    evaluated_claims: int = 0
    unsupported_claims: list[str] = field(default_factory=list)
    evaluator_version: str = "rules-v1"


@dataclass
class CitationEvaluation:
    assessments: list[ClaimAssessment]
    quality: ResearchQuality
    unknown_citation_ids: set[str] = field(default_factory=set)

    @property
    def has_hard_failure(self) -> bool:
        return bool(self.unknown_citation_ids) or any(
            item.verdict == ClaimVerdict.CONTRADICTED for item in self.assessments
        )


@dataclass
class ResearchReport:
    topic: str
    answer: str
    sources: list[SourceDocument]
    evidence: list[Chunk] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    report_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "complete"
    quality: ResearchQuality | None = None
    run_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    def to_markdown(self) -> str:
        lines = [f"# Research: {self.topic}", "", self.answer.strip(), ""]
        if self.quality:
            lines.extend(
                [
                    "## Quality",
                    "",
                    f"- Claim coverage: {self.quality.claim_coverage:.2f}",
                    f"- Claim support rate: {self.quality.claim_support_rate:.2f}",
                    f"- Citation precision: {self.quality.citation_precision:.2f}",
                    f"- Contradiction rate: {self.quality.contradiction_rate:.2f}",
                    "",
                ]
            )
        if self.evidence:
            lines.extend(["## Evidence", ""])
            for chunk in self.evidence:
                excerpt = " ".join(chunk.text.split())[:280]
                lines.append(f"- [{chunk.source_id}] {chunk.title}: {excerpt}...")
            lines.append("")
        if self.sources:
            lines.extend(["## Sources", ""])
            for src in self.sources:
                lines.append(f"- [{src.source_id}] {src.title}: {src.url}")
            lines.append("")
        if self.warnings:
            lines.extend(["## Warnings", ""])
            for warning in self.warnings:
                lines.append(f"- {warning}")
        return "\n".join(lines).rstrip() + "\n"
