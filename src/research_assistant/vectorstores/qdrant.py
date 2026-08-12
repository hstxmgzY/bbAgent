"""Qdrant-backed persistent evidence index."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_assistant.models import IndexedChunk, RetrievalRequest, RetrievedChunk
from research_assistant.rag import _select_with_source_quota


POINT_NAMESPACE = uuid.UUID("d4b1328c-5de4-4f61-8591-66647ebd19ca")


class QdrantEvidenceIndex:
    persistent = True

    def __init__(
        self,
        *,
        dimensions: int,
        embedding_model: str,
        collection_prefix: str = "research_chunks",
        path: Path | None = None,
        url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        if bool(path) == bool(url):
            raise ValueError("configure exactly one of Qdrant path or url")
        version_hash = hashlib.sha256(embedding_model.encode("utf-8")).hexdigest()[:12]
        self.collection_name = f"{collection_prefix}_{version_hash}"
        self.dimensions = dimensions
        self.embedding_model = embedding_model
        self.path = path
        self.url = url
        self.api_key = api_key
        self._client_instance: Any | None = None
        self._models_module: Any | None = None
        self._init_lock = asyncio.Lock()
        self._initialized = False

    def _load_qdrant(self) -> tuple[Any, Any]:
        try:
            from qdrant_client import QdrantClient, models
        except ImportError as exc:
            raise RuntimeError(
                "Qdrant evidence storage requires the 'semantic-rag' optional "
                "dependency group"
            ) from exc
        return QdrantClient, models

    def _client(self) -> tuple[Any, Any]:
        if self._client_instance is None:
            client_class, models = self._load_qdrant()
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                client = client_class(path=str(self.path))
            else:
                client = client_class(url=self.url, api_key=self.api_key)
            self._client_instance = client
            self._models_module = models
        return self._client_instance, self._models_module

    async def _ensure_collection(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._ensure_collection_sync)
            self._initialized = True

    def _ensure_collection_sync(self) -> None:
        client, models = self._client()
        if not client.collection_exists(self.collection_name):
            client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=self.dimensions, distance=models.Distance.COSINE
                ),
            )
            return
        info = client.get_collection(self.collection_name)
        vectors = info.config.params.vectors
        actual_size = getattr(vectors, "size", None)
        if actual_size is None and isinstance(vectors, dict) and vectors:
            actual_size = getattr(next(iter(vectors.values())), "size", None)
        if int(actual_size or -1) != self.dimensions:
            raise ValueError(
                f"Qdrant collection dimension mismatch for {self.collection_name}: "
                f"expected {self.dimensions}, found {actual_size}"
            )

    async def upsert(self, chunks: list[IndexedChunk]) -> None:
        if not chunks:
            return
        await self._ensure_collection()
        await asyncio.to_thread(self._upsert_sync, chunks)

    def _upsert_sync(self, chunks: list[IndexedChunk]) -> None:
        client, models = self._client()
        by_source: dict[str, set[str]] = defaultdict(set)
        for indexed in chunks:
            if len(indexed.vector) != self.dimensions:
                raise ValueError(
                    f"vector dimension mismatch: expected {self.dimensions}, "
                    f"received {len(indexed.vector)}"
                )
            by_source[indexed.persistent_source_id].add(indexed.document_version_id)

        for source_id, current_versions in by_source.items():
            client.delete(
                collection_name=self.collection_name,
                wait=True,
                points_selector=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source_id", match=models.MatchValue(value=source_id)
                        )
                    ],
                    must_not=[
                        models.FieldCondition(
                            key="document_version_id",
                            match=models.MatchAny(any=list(current_versions)),
                        )
                    ],
                ),
            )

        points = []
        for indexed in chunks:
            chunk = indexed.chunk
            point_id = str(
                uuid.uuid5(
                    POINT_NAMESPACE,
                    f"{indexed.embedding_model}|{indexed.document_version_id}|"
                    f"{chunk.chunk_index}",
                )
            )
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=indexed.vector,
                    payload={
                        "chunk_id": chunk.chunk_id,
                        "chunk_index": chunk.chunk_index,
                        "document_version_id": indexed.document_version_id,
                        "source_id": indexed.persistent_source_id,
                        "canonical_url": chunk.url,
                        "title": chunk.title,
                        "language": chunk.language,
                        "text": chunk.text,
                        "content_hash": indexed.content_hash,
                        "fetched_at": indexed.fetched_at.isoformat(),
                        "expires_at": indexed.expires_at.isoformat(),
                        "expires_at_ts": indexed.expires_at.timestamp(),
                        "scope_partition": indexed.scope_partition,
                        "embedding_model": indexed.embedding_model,
                        "chunker_version": indexed.chunker_version,
                    },
                )
            )
        client.upsert(
            collection_name=self.collection_name,
            wait=True,
            points=points,
        )

    async def search(self, request: RetrievalRequest) -> list[RetrievedChunk]:
        await self._ensure_collection()
        return await asyncio.to_thread(self._search_sync, request)

    def _search_sync(self, request: RetrievalRequest) -> list[RetrievedChunk]:
        client, models = self._client()
        if len(request.query_vector) != self.dimensions:
            raise ValueError("query vector dimension does not match Qdrant collection")
        conditions = [
            models.FieldCondition(
                key="scope_partition",
                match=models.MatchAny(any=list(request.scope.visible_partitions)),
            ),
            models.FieldCondition(
                key="embedding_model",
                match=models.MatchValue(value=request.embedding_model),
            ),
            models.FieldCondition(
                key="chunker_version",
                match=models.MatchValue(value=request.chunker_version),
            ),
            models.FieldCondition(
                key="expires_at_ts",
                range=models.Range(gte=request.now.timestamp()),
            ),
        ]
        points = client.query_points(
            collection_name=self.collection_name,
            query=request.query_vector,
            query_filter=models.Filter(must=conditions),
            limit=request.candidate_k,
            score_threshold=request.min_score,
            with_payload=True,
        ).points
        ranked: list[RetrievedChunk] = []
        for point in points:
            payload = point.payload or {}
            ranked.append(
                RetrievedChunk(
                    chunk_id=str(payload.get("chunk_id") or point.id),
                    source_id=str(payload.get("source_id") or ""),
                    title=str(payload.get("title") or "Untitled"),
                    url=str(payload.get("canonical_url") or ""),
                    text=str(payload.get("text") or ""),
                    score=float(point.score),
                    document_version_id=str(payload.get("document_version_id") or ""),
                    chunk_index=int(payload.get("chunk_index") or 0),
                    content_hash=str(payload.get("content_hash") or ""),
                    fetched_at=_parse_datetime(payload.get("fetched_at")),
                    expires_at=_parse_datetime(payload.get("expires_at")),
                    language=str(payload.get("language") or "unknown"),
                    scope_partition=str(payload.get("scope_partition") or "global"),
                    embedding_model=str(payload.get("embedding_model") or ""),
                    chunker_version=str(payload.get("chunker_version") or ""),
                )
            )
        return _select_with_source_quota(
            ranked,
            top_k=request.top_k,
            max_per_source=request.max_per_source,
        )

    async def delete_document_version(self, document_version_id: str) -> None:
        await self._ensure_collection()
        await asyncio.to_thread(self._delete_document_version_sync, document_version_id)

    def _delete_document_version_sync(self, document_version_id: str) -> None:
        client, models = self._client()
        client.delete(
            collection_name=self.collection_name,
            wait=True,
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key="document_version_id",
                        match=models.MatchValue(value=document_version_id),
                    )
                ]
            ),
        )

    async def close(self) -> None:
        if self._client_instance is not None:
            await asyncio.to_thread(self._client_instance.close)


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
