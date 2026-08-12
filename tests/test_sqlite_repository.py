import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from research_assistant.models import MemoryScope, ResearchRequest
from research_assistant.repositories.sqlite import SqliteMemoryRepository


def test_document_versions_are_idempotent_and_content_changes_create_history(tmp_path):
    repository = SqliteMemoryRepository(tmp_path / "research.db")
    now = datetime.now(UTC)
    first = asyncio.run(
        repository.save_document(
            url="https://Example.com/docs/?utm_source=test",
            title="Doc",
            text="first body",
            fetched_at=now,
            expires_at=now + timedelta(hours=1),
            scope_partition="global",
        )
    )
    duplicate = asyncio.run(
        repository.save_document(
            url="https://example.com/docs",
            title="Doc",
            text="first body",
            fetched_at=now,
            expires_at=now + timedelta(hours=2),
            scope_partition="global",
        )
    )
    changed = asyncio.run(
        repository.save_document(
            url="https://example.com/docs",
            title="Doc v2",
            text="second body",
            fetched_at=now,
            expires_at=now + timedelta(hours=2),
            scope_partition="global",
        )
    )

    assert first.document_version_id == duplicate.document_version_id
    assert changed.document_version_id != first.document_version_id
    fresh = asyncio.run(
        repository.get_fresh_document("https://example.com/docs", MemoryScope(), now)
    )
    assert fresh.document_version_id == changed.document_version_id
    with sqlite3.connect(repository.path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM document_versions").fetchone()[0]
            == 2
        )


def test_memory_scope_and_legacy_migration_are_isolated_and_idempotent(tmp_path):
    repository = SqliteMemoryRepository(tmp_path / "research.db")
    request = ResearchRequest(topic="private topic", session_id="session-a")
    run = asyncio.run(repository.begin_run(request))
    assert (
        asyncio.run(
            repository.find_related_runs(
                "private topic", MemoryScope(session_id="session-b")
            )
        )
        == []
    )

    legacy_path = tmp_path / "memory.json"
    legacy_path.write_text(
        json.dumps(
            {
                "recent_topics": ["legacy RAG"],
                "queries": ["rag evidence"],
                "sources": {"https://example.com": "Example"},
                "answer": "must never become evidence",
            }
        ),
        encoding="utf-8",
    )
    assert repository.migrate_legacy_json(legacy_path) is True
    assert repository.migrate_legacy_json(legacy_path) is False
    with sqlite3.connect(repository.path) as connection:
        bodies = connection.execute(
            "SELECT body_text FROM document_versions"
        ).fetchall()
        assert bodies == []
        assert (
            connection.execute("SELECT count(*) FROM legacy_imports").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT status FROM research_runs WHERE run_id = ?", (run.run_id,)
            ).fetchone()[0]
            == "running"
        )


def test_delete_scope_removes_only_requested_private_memory(tmp_path):
    repository = SqliteMemoryRepository(tmp_path / "research.db")
    now = datetime.now(UTC)
    private = asyncio.run(
        repository.save_document(
            url="https://example.com/private",
            title="Private",
            text="private evidence",
            fetched_at=now,
            expires_at=now + timedelta(hours=1),
            scope_partition="session:a",
        )
    )
    asyncio.run(repository.begin_run(ResearchRequest(topic="private", session_id="a")))
    asyncio.run(
        repository.begin_run(ResearchRequest(topic="other private", session_id="b"))
    )

    deleted = asyncio.run(repository.delete_scope(MemoryScope(session_id="a")))

    assert deleted == [private.document_version_id]
    with sqlite3.connect(repository.path) as connection:
        partitions = {
            row[0]
            for row in connection.execute("SELECT scope_partition FROM research_runs")
        }
    assert partitions == {"session:b"}
