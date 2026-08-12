from research_assistant.memory import MemoryStore


def test_memory_deduplicates_queries(tmp_path):
    memory = MemoryStore(tmp_path / "memory.json")

    assert memory.remember_query("rag") is True
    assert memory.remember_query("rag") is False

    memory.remember_topic("Stage 2")
    memory.remember_source("https://example.com", "Example")
    memory.save()

    loaded = MemoryStore(tmp_path / "memory.json")
    assert loaded.data["recent_topics"][0] == "Stage 2"
    assert loaded.data["sources"]["https://example.com"] == "Example"
