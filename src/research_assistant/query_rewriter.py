"""Bounded deterministic query rewriting with experience-memory hints."""

from __future__ import annotations

import re
import unicodedata

from research_assistant.models import QueryRewriteRequest, ResearchQuery


class DeterministicQueryRewriter:
    async def rewrite(self, request: QueryRewriteRequest) -> list[ResearchQuery]:
        if not 1 <= request.max_queries <= 10:
            raise ValueError("max_queries must be between 1 and 10")
        candidates = [
            ResearchQuery(request.topic, "overview", "current_topic"),
            ResearchQuery(f"{request.topic} overview", "overview", "current_topic"),
            ResearchQuery(
                f"{request.topic} official documentation OR paper",
                "official",
                "current_topic",
            ),
            ResearchQuery(
                f"{request.topic} best practices", "best_practices", "current_topic"
            ),
        ]
        for hit in request.related_runs:
            for historical in hit.queries:
                candidates.append(
                    ResearchQuery(
                        historical.query,
                        historical.intent,
                        "historical_success",
                    )
                )

        deduped: list[ResearchQuery] = []
        seen: set[str] = set()
        for candidate in candidates:
            query = re.sub(r"\s+", " ", candidate.query).strip()
            key = unicodedata.normalize("NFKC", query).casefold()
            if not query or key in seen:
                continue
            seen.add(key)
            deduped.append(
                ResearchQuery(query, candidate.intent, candidate.derived_from)
            )
            if len(deduped) >= request.max_queries:
                break
        return deduped
