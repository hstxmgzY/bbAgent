"""Injectable research workflow with persistent evidence and quality gates."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from research_assistant.citations import extract_citations
from research_assistant.claims import CitationEvaluator
from research_assistant.embeddings import HashingEmbedder
from research_assistant.llm import fallback_answer
from research_assistant.memory_context import MemoryContextBuilder
from research_assistant.models import (
    CitationEvaluation,
    DocumentVersion,
    IndexedChunk,
    QueryRewriteRequest,
    ResearchQuery,
    ResearchQuality,
    ResearchReport,
    ResearchRequest,
    ResearchRun,
    RetrievalRequest,
    SearchResult,
    SourceDocument,
    ToolResult,
)
from research_assistant.ports import (
    AnswerSynthesizer,
    DocumentReader,
    Embedder,
    EvidenceIndex,
    MemoryRepository,
    QueryRewriter,
    ResearchRepository,
    SearchBackend,
)
from research_assistant.query_rewriter import DeterministicQueryRewriter
from research_assistant.rag import (
    CHUNKER_VERSION,
    InMemoryEvidenceIndex,
    _select_with_source_quota,
    chunk_text,
)
from research_assistant.telemetry import ResearchTelemetry, classify_error


class ResearchService:
    def __init__(
        self,
        search: SearchBackend,
        reader: DocumentReader,
        synthesizer: AnswerSynthesizer,
        repository: ResearchRepository | MemoryRepository,
        *,
        embedder: Embedder | None = None,
        evidence_index: EvidenceIndex | None = None,
        memory_repository: MemoryRepository | None = None,
        query_rewriter: QueryRewriter | None = None,
        citation_evaluator: CitationEvaluator | None = None,
        telemetry: ResearchTelemetry | None = None,
        candidate_k: int = 30,
        max_per_source: int = 2,
        min_score: float | None = 0.0,
        max_related_runs: int = 5,
        memory_enabled: bool = True,
        default_ttl_hours: int = 168,
        official_ttl_hours: int = 720,
        volatile_ttl_hours: int = 6,
        citation_evaluation_enabled: bool = True,
        min_coverage: float = 0.85,
        min_support_rate: float = 0.80,
        max_repairs: int = 1,
    ) -> None:
        self.search = search
        self.reader = reader
        self.synthesizer = synthesizer
        self.legacy_repository: ResearchRepository | None = None
        if memory_repository is not None:
            self.memory_repository = memory_repository
            if not hasattr(repository, "begin_run"):
                self.legacy_repository = repository  # type: ignore[assignment]
        elif hasattr(repository, "begin_run"):
            self.memory_repository = repository  # type: ignore[assignment]
        else:
            self.memory_repository = None
            self.legacy_repository = repository  # type: ignore[assignment]
        self.repository = repository
        self.embedder = embedder or HashingEmbedder()
        self.evidence_index = evidence_index or InMemoryEvidenceIndex(
            self.embedder.dimensions
        )
        self.query_rewriter = query_rewriter or DeterministicQueryRewriter()
        self.citation_evaluator = citation_evaluator or CitationEvaluator()
        self.telemetry = telemetry or ResearchTelemetry(enabled=False)
        self.candidate_k = candidate_k
        self.max_per_source = max_per_source
        self.min_score = min_score
        self.memory_enabled = memory_enabled
        self.memory_context = (
            MemoryContextBuilder(self.memory_repository, max_related_runs)
            if self.memory_repository and memory_enabled
            else None
        )
        self.default_ttl = timedelta(hours=default_ttl_hours)
        self.official_ttl = timedelta(hours=official_ttl_hours)
        self.volatile_ttl = timedelta(hours=volatile_ttl_hours)
        self.citation_evaluation_enabled = citation_evaluation_enabled
        self.min_coverage = min_coverage
        self.min_support_rate = min_support_rate
        self.max_repairs = max(0, min(1, max_repairs))

    async def research(self, request: ResearchRequest) -> ResearchReport:
        self._validate_request(request)
        topic = request.topic.strip()
        normalized_request = ResearchRequest(
            topic=topic,
            max_queries=request.max_queries,
            max_sources=request.max_sources,
            top_k=request.top_k,
            refresh=request.refresh,
            session_id=request.session_id,
            user_scope=request.user_scope,
        )
        run: ResearchRun | None = None
        async with self.telemetry.stage(
            "research.run", {"refresh": normalized_request.refresh}
        ):
            try:
                if self.memory_repository:
                    run = await self.memory_repository.begin_run(normalized_request)
                report, evaluation = await self._run_pipeline(normalized_request, run)
                if self.memory_repository and run:
                    async with self.telemetry.stage("memory.commit"):
                        if evaluation:
                            await self.memory_repository.save_evaluation(
                                run.run_id, evaluation
                            )
                        quality_json = json.dumps(
                            asdict(report.quality) if report.quality else {},
                            ensure_ascii=False,
                        )
                        await self.memory_repository.finish_run(
                            run.run_id, report, quality_json
                        )
                self.telemetry.runs.add(
                    1,
                    {
                        "status": report.status,
                        "refresh": normalized_request.refresh,
                    },
                )
                return report
            except BaseException:
                if self.memory_repository and run:
                    await self.memory_repository.fail_run(run.run_id)
                self.telemetry.runs.add(
                    1,
                    {"status": "failed", "refresh": normalized_request.refresh},
                )
                raise

    async def delete_memory(self, request: ResearchRequest) -> None:
        """Delete all private metadata and vector evidence for a request scope."""
        if not self.memory_repository:
            raise RuntimeError("persistent research memory is not configured")
        delete_scope = getattr(self.memory_repository, "delete_scope", None)
        if not callable(delete_scope):
            raise RuntimeError("the configured memory repository cannot delete scopes")
        document_ids = await delete_scope(request.memory_scope)
        for document_version_id in document_ids:
            await self.evidence_index.delete_document_version(document_version_id)

    async def close(self) -> None:
        for component in (self.evidence_index, self.embedder):
            close = getattr(component, "close", None)
            if not callable(close):
                continue
            result = close()
            if inspect.isawaitable(result):
                await result
        shutdown = getattr(self.telemetry, "shutdown", None)
        if callable(shutdown):
            shutdown()

    async def _run_pipeline(
        self, request: ResearchRequest, run: ResearchRun | None
    ) -> tuple[ResearchReport, CitationEvaluation | None]:
        warnings: list[str] = []
        if self.legacy_repository:
            self.legacy_repository.remember_topic(request.topic)

        related_runs = []
        if self.memory_context:
            async with self.telemetry.stage("memory.load_context"):
                related_runs = await self.memory_context.build(
                    request.topic, request.memory_scope
                )
        async with self.telemetry.stage("query.rewrite"):
            queries = await self.query_rewriter.rewrite(
                QueryRewriteRequest(
                    topic=request.topic,
                    max_queries=request.max_queries,
                    related_runs=tuple(related_runs),
                )
            )
        if self.legacy_repository:
            for query in queries:
                self.legacy_repository.remember_query(query.query)

        async with self.telemetry.stage("search.batch"):
            search_results = await self._search_all(
                queries, warnings, request.max_sources, run
            )
        documents = await self._resolve_documents(search_results, request, warnings)
        await self._index_documents(documents)
        evidence = await self._retrieve(queries, request)
        sources, evidence = await self._bind_citations(documents, evidence, request)

        evaluation: CitationEvaluation | None = None
        if evidence:
            async with self.telemetry.stage("generation.answer"):
                answer = await self.synthesizer.synthesize(
                    request.topic, evidence, warnings
                )
            self.telemetry.generation.add(
                1,
                {
                    "model": type(self.synthesizer).__name__,
                    "status": "ok",
                    "fallback": False,
                },
            )
            answer, evaluation = await self._evaluate_and_repair(
                request.topic, answer, sources, evidence, warnings
            )
        else:
            answer = fallback_answer(request.topic, evidence, warnings)

        quality = (
            evaluation.quality
            if evaluation
            else ResearchQuality(
                evaluated_claims=0,
                evaluator_version=self.citation_evaluator.version,
            )
        )
        status = (
            "complete"
            if evidence and (not evaluation or self._passes_quality(evaluation))
            else "insufficient_evidence"
        )
        report = ResearchReport(
            topic=request.topic,
            answer=answer,
            sources=sources,
            evidence=evidence,
            warnings=warnings,
            status=status,
            quality=quality,
            run_id=run.run_id if run else None,
        )
        if self.legacy_repository:
            for document in sources:
                self.legacy_repository.remember_source(document.url, document.title)
            self.legacy_repository.save()
        return report, evaluation

    async def _search_all(
        self,
        queries: list[ResearchQuery],
        warnings: list[str],
        max_sources: int,
        run: ResearchRun | None,
    ) -> list[SearchResult]:
        async def execute(query: ResearchQuery) -> list[SearchResult]:
            started = time.perf_counter()
            error_type: str | None = None
            found: list[SearchResult] = []
            async with self.telemetry.stage(
                "search.query", {"provider": type(self.search).__name__}
            ):
                try:
                    outcome = await self.search.search(query.query, limit=max_sources)
                    if not isinstance(outcome, ToolResult) or not outcome.ok:
                        error = (
                            outcome.error
                            if isinstance(outcome, ToolResult)
                            else "invalid result"
                        )
                        error_type = classify_error(error)
                        warnings.append(f"search failed for '{query.query}': {error}")
                    elif not isinstance(outcome.data or [], list):
                        error_type = "invalid_result"
                        warnings.append(
                            f"search returned an invalid result for '{query.query}'"
                        )
                    else:
                        found = [
                            item
                            for item in (outcome.data or [])
                            if isinstance(item, SearchResult)
                        ]
                        if not found:
                            warnings.append(
                                f"search returned no results for '{query.query}'"
                            )
                except BaseException as exc:
                    error_type = classify_error(exc)
                    warnings.append(
                        f"search failed for '{query.query}': {type(exc).__name__}"
                    )
            duration = time.perf_counter() - started
            self.telemetry.search_results.record(
                len(found), {"provider": type(self.search).__name__}
            )
            if self.memory_repository and run:
                await self.memory_repository.save_query(
                    run.run_id,
                    query,
                    result_count=len(found),
                    duration_seconds=duration,
                    error_type=error_type,
                )
            return found

        outcomes = await asyncio.gather(*(execute(query) for query in queries))
        combined = [item for outcome in outcomes for item in outcome]
        return self._dedupe(combined)[: max_sources * 2]

    async def _resolve_documents(
        self,
        results: list[SearchResult],
        request: ResearchRequest,
        warnings: list[str],
    ) -> list[SourceDocument]:
        if not self.memory_repository:
            return await self._read_without_persistence(
                results, warnings, request.max_sources
            )

        documents: list[DocumentVersion] = []
        misses: list[SearchResult] = []
        now = datetime.now(UTC)
        async with self.telemetry.stage("source.resolve_freshness"):
            for result in results:
                if len(documents) + len(misses) >= request.max_sources:
                    break
                cached = None
                if self.memory_enabled and not request.refresh:
                    try:
                        cached = await self.memory_repository.get_fresh_document(
                            result.url, request.memory_scope, now
                        )
                    except ValueError:
                        cached = None
                if cached:
                    documents.append(cached)
                    self.telemetry.cache.add(1, {"cache_status": "hit"})
                else:
                    misses.append(result)
                    self.telemetry.cache.add(
                        1,
                        {"cache_status": "stale" if request.refresh else "miss"},
                    )

        async with self.telemetry.stage("read.batch"):
            outcomes = await asyncio.gather(
                *(self._read_one(result) for result in misses),
                return_exceptions=True,
            )
        for result, outcome in zip(misses, outcomes):
            if len(documents) >= request.max_sources:
                break
            if isinstance(outcome, BaseException):
                warnings.append(
                    f"read failed for {result.url}: {type(outcome).__name__}"
                )
                self.telemetry.reads.add(
                    1,
                    {
                        "status": "error",
                        "error_type": classify_error(outcome),
                    },
                )
                continue
            if not outcome.ok or not isinstance(outcome.data, dict):
                warnings.append(f"read failed for {result.url}: {outcome.error}")
                self.telemetry.reads.add(
                    1,
                    {
                        "status": "error",
                        "error_type": classify_error(outcome.error),
                    },
                )
                continue
            text = str(outcome.data.get("text") or result.snippet).strip()
            if not text:
                warnings.append(f"read failed for {result.url}: empty readable content")
                self.telemetry.reads.add(
                    1, {"status": "error", "error_type": "parse_empty"}
                )
                continue
            resolved_url = str(outcome.data.get("url") or result.url)
            fetched_at = datetime.now(UTC)
            version = await self.memory_repository.save_document(
                url=resolved_url,
                title=str(outcome.data.get("title") or result.title),
                text=text,
                fetched_at=fetched_at,
                expires_at=fetched_at + self._ttl_for_url(resolved_url),
                scope_partition="global",
            )
            documents.append(version)
            self.telemetry.reads.add(1, {"status": "ok"})
        if not documents:
            warnings.append("no readable sources remained after filtering")
        return [document.to_source_document() for document in documents]

    async def _read_without_persistence(
        self,
        results: list[SearchResult],
        warnings: list[str],
        max_sources: int,
    ) -> list[SourceDocument]:
        outcomes = await asyncio.gather(
            *(self._read_one(result) for result in results),
            return_exceptions=True,
        )
        documents: list[SourceDocument] = []
        for result, outcome in zip(results, outcomes):
            if len(documents) >= max_sources:
                break
            if isinstance(outcome, BaseException):
                warnings.append(
                    f"read failed for {result.url}: {type(outcome).__name__}"
                )
                continue
            if not outcome.ok or not isinstance(outcome.data, dict):
                warnings.append(f"read failed for {result.url}: {outcome.error}")
                continue
            text = str(outcome.data.get("text") or result.snippet).strip()
            if not text:
                warnings.append(f"read failed for {result.url}: empty readable content")
                continue
            source_id = f"S{len(documents) + 1}"
            now = datetime.now(UTC)
            content_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            documents.append(
                SourceDocument(
                    source_id=source_id,
                    persistent_source_id=source_id,
                    title=str(outcome.data.get("title") or result.title),
                    url=str(outcome.data.get("url") or result.url),
                    text=text,
                    document_version_id=f"legacy-{content_hash[7:39]}",
                    content_hash=content_hash,
                    fetched_at=now,
                    expires_at=now + self.default_ttl,
                )
            )
        if not documents:
            warnings.append("no readable sources remained after filtering")
        return documents

    async def _read_one(self, result: SearchResult) -> ToolResult:
        async with self.telemetry.stage(
            "read.source", {"provider": type(self.reader).__name__}
        ):
            return await self.reader.read(result.url)

    async def _index_documents(self, documents: list[SourceDocument]) -> None:
        chunks_by_document: list[tuple[SourceDocument, list[Any]]] = []
        async with self.telemetry.stage("chunk.documents"):
            for document in documents:
                if (
                    self.memory_repository
                    and self.evidence_index.persistent
                    and document.document_version_id
                    and await self.memory_repository.is_document_indexed(
                        document.document_version_id,
                        self.embedder.model_id,
                        CHUNKER_VERSION,
                    )
                ):
                    continue
                chunks = chunk_text(document)
                if chunks:
                    chunks_by_document.append((document, chunks))
        all_chunks = [chunk for _, chunks in chunks_by_document for chunk in chunks]
        if not all_chunks:
            return
        async with self.telemetry.stage(
            "embedding.documents", {"embedding_model": self.embedder.model_id}
        ):
            vectors = await self.embedder.embed_documents(
                [chunk.text for chunk in all_chunks]
            )
        indexed: list[IndexedChunk] = []
        for chunk, vector in zip(all_chunks, vectors):
            if not chunk.document_version_id or not chunk.content_hash:
                raise RuntimeError(
                    "indexable chunks require a document version and hash"
                )
            indexed.append(
                IndexedChunk(
                    chunk=chunk,
                    vector=vector,
                    document_version_id=chunk.document_version_id,
                    persistent_source_id=chunk.source_id,
                    embedding_model=self.embedder.model_id,
                    scope_partition=chunk.scope_partition,
                    fetched_at=chunk.fetched_at or datetime.now(UTC),
                    expires_at=chunk.expires_at or datetime.now(UTC) + self.default_ttl,
                    content_hash=chunk.content_hash,
                    chunker_version=CHUNKER_VERSION,
                )
            )
        async with self.telemetry.stage("vector.upsert"):
            await self.evidence_index.upsert(indexed)
        self.telemetry.chunks_indexed.add(
            len(indexed), {"embedding_model": self.embedder.model_id}
        )
        if self.memory_repository and self.evidence_index.persistent:
            for document, _ in chunks_by_document:
                if document.document_version_id:
                    await self.memory_repository.mark_document_indexed(
                        document.document_version_id,
                        self.embedder.model_id,
                        CHUNKER_VERSION,
                    )

    async def _retrieve(
        self, queries: list[ResearchQuery], request: ResearchRequest
    ) -> list[Any]:
        async with self.telemetry.stage("retrieval.query"):
            query_vectors = await asyncio.gather(
                *(self.embedder.embed_query(query.query) for query in queries)
            )
            outcomes = await asyncio.gather(
                *(
                    self.evidence_index.search(
                        RetrievalRequest(
                            query_vector=vector,
                            embedding_model=self.embedder.model_id,
                            scope=request.memory_scope,
                            candidate_k=self.candidate_k,
                            top_k=self.candidate_k,
                            max_per_source=self.max_per_source,
                            min_score=self.min_score,
                        )
                    )
                    for vector in query_vectors
                )
            )
        merged: dict[str, Any] = {}
        for outcome in outcomes:
            for chunk in outcome:
                existing = merged.get(chunk.chunk_id)
                if existing is None or chunk.score > existing.score:
                    merged[chunk.chunk_id] = chunk
        ranked = sorted(merged.values(), key=lambda item: item.score, reverse=True)
        selected = _select_with_source_quota(
            ranked,
            top_k=request.top_k,
            max_per_source=self.max_per_source,
        )
        for chunk in selected:
            self.telemetry.retrieval_score.record(
                chunk.score, {"provider": type(self.evidence_index).__name__}
            )
        return selected

    async def _bind_citations(
        self,
        current_documents: list[SourceDocument],
        evidence: list[Any],
        request: ResearchRequest,
    ) -> tuple[list[SourceDocument], list[Any]]:
        documents_by_version = {
            document.document_version_id: document
            for document in current_documents
            if document.document_version_id
        }
        if self.memory_repository:
            for chunk in evidence:
                version_id = chunk.document_version_id
                if not version_id or version_id in documents_by_version:
                    continue
                version = await self.memory_repository.get_document_version(
                    version_id, request.memory_scope
                )
                if version:
                    documents_by_version[version_id] = version.to_source_document()

        ordered_documents = list(current_documents)
        current_versions = {
            document.document_version_id for document in current_documents
        }
        ordered_documents.extend(
            document
            for version_id, document in documents_by_version.items()
            if version_id not in current_versions
        )
        citation_by_source: dict[str, str] = {}
        sources: list[SourceDocument] = []
        for document in ordered_documents:
            persistent_id = document.persistent_source_id or document.source_id
            if persistent_id in citation_by_source:
                continue
            citation_id = f"S{len(sources) + 1}"
            citation_by_source[persistent_id] = citation_id
            values = vars(document).copy()
            values["source_id"] = citation_id
            sources.append(SourceDocument(**values))

        bound_evidence = []
        for chunk in evidence:
            citation_id = citation_by_source.get(chunk.source_id)
            if not citation_id:
                continue
            values = vars(chunk).copy()
            values["source_id"] = citation_id
            bound_evidence.append(type(chunk)(**values))
        return sources, bound_evidence

    async def _evaluate_and_repair(
        self,
        topic: str,
        answer: str,
        sources: list[SourceDocument],
        evidence: list[Any],
        warnings: list[str],
    ) -> tuple[str, CitationEvaluation | None]:
        valid_ids = {source.source_id for source in sources}
        if not self.citation_evaluation_enabled:
            unknown = extract_citations(answer) - valid_ids
            if unknown or not extract_citations(answer):
                answer = fallback_answer(topic, evidence, warnings)
            return answer, None

        async with self.telemetry.stage("citation.evaluate"):
            evaluation = await self.citation_evaluator.evaluate(
                answer, evidence, valid_ids
            )
        if evaluation.unknown_citation_ids:
            warnings.append(
                "rejected invalid citations: "
                + ", ".join(sorted(evaluation.unknown_citation_ids))
            )
            self.telemetry.citation_failures.add(1, {"error_type": "invalid_result"})
        elif not extract_citations(answer):
            warnings.append(
                "generated answer contained no citations; used extractive fallback"
            )
            self.telemetry.citation_failures.add(1, {"error_type": "invalid_result"})
        elif not self._passes_quality(evaluation):
            warnings.append(
                "citation quality gate failed; attempted one conservative repair"
            )

        if not self._passes_quality(evaluation) and self.max_repairs:
            repair = getattr(self.synthesizer, "repair", None)
            if callable(repair):
                async with self.telemetry.stage(
                    "generation.answer", {"fallback": True}
                ):
                    answer = await repair(
                        topic,
                        evidence,
                        evaluation.quality.unsupported_claims,
                        warnings,
                    )
            else:
                answer = fallback_answer(topic, evidence, warnings)
            async with self.telemetry.stage("citation.evaluate"):
                evaluation = await self.citation_evaluator.evaluate(
                    answer, evidence, valid_ids
                )

        if not self._passes_quality(evaluation):
            answer = fallback_answer(topic, evidence, warnings)
            async with self.telemetry.stage("citation.evaluate"):
                evaluation = await self.citation_evaluator.evaluate(
                    answer, evidence, valid_ids
                )
        self.telemetry.record_quality(evaluation.quality)
        return answer, evaluation

    def _passes_quality(self, evaluation: CitationEvaluation) -> bool:
        quality = evaluation.quality
        return (
            not evaluation.has_hard_failure
            and quality.claim_coverage >= self.min_coverage
            and quality.claim_support_rate >= self.min_support_rate
        )

    def _ttl_for_url(self, url: str) -> timedelta:
        lowered = url.casefold()
        if any(marker in lowered for marker in ("/news", "news.", "/latest", "/live")):
            return self.volatile_ttl
        if any(
            marker in lowered
            for marker in (".gov", ".edu", "docs.", "/docs/", "github.com")
        ):
            return self.official_ttl
        return self.default_ttl

    @staticmethod
    def _validate_request(request: ResearchRequest) -> None:
        if not request.topic.strip():
            raise ValueError("topic must not be empty")
        if not 1 <= request.max_queries <= 10:
            raise ValueError("max_queries must be between 1 and 10")
        if not 1 <= request.max_sources <= 10:
            raise ValueError("max_sources must be between 1 and 10")
        if not 1 <= request.top_k <= 30:
            raise ValueError("top_k must be between 1 and 30")

    @staticmethod
    def _dedupe(results: list[SearchResult]) -> list[SearchResult]:
        seen: set[str] = set()
        deduped: list[SearchResult] = []
        for result in results:
            if not result.url or result.url in seen:
                continue
            seen.add(result.url)
            deduped.append(result)
        return deduped
