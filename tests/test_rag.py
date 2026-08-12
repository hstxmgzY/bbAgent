from research_assistant.models import SourceDocument
from research_assistant.rag import VectorIndex, chunk_text


def test_chunk_and_retrieve_returns_relevant_source():
    doc = SourceDocument(
        source_id="S1",
        title="RAG Notes",
        url="https://example.com/rag",
        text="RAG uses retrieval and citations. " * 80
        + "Cooking recipes use ingredients and ovens. " * 80,
    )
    chunks = chunk_text(doc, chunk_size=30, overlap=5)
    index = VectorIndex()
    index.add(chunks)

    results = index.retrieve("retrieval citations", k=3)

    assert results
    assert results[0].source_id == "S1"
    assert "retrieval" in results[0].text.lower()


def test_chunk_text_splits_chinese_without_spaces_with_overlap():
    text = "检索增强生成需要可靠的证据和引用。" * 80
    doc = SourceDocument("S1", "中文资料", "https://example.com/zh", text)

    chunks = chunk_text(doc, chunk_size=100, overlap=20)

    assert len(chunks) > 2
    assert all(len(chunk.text) <= 100 for chunk in chunks)
    assert chunks[0].text[-20:] == chunks[1].text[:20]
