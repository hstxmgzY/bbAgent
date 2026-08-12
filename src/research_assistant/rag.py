"""Chunking and dependency-light evidence index implementations."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from research_assistant.embeddings import HashingEmbedder, cosine
from research_assistant.models import (
    Chunk,
    IndexedChunk,
    RetrievalRequest,
    RetrievedChunk,
    SourceDocument,
)


CHUNKER_VERSION = "token-window-v1"


def chunk_text(
    document: SourceDocument,
    chunk_size: int = 900,
    overlap: int = 160,
) -> list[Chunk]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")

    units = list(re.finditer(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\s]", document.text))
    if not units:
        return []

    chunks: list[Chunk] = []
    start = 0
    index = 1
    while start < len(units):
        end = min(start + chunk_size, len(units))
        text = document.text[units[start].start() : units[end - 1].end()].strip()
        version_prefix = document.document_version_id or document.source_id
        chunks.append(
            Chunk(
                chunk_id=f"{version_prefix}-C{index}",
                source_id=document.persistent_source_id or document.source_id,
                title=document.title,
                url=document.url,
                text=text,
                document_version_id=document.document_version_id,
                chunk_index=index,
                content_hash=document.content_hash,
                fetched_at=document.fetched_at,
                expires_at=document.expires_at,
                language=document.language,
                scope_partition=document.scope_partition,
                chunker_version=CHUNKER_VERSION,
            )
        )
        if end == len(units):
            break
        start = max(0, end - overlap)
        index += 1
    return chunks


class VectorIndex:
    """Original synchronous index retained as a compact unit-test implementation."""

    def __init__(self, embedder: HashingEmbedder | None = None):
        self.embedder = embedder or HashingEmbedder()
        self._items: list[tuple[Chunk, list[float]]] = []

    def add(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            self._items.append((chunk, self.embedder.embed(chunk.text)))

    def retrieve(
        self, query: str, k: int = 8, min_score: float = 0.0, max_per_source: int = 2
    ) -> list[Chunk]:
        query_vector = self.embedder.embed(query)
        ranked: list[Chunk] = []
        for chunk, vector in self._items:
            values = vars(chunk).copy()
            values["score"] = cosine(query_vector, vector)
            ranked.append(Chunk(**values))
        ranked.sort(key=lambda item: item.score, reverse=True)
        selected: list[Chunk] = []
        source_counts: dict[str, int] = {}
        seen_text: set[str] = set()
        for chunk in ranked:
            if chunk.score <= min_score or chunk.text in seen_text:
                continue
            if source_counts.get(chunk.source_id, 0) >= max_per_source:
                continue
            selected.append(chunk)
            seen_text.add(chunk.text)
            source_counts[chunk.source_id] = source_counts.get(chunk.source_id, 0) + 1
            if len(selected) >= k:
                break
        return selected


class InMemoryEvidenceIndex:
    """Async EvidenceIndex used for tests and dependency-light deployments."""

    persistent = False

    def __init__(self, dimensions: int):
        self.dimensions = dimensions
        self._items: dict[str, IndexedChunk] = {}

    async def upsert(self, chunks: list[IndexedChunk]) -> None:
        for indexed in chunks:
            if len(indexed.vector) != self.dimensions:
                raise ValueError(
                    f"vector dimension mismatch: expected {self.dimensions}, "
                    f"received {len(indexed.vector)}"
                )
            self._items[indexed.chunk.chunk_id] = indexed

    async def search(self, request: RetrievalRequest) -> list[RetrievedChunk]:
        if len(request.query_vector) != self.dimensions:
            raise ValueError("query vector dimension does not match evidence index")
        ranked: list[RetrievedChunk] = []
        visible = set(request.scope.visible_partitions)
        now = _aware(request.now)
        for indexed in self._items.values():
            if indexed.embedding_model != request.embedding_model:
                continue
            if indexed.scope_partition not in visible:
                continue
            if indexed.chunker_version != request.chunker_version:
                continue
            if _aware(indexed.expires_at) <= now:
                continue
            score = cosine(request.query_vector, indexed.vector)
            if request.min_score is not None and score < request.min_score:
                continue
            values = vars(indexed.chunk).copy()
            values["score"] = score
            values["embedding_model"] = indexed.embedding_model
            ranked.append(RetrievedChunk(**values))
        ranked.sort(key=lambda item: item.score, reverse=True)
        return _select_with_source_quota(
            ranked[: request.candidate_k],
            top_k=request.top_k,
            max_per_source=request.max_per_source,
        )

    async def delete_document_version(self, document_version_id: str) -> None:
        self._items = {
            chunk_id: item
            for chunk_id, item in self._items.items()
            if item.document_version_id != document_version_id
        }


def _select_with_source_quota(
    ranked: list[RetrievedChunk], *, top_k: int, max_per_source: int
) -> list[RetrievedChunk]:
    selected: list[RetrievedChunk] = []
    source_counts: dict[str, int] = {}
    seen_text: set[str] = set()
    for chunk in ranked:
        normalized_text = " ".join(chunk.text.split())
        if normalized_text in seen_text:
            continue
        if source_counts.get(chunk.source_id, 0) >= max_per_source:
            continue
        selected.append(chunk)
        seen_text.add(normalized_text)
        source_counts[chunk.source_id] = source_counts.get(chunk.source_id, 0) + 1
        if len(selected) >= top_k:
            break
    return selected


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
