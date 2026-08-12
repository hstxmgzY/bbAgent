import asyncio
import math

from research_assistant.embeddings import (
    HashingEmbedder,
    SentenceTransformerEmbedder,
)


class FakeVector:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return self.values


class FakeSentenceModel:
    def __init__(self):
        self.calls = []

    def get_embedding_dimension(self):
        return 3

    def encode_document(self, texts, **kwargs):
        self.calls.append(("document", texts, kwargs))
        return [FakeVector([1.0, 0.0, 0.0]) for _ in texts]

    def encode_query(self, texts, **kwargs):
        self.calls.append(("query", texts, kwargs))
        return [FakeVector([0.0, 1.0, 0.0])]


def test_hashing_embedder_async_api_returns_normalized_vectors():
    embedder = HashingEmbedder(32)
    document, query = asyncio.run(
        _embed_both(embedder, "retrieval evidence", "retrieval")
    )

    assert len(document) == len(query) == 32
    assert math.isclose(sum(value * value for value in document), 1.0)
    assert embedder.model_id == "hashing-v1-32"


def test_sentence_transformer_uses_asymmetric_entrypoints_and_batch_options():
    embedder = SentenceTransformerEmbedder("fake/model", batch_size=7)
    fake = FakeSentenceModel()
    embedder._model = fake
    try:
        documents, query = asyncio.run(
            _embed_many(embedder, ["doc one", "doc two"], "query")
        )
    finally:
        embedder.close()

    assert embedder.dimensions == 3
    assert documents == [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
    assert query == [0.0, 1.0, 0.0]
    assert [call[0] for call in fake.calls] == ["document", "query"]
    assert fake.calls[0][2]["batch_size"] == 7
    assert fake.calls[0][2]["normalize_embeddings"] is True


async def _embed_both(embedder, document, query):
    documents = await embedder.embed_documents([document])
    query_vector = await embedder.embed_query(query)
    return documents[0], query_vector


async def _embed_many(embedder, documents, query):
    return await embedder.embed_documents(documents), await embedder.embed_query(query)
