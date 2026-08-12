import asyncio
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("qdrant_client")

from research_assistant.models import (  # noqa: E402
    Chunk,
    IndexedChunk,
    MemoryScope,
    RetrievalRequest,
)
from research_assistant.vectorstores.qdrant import QdrantEvidenceIndex  # noqa: E402


def test_local_qdrant_persists_and_reopens(tmp_path):
    path = tmp_path / "qdrant"
    now = datetime.now(UTC)
    chunk = Chunk(
        "version-1-C1",
        "source-1",
        "Doc",
        "https://example.com/doc",
        "persistent evidence",
        document_version_id="version-1",
        chunk_index=1,
        content_hash="sha256:test",
        fetched_at=now,
        expires_at=now + timedelta(hours=1),
    )
    indexed = IndexedChunk(
        chunk=chunk,
        vector=[1.0, 0.0],
        document_version_id="version-1",
        persistent_source_id="source-1",
        embedding_model="test-model",
        scope_partition="global",
        fetched_at=now,
        expires_at=now + timedelta(hours=1),
        content_hash="sha256:test",
    )

    async def write():
        index = QdrantEvidenceIndex(
            dimensions=2,
            embedding_model="test-model",
            path=path,
        )
        await index.upsert([indexed])
        await index.upsert([indexed])
        await index.close()

    async def reopen():
        index = QdrantEvidenceIndex(
            dimensions=2,
            embedding_model="test-model",
            path=path,
        )
        result = await index.search(
            RetrievalRequest(
                query_vector=[1.0, 0.0],
                embedding_model="test-model",
                scope=MemoryScope(),
                candidate_k=5,
                top_k=5,
                min_score=-1.0,
            )
        )
        await index.close()
        return result

    asyncio.run(write())
    result = asyncio.run(reopen())

    assert [item.chunk_id for item in result] == ["version-1-C1"]
