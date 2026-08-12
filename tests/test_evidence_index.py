import asyncio
from datetime import UTC, datetime, timedelta

from research_assistant.models import (
    Chunk,
    IndexedChunk,
    MemoryScope,
    RetrievalRequest,
)
from research_assistant.rag import InMemoryEvidenceIndex


def test_evidence_index_enforces_scope_ttl_and_idempotent_upsert():
    now = datetime.now(UTC)
    index = InMemoryEvidenceIndex(2)
    chunks = [
        _indexed("global", "G", [1.0, 0.0], now + timedelta(hours=1)),
        _indexed("session:a", "A", [0.9, 0.1], now + timedelta(hours=1)),
        _indexed("session:b", "B", [1.0, 0.0], now + timedelta(hours=1)),
        _indexed("global", "OLD", [1.0, 0.0], now - timedelta(seconds=1)),
    ]
    asyncio.run(index.upsert(chunks))
    asyncio.run(index.upsert([chunks[0]]))

    results = asyncio.run(
        index.search(
            RetrievalRequest(
                query_vector=[1.0, 0.0],
                embedding_model="test-model",
                scope=MemoryScope(session_id="a"),
                candidate_k=10,
                top_k=10,
                max_per_source=2,
                min_score=-1.0,
                now=now,
            )
        )
    )

    assert {item.chunk_id for item in results} == {"G", "A"}
    assert len(index._items) == 4


def _indexed(scope, chunk_id, vector, expires_at):
    now = datetime.now(UTC)
    chunk = Chunk(
        chunk_id,
        chunk_id,
        chunk_id,
        f"https://example.com/{chunk_id}",
        f"evidence {chunk_id}",
        document_version_id=f"version-{chunk_id}",
        chunk_index=1,
        content_hash=f"sha256:{chunk_id}",
        fetched_at=now,
        expires_at=expires_at,
        scope_partition=scope,
    )
    return IndexedChunk(
        chunk=chunk,
        vector=vector,
        document_version_id=f"version-{chunk_id}",
        persistent_source_id=chunk_id,
        embedding_model="test-model",
        scope_partition=scope,
        fetched_at=now,
        expires_at=expires_at,
        content_hash=f"sha256:{chunk_id}",
    )
