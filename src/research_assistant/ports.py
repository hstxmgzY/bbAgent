"""Dependency boundaries for the research workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from research_assistant.models import (
    AtomicClaim,
    Chunk,
    CitationEvaluation,
    DocumentVersion,
    IndexedChunk,
    MemoryHit,
    MemoryScope,
    QueryRewriteRequest,
    ResearchQuery,
    ResearchReport,
    ResearchRequest,
    ResearchRun,
    RetrievalRequest,
    RetrievedChunk,
    SearchResult,
    ToolResult,
)


class SearchBackend(Protocol):
    async def search(self, query: str, limit: int = 5) -> ToolResult: ...


class DocumentReader(Protocol):
    async def read(self, url: str) -> ToolResult: ...


class AnswerSynthesizer(Protocol):
    async def synthesize(
        self, topic: str, evidence: list[Chunk], warnings: list[str]
    ) -> str: ...


class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class EvidenceIndex(Protocol):
    persistent: bool

    async def upsert(self, chunks: list[IndexedChunk]) -> None: ...

    async def search(self, request: RetrievalRequest) -> list[RetrievedChunk]: ...

    async def delete_document_version(self, document_version_id: str) -> None: ...


class MemoryRepository(Protocol):
    async def begin_run(self, request: ResearchRequest) -> ResearchRun: ...

    async def finish_run(
        self, run_id: str, report: ResearchReport, quality_json: str
    ) -> None: ...

    async def fail_run(self, run_id: str) -> None: ...

    async def find_related_runs(
        self, topic: str, scope: MemoryScope, limit: int = 5
    ) -> list[MemoryHit]: ...

    async def get_fresh_document(
        self, canonical_url: str, scope: MemoryScope, now: datetime
    ) -> DocumentVersion | None: ...

    async def get_document_version(
        self, document_version_id: str, scope: MemoryScope
    ) -> DocumentVersion | None: ...

    async def save_document(
        self,
        *,
        url: str,
        title: str,
        text: str,
        fetched_at: datetime,
        expires_at: datetime,
        scope_partition: str,
    ) -> DocumentVersion: ...

    async def save_query(
        self,
        run_id: str,
        query: ResearchQuery,
        result_count: int,
        duration_seconds: float,
        error_type: str | None,
    ) -> None: ...

    async def save_evaluation(
        self, run_id: str, evaluation: CitationEvaluation
    ) -> None: ...

    async def is_document_indexed(
        self,
        document_version_id: str,
        embedding_model: str,
        chunker_version: str,
    ) -> bool: ...

    async def mark_document_indexed(
        self,
        document_version_id: str,
        embedding_model: str,
        chunker_version: str,
    ) -> None: ...

    async def delete_scope(self, scope: MemoryScope) -> list[str]: ...


class QueryRewriter(Protocol):
    async def rewrite(self, request: QueryRewriteRequest) -> list[ResearchQuery]: ...


class ClaimVerifier(Protocol):
    @property
    def version(self) -> str: ...

    async def verify(
        self, claim: AtomicClaim, evidence: list[Chunk]
    ) -> tuple[str, float, list[str]]: ...


class ResearchRepository(Protocol):
    """Legacy JSON repository retained for compatibility and small demos."""

    def remember_topic(self, topic: str) -> None: ...

    def remember_query(self, query: str) -> bool: ...

    def remember_source(self, url: str, title: str) -> None: ...

    def save(self) -> None: ...
