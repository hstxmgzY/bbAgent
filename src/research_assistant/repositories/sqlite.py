"""SQLite metadata, memory, document version, and evaluation repository."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from research_assistant.embeddings import tokenize
from research_assistant.models import (
    CitationEvaluation,
    DocumentVersion,
    MemoryHit,
    MemoryScope,
    ResearchQuery,
    ResearchReport,
    ResearchRequest,
    ResearchRun,
)


SOURCE_NAMESPACE = uuid.UUID("ad99cfbf-321c-47d6-b937-9ba8e7377f54")
VERSION_NAMESPACE = uuid.UUID("3a89143f-e94e-45b1-b8dc-ad4aeb14577d")
LEGACY_NAMESPACE = uuid.UUID("6771db54-16af-4b0c-b0c1-cb1bedfe9010")
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}


class SqliteMemoryRepository:
    """Transactional metadata store with one short-lived connection per operation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _migrate(self) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > 1:
                raise RuntimeError(
                    f"unsupported research database schema version: {version}"
                )
            if version == 0:
                connection.executescript(
                    """
                    CREATE TABLE research_runs (
                        run_id TEXT PRIMARY KEY,
                        session_id TEXT,
                        user_scope TEXT,
                        scope_partition TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        answer TEXT,
                        quality_json TEXT
                    );
                    CREATE INDEX research_runs_scope_started
                        ON research_runs(scope_partition, started_at DESC);

                    CREATE TABLE research_queries (
                        query_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES research_runs(run_id) ON DELETE CASCADE,
                        original_query TEXT NOT NULL,
                        rewritten_query TEXT NOT NULL,
                        intent TEXT NOT NULL,
                        derived_from TEXT NOT NULL,
                        result_count INTEGER NOT NULL DEFAULT 0,
                        valid_evidence_count INTEGER NOT NULL DEFAULT 0,
                        duration_seconds REAL NOT NULL DEFAULT 0,
                        error_type TEXT
                    );

                    CREATE TABLE sources (
                        source_id TEXT PRIMARY KEY,
                        canonical_url TEXT NOT NULL UNIQUE,
                        domain TEXT NOT NULL,
                        source_type TEXT NOT NULL DEFAULT 'web',
                        first_discovered_at TEXT NOT NULL,
                        last_discovered_at TEXT NOT NULL
                    );

                    CREATE TABLE document_versions (
                        document_version_id TEXT PRIMARY KEY,
                        source_id TEXT NOT NULL REFERENCES sources(source_id),
                        content_hash TEXT NOT NULL,
                        title TEXT NOT NULL,
                        language TEXT NOT NULL,
                        fetched_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        parser_version TEXT NOT NULL,
                        body_text TEXT NOT NULL,
                        scope_partition TEXT NOT NULL,
                        http_etag TEXT,
                        http_last_modified TEXT,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(source_id, content_hash, scope_partition)
                    );
                    CREATE INDEX document_versions_fresh
                        ON document_versions(source_id, is_active, expires_at);

                    CREATE TABLE run_sources (
                        run_id TEXT NOT NULL REFERENCES research_runs(run_id) ON DELETE CASCADE,
                        document_version_id TEXT NOT NULL REFERENCES document_versions(document_version_id),
                        PRIMARY KEY(run_id, document_version_id)
                    );

                    CREATE TABLE document_index_state (
                        document_version_id TEXT NOT NULL REFERENCES document_versions(document_version_id) ON DELETE CASCADE,
                        embedding_model TEXT NOT NULL,
                        chunker_version TEXT NOT NULL,
                        indexed_at TEXT NOT NULL,
                        PRIMARY KEY(document_version_id, embedding_model, chunker_version)
                    );

                    CREATE TABLE claims (
                        claim_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES research_runs(run_id) ON DELETE CASCADE,
                        claim_text TEXT NOT NULL,
                        importance INTEGER NOT NULL,
                        verifiable INTEGER NOT NULL,
                        verdict TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        verifier_version TEXT NOT NULL
                    );
                    CREATE TABLE claim_citations (
                        claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
                        source_id TEXT NOT NULL,
                        PRIMARY KEY(claim_id, source_id)
                    );
                    CREATE TABLE claim_evidence (
                        claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
                        chunk_id TEXT NOT NULL,
                        verdict TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        verifier_version TEXT NOT NULL,
                        PRIMARY KEY(claim_id, chunk_id)
                    );

                    CREATE TABLE legacy_imports (
                        import_key TEXT PRIMARY KEY,
                        imported_at TEXT NOT NULL
                    );
                    PRAGMA user_version = 1;
                    """
                )

    async def begin_run(self, request: ResearchRequest) -> ResearchRun:
        return await asyncio.to_thread(self._begin_run_sync, request)

    def _begin_run_sync(self, request: ResearchRequest) -> ResearchRun:
        run = ResearchRun(
            run_id=str(uuid.uuid4()),
            topic=request.topic,
            scope_partition=request.memory_scope.run_partition,
        )
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO research_runs(
                    run_id, session_id, user_scope, scope_partition, topic,
                    request_json, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    run.run_id,
                    request.session_id,
                    request.user_scope,
                    run.scope_partition,
                    request.topic,
                    json.dumps(asdict(request), ensure_ascii=False),
                    run.started_at.isoformat(),
                ),
            )
        return run

    async def finish_run(
        self, run_id: str, report: ResearchReport, quality_json: str
    ) -> None:
        await asyncio.to_thread(self._finish_run_sync, run_id, report, quality_json)

    def _finish_run_sync(
        self, run_id: str, report: ResearchReport, quality_json: str
    ) -> None:
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE research_runs
                SET status = ?, finished_at = ?, answer = ?, quality_json = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (
                    report.status,
                    datetime.now(UTC).isoformat(),
                    report.answer,
                    quality_json,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"research run is not active: {run_id}")
            connection.executemany(
                "INSERT OR IGNORE INTO run_sources(run_id, document_version_id) VALUES (?, ?)",
                [
                    (run_id, source.document_version_id)
                    for source in report.sources
                    if source.document_version_id
                ],
            )

    async def fail_run(self, run_id: str) -> None:
        await asyncio.to_thread(self._fail_run_sync, run_id)

    def _fail_run_sync(self, run_id: str) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE research_runs SET status = 'failed', finished_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (datetime.now(UTC).isoformat(), run_id),
            )

    async def find_related_runs(
        self, topic: str, scope: MemoryScope, limit: int = 5
    ) -> list[MemoryHit]:
        return await asyncio.to_thread(
            self._find_related_runs_sync, topic, scope, limit
        )

    def _find_related_runs_sync(
        self, topic: str, scope: MemoryScope, limit: int
    ) -> list[MemoryHit]:
        placeholders = ",".join("?" for _ in scope.visible_partitions)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT run_id, topic FROM research_runs
                WHERE scope_partition IN ({placeholders}) AND status IN ('complete', 'insufficient_evidence')
                ORDER BY started_at DESC LIMIT 100
                """,
                scope.visible_partitions,
            ).fetchall()
            candidates: list[MemoryHit] = []
            for row in rows:
                score = _topic_similarity(topic, row["topic"])
                if score <= 0:
                    continue
                query_rows = connection.execute(
                    """
                    SELECT rewritten_query, intent, derived_from
                    FROM research_queries WHERE run_id = ?
                    ORDER BY valid_evidence_count DESC, result_count DESC, query_id
                    """,
                    (row["run_id"],),
                ).fetchall()
                candidates.append(
                    MemoryHit(
                        run_id=row["run_id"],
                        topic=row["topic"],
                        queries=[
                            ResearchQuery(
                                query=item["rewritten_query"],
                                intent=item["intent"],
                                derived_from=item["derived_from"],
                            )
                            for item in query_rows
                        ],
                        score=score,
                    )
                )
        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates[: max(0, limit)]

    async def get_fresh_document(
        self, canonical_url: str, scope: MemoryScope, now: datetime
    ) -> DocumentVersion | None:
        return await asyncio.to_thread(
            self._get_fresh_document_sync, canonical_url, scope, now
        )

    def _get_fresh_document_sync(
        self, url: str, scope: MemoryScope, now: datetime
    ) -> DocumentVersion | None:
        canonical_url = canonicalize_url(url)
        placeholders = ",".join("?" for _ in scope.visible_partitions)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT d.*, s.canonical_url FROM document_versions d
                JOIN sources s ON s.source_id = d.source_id
                WHERE s.canonical_url = ? AND d.is_active = 1
                  AND d.scope_partition IN ({placeholders}) AND d.expires_at > ?
                ORDER BY d.fetched_at DESC LIMIT 1
                """,
                (canonical_url, *scope.visible_partitions, _as_utc(now).isoformat()),
            ).fetchone()
        return _row_to_document(row) if row else None

    async def get_document_version(
        self, document_version_id: str, scope: MemoryScope
    ) -> DocumentVersion | None:
        return await asyncio.to_thread(
            self._get_document_version_sync, document_version_id, scope
        )

    def _get_document_version_sync(
        self, document_version_id: str, scope: MemoryScope
    ) -> DocumentVersion | None:
        placeholders = ",".join("?" for _ in scope.visible_partitions)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT d.*, s.canonical_url FROM document_versions d
                JOIN sources s ON s.source_id = d.source_id
                WHERE d.document_version_id = ? AND d.scope_partition IN ({placeholders})
                """,
                (document_version_id, *scope.visible_partitions),
            ).fetchone()
        return _row_to_document(row) if row else None

    async def save_document(
        self,
        *,
        url: str,
        title: str,
        text: str,
        fetched_at: datetime,
        expires_at: datetime,
        scope_partition: str,
    ) -> DocumentVersion:
        return await asyncio.to_thread(
            self._save_document_sync,
            url,
            title,
            text,
            fetched_at,
            expires_at,
            scope_partition,
        )

    def _save_document_sync(
        self,
        url: str,
        title: str,
        text: str,
        fetched_at: datetime,
        expires_at: datetime,
        scope_partition: str,
    ) -> DocumentVersion:
        canonical_url = canonicalize_url(url)
        source_id = str(uuid.uuid5(SOURCE_NAMESPACE, canonical_url))
        content_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        document_version_id = str(
            uuid.uuid5(
                VERSION_NAMESPACE, f"{source_id}|{content_hash}|{scope_partition}"
            )
        )
        fetched = _as_utc(fetched_at)
        expires = _as_utc(expires_at)
        language = detect_language(text)
        domain = urlsplit(canonical_url).hostname or ""
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sources(
                    source_id, canonical_url, domain, first_discovered_at, last_discovered_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(canonical_url) DO UPDATE SET last_discovered_at = excluded.last_discovered_at
                """,
                (
                    source_id,
                    canonical_url,
                    domain,
                    fetched.isoformat(),
                    fetched.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE document_versions SET is_active = 0
                WHERE source_id = ? AND scope_partition = ? AND document_version_id != ?
                """,
                (source_id, scope_partition, document_version_id),
            )
            connection.execute(
                """
                INSERT INTO document_versions(
                    document_version_id, source_id, content_hash, title, language,
                    fetched_at, expires_at, parser_version, body_text,
                    scope_partition, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'webread-v1', ?, ?, 1)
                ON CONFLICT(document_version_id) DO UPDATE SET
                    title = excluded.title,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at,
                    body_text = excluded.body_text,
                    is_active = 1
                """,
                (
                    document_version_id,
                    source_id,
                    content_hash,
                    title,
                    language,
                    fetched.isoformat(),
                    expires.isoformat(),
                    text,
                    scope_partition,
                ),
            )
        return DocumentVersion(
            document_version_id=document_version_id,
            source_id=source_id,
            canonical_url=canonical_url,
            title=title,
            text=text,
            content_hash=content_hash,
            fetched_at=fetched,
            expires_at=expires,
            language=language,
            scope_partition=scope_partition,
        )

    async def save_query(
        self,
        run_id: str,
        query: ResearchQuery,
        result_count: int,
        duration_seconds: float,
        error_type: str | None,
    ) -> None:
        await asyncio.to_thread(
            self._save_query_sync,
            run_id,
            query,
            result_count,
            duration_seconds,
            error_type,
        )

    def _save_query_sync(
        self,
        run_id: str,
        query: ResearchQuery,
        result_count: int,
        duration_seconds: float,
        error_type: str | None,
    ) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO research_queries(
                    run_id, original_query, rewritten_query, intent, derived_from,
                    result_count, duration_seconds, error_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    query.query,
                    query.query,
                    query.intent,
                    query.derived_from,
                    result_count,
                    duration_seconds,
                    error_type,
                ),
            )

    async def save_evaluation(
        self, run_id: str, evaluation: CitationEvaluation
    ) -> None:
        await asyncio.to_thread(self._save_evaluation_sync, run_id, evaluation)

    def _save_evaluation_sync(
        self, run_id: str, evaluation: CitationEvaluation
    ) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute("DELETE FROM claims WHERE run_id = ?", (run_id,))
            for item in evaluation.assessments:
                stored_claim_id = f"{run_id}:{item.claim.claim_id}"
                connection.execute(
                    """
                    INSERT INTO claims(
                        claim_id, run_id, claim_text, importance, verifiable,
                        verdict, confidence, verifier_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored_claim_id,
                        run_id,
                        item.claim.text,
                        item.claim.importance,
                        int(item.claim.verifiable),
                        item.verdict.value,
                        item.confidence,
                        item.verifier_version,
                    ),
                )
                connection.executemany(
                    "INSERT INTO claim_citations(claim_id, source_id) VALUES (?, ?)",
                    [
                        (stored_claim_id, source_id)
                        for source_id in item.claim.citation_ids
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO claim_evidence(
                        claim_id, chunk_id, verdict, confidence, verifier_version
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            stored_claim_id,
                            chunk_id,
                            item.verdict.value,
                            item.confidence,
                            item.verifier_version,
                        )
                        for chunk_id in item.evidence_chunk_ids
                    ],
                )

    async def is_document_indexed(
        self,
        document_version_id: str,
        embedding_model: str,
        chunker_version: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._is_document_indexed_sync,
            document_version_id,
            embedding_model,
            chunker_version,
        )

    def _is_document_indexed_sync(
        self,
        document_version_id: str,
        embedding_model: str,
        chunker_version: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM document_index_state
                WHERE document_version_id = ? AND embedding_model = ? AND chunker_version = ?
                """,
                (document_version_id, embedding_model, chunker_version),
            ).fetchone()
        return row is not None

    async def mark_document_indexed(
        self,
        document_version_id: str,
        embedding_model: str,
        chunker_version: str,
    ) -> None:
        await asyncio.to_thread(
            self._mark_document_indexed_sync,
            document_version_id,
            embedding_model,
            chunker_version,
        )

    def _mark_document_indexed_sync(
        self,
        document_version_id: str,
        embedding_model: str,
        chunker_version: str,
    ) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO document_index_state(
                    document_version_id, embedding_model, chunker_version, indexed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    document_version_id,
                    embedding_model,
                    chunker_version,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def migrate_legacy_json(self, path: Path) -> bool:
        """Import the old JSON memory exactly once for each distinct payload."""
        if not path.exists():
            return False
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        import_key = f"{path.resolve()}:{hashlib.sha256(raw).hexdigest()}"
        now = datetime.now(UTC).isoformat()
        with self._write_lock, self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM legacy_imports WHERE import_key = ?", (import_key,)
            ).fetchone():
                return False
            topics = [str(item) for item in data.get("recent_topics", []) if item]
            queries = [str(item) for item in data.get("queries", []) if item]
            if not topics and queries:
                topics = ["Imported legacy research memory"]
            for index, topic in enumerate(topics):
                run_id = str(
                    uuid.uuid5(LEGACY_NAMESPACE, f"{import_key}|{index}|{topic}")
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO research_runs(
                        run_id, scope_partition, topic, request_json, status,
                        started_at, finished_at
                    ) VALUES (?, 'global', ?, '{}', 'complete', ?, ?)
                    """,
                    (run_id, topic, now, now),
                )
                if index == 0:
                    connection.executemany(
                        """
                        INSERT INTO research_queries(
                            run_id, original_query, rewritten_query, intent, derived_from
                        ) VALUES (?, ?, ?, 'overview', 'legacy_memory')
                        """,
                        [(run_id, query, query) for query in queries],
                    )
            for url in data.get("sources", {}):
                try:
                    canonical_url = canonicalize_url(str(url))
                except ValueError:
                    continue
                source_id = str(uuid.uuid5(SOURCE_NAMESPACE, canonical_url))
                connection.execute(
                    """
                    INSERT OR IGNORE INTO sources(
                        source_id, canonical_url, domain, first_discovered_at, last_discovered_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        canonical_url,
                        urlsplit(canonical_url).hostname or "",
                        now,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO legacy_imports(import_key, imported_at) VALUES (?, ?)",
                (import_key, now),
            )
        return True

    async def delete_scope(self, scope: MemoryScope) -> list[str]:
        """Delete private memory and return vector document ids to remove."""
        return await asyncio.to_thread(self._delete_scope_sync, scope)

    def _delete_scope_sync(self, scope: MemoryScope) -> list[str]:
        partition = scope.run_partition
        if partition == "global":
            raise ValueError(
                "global research memory requires an explicit admin workflow"
            )
        with self._write_lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT document_version_id FROM document_versions
                WHERE scope_partition = ?
                """,
                (partition,),
            ).fetchall()
            document_ids = [row["document_version_id"] for row in rows]
            connection.execute(
                "DELETE FROM research_runs WHERE scope_partition = ?", (partition,)
            )
            if document_ids:
                placeholders = ",".join("?" for _ in document_ids)
                connection.execute(
                    f"DELETE FROM run_sources WHERE document_version_id IN ({placeholders})",
                    document_ids,
                )
                connection.execute(
                    f"DELETE FROM document_versions WHERE document_version_id IN ({placeholders})",
                    document_ids,
                )
                connection.execute(
                    "DELETE FROM sources WHERE source_id NOT IN "
                    "(SELECT DISTINCT source_id FROM document_versions)"
                )
        return document_ids


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source URL must be an absolute http(s) URL")
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    port = parsed.port
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    return urlunsplit((scheme, hostname, path, urlencode(sorted(query)), ""))


def detect_language(text: str) -> str:
    chinese = sum(1 for character in text[:4000] if "\u4e00" <= character <= "\u9fff")
    latin = sum(
        1 for character in text[:4000] if character.isascii() and character.isalpha()
    )
    if chinese > latin * 0.2:
        return "zh"
    if latin:
        return "en"
    return "unknown"


def _topic_similarity(left: str, right: str) -> float:
    left_tokens = set(tokenize(left))
    right_tokens = set(tokenize(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _row_to_document(row: sqlite3.Row) -> DocumentVersion:
    return DocumentVersion(
        document_version_id=row["document_version_id"],
        source_id=row["source_id"],
        canonical_url=row["canonical_url"],
        title=row["title"],
        text=row["body_text"],
        content_hash=row["content_hash"],
        fetched_at=_parse_datetime(row["fetched_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
        language=row["language"],
        parser_version=row["parser_version"],
        scope_partition=row["scope_partition"],
        http_etag=row["http_etag"],
        http_last_modified=row["http_last_modified"],
    )


def _parse_datetime(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
