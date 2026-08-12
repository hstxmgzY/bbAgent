"""Adapters that inject mybot infrastructure into the research domain service."""

from __future__ import annotations

from pathlib import Path

from mybot.provider.llm import LLMProvider
from mybot.provider.web_search import WebSearchProvider
from research_assistant.embeddings import (
    HashingEmbedder,
    SentenceTransformerEmbedder,
)
from research_assistant.llm import fallback_answer
from research_assistant.models import Chunk, SearchResult, ToolResult
from research_assistant.query_rewriter import DeterministicQueryRewriter
from research_assistant.rag import InMemoryEvidenceIndex
from research_assistant.repositories.sqlite import SqliteMemoryRepository
from research_assistant.service import ResearchService
from research_assistant.telemetry import create_research_telemetry
from research_assistant.tools import SearchTool, WebReadTool
from research_assistant.vectorstores.qdrant import QdrantEvidenceIndex


class ConfiguredSearchBackend:
    def __init__(self, provider: WebSearchProvider):
        self.provider = provider

    async def search(self, query: str, limit: int = 5) -> ToolResult:
        try:
            results = await self.provider.search(query)
            normalized = [
                SearchResult(title=item.title, url=item.url, snippet=item.snippet)
                for item in results[:limit]
            ]
            return ToolResult(ok=True, data=normalized)
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


class ConfiguredAnswerSynthesizer:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def synthesize(
        self, topic: str, evidence: list[Chunk], warnings: list[str]
    ) -> str:
        context = _evidence_context(evidence)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a careful research assistant. Answer in Chinese. "
                    "Use only the provided evidence and cite every factual claim "
                    "with source ids such as [S1]."
                ),
            },
            {
                "role": "user",
                "content": f"Topic: {topic}\n\nEvidence:\n{context}",
            },
        ]
        try:
            content, _, _ = await self.provider.chat(messages, tools=[])
            return content or fallback_answer(topic, evidence, warnings)
        except Exception as exc:
            warnings.append(
                f"LLM synthesis failed ({type(exc).__name__}); used extractive fallback"
            )
            return fallback_answer(topic, evidence, warnings)

    async def repair(
        self,
        topic: str,
        evidence: list[Chunk],
        unsupported_claims: list[str],
        warnings: list[str],
    ) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "Rewrite a conservative Chinese answer using only the supplied "
                    "evidence. Remove unsupported claims and cite every factual claim "
                    "with the exact provided source ids. Return only the repaired answer."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Topic: {topic}\n\nFailed claims:\n"
                    + "\n".join(f"- {claim}" for claim in unsupported_claims)
                    + f"\n\nAllowed evidence:\n{_evidence_context(evidence)}"
                ),
            },
        ]
        try:
            content, _, _ = await self.provider.chat(messages, tools=[])
            return content or fallback_answer(topic, evidence, warnings)
        except Exception as exc:
            warnings.append(
                f"LLM repair failed ({type(exc).__name__}); used extractive fallback"
            )
            return fallback_answer(topic, evidence, warnings)


def create_research_service(config, memory_path: Path) -> ResearchService:
    research = config.research
    search = (
        ConfiguredSearchBackend(WebSearchProvider.from_config(config))
        if config.websearch
        else SearchTool()
    )
    metadata = SqliteMemoryRepository(research.metadata_store.path(config.workspace))
    metadata.migrate_legacy_json(memory_path)

    if research.embedding.provider == "sentence_transformers":
        embedder = SentenceTransformerEmbedder(
            research.embedding.model,
            revision=research.embedding.revision,
            device=research.embedding.device,
            batch_size=research.embedding.batch_size,
            normalize_embeddings=research.embedding.normalize,
            cache_folder=(
                str(research.embedding.cache_folder)
                if research.embedding.cache_folder
                else None
            ),
            local_files_only=research.embedding.local_files_only,
        )
    else:
        embedder = HashingEmbedder(research.embedding.dimensions)

    if research.vector_store.provider == "qdrant":
        vector_store = QdrantEvidenceIndex(
            dimensions=embedder.dimensions,
            embedding_model=embedder.model_id,
            collection_prefix=research.vector_store.collection_prefix,
            path=(
                research.vector_store.path
                if research.vector_store.mode == "local"
                else None
            ),
            url=(
                research.vector_store.url
                if research.vector_store.mode == "remote"
                else None
            ),
            api_key=(
                research.vector_store.api_key.get_secret_value()
                if research.vector_store.api_key
                else None
            ),
        )
    else:
        vector_store = InMemoryEvidenceIndex(embedder.dimensions)

    min_score = research.retrieval.min_score
    if min_score is None and research.embedding.provider == "hashing":
        min_score = 0.0
    return ResearchService(
        search=search,
        reader=WebReadTool(),
        synthesizer=ConfiguredAnswerSynthesizer(LLMProvider.from_config(config.llm)),
        repository=metadata,
        embedder=embedder,
        evidence_index=vector_store,
        query_rewriter=DeterministicQueryRewriter(),
        telemetry=create_research_telemetry(research.telemetry),
        candidate_k=research.retrieval.candidate_k,
        max_per_source=research.retrieval.max_per_source,
        min_score=min_score,
        max_related_runs=research.memory.max_related_runs,
        memory_enabled=research.memory.enabled,
        default_ttl_hours=research.memory.default_ttl_hours,
        official_ttl_hours=research.memory.official_ttl_hours,
        volatile_ttl_hours=research.memory.volatile_ttl_hours,
        citation_evaluation_enabled=research.citation_evaluation.enabled,
        min_coverage=research.citation_evaluation.min_coverage,
        min_support_rate=research.citation_evaluation.min_support_rate,
        max_repairs=research.citation_evaluation.max_repairs,
    )


def _evidence_context(evidence: list[Chunk]) -> str:
    return "\n\n".join(
        f"[{chunk.source_id}] {chunk.title}\nURL: {chunk.url}\n{chunk.text[:1800]}"
        for chunk in evidence
    )
