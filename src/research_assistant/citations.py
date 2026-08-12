"""Citation parsing and validation."""

from __future__ import annotations

import re


CITATION_RE = re.compile(r"\[(S\d+)\]")


class InvalidCitationError(ValueError):
    def __init__(self, unknown_ids: set[str]):
        self.unknown_ids = unknown_ids
        super().__init__(
            "answer referenced unknown source ids: " + ", ".join(sorted(unknown_ids))
        )


def extract_citations(answer: str) -> set[str]:
    return set(CITATION_RE.findall(answer))


def validate_citations(answer: str, valid_source_ids: set[str]) -> set[str]:
    cited = extract_citations(answer)
    unknown = cited - valid_source_ids
    if unknown:
        raise InvalidCitationError(unknown)
    return cited
