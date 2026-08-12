"""Simple memory store for short notes, repeated queries, and seen sources."""

import json
import os
import threading
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.data = self._load()

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {
            "recent_topics": [],
            "queries": [],
            "sources": {},
            "preferences": {
                "citation_style": "inline source ids like [S1]",
                "answer_language": "Chinese unless the user asks otherwise",
            },
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_data()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_data()
        defaults = self._empty_data()
        defaults.update(loaded)
        return defaults

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self.data, ensure_ascii=False, indent=2)
            temporary_path: Path | None = None
            try:
                with NamedTemporaryFile(
                    "w", encoding="utf-8", dir=self.path.parent, delete=False
                ) as temporary:
                    temporary.write(payload)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = Path(temporary.name)
                os.replace(temporary_path, self.path)
            finally:
                if temporary_path and temporary_path.exists():
                    temporary_path.unlink()

    def remember_topic(self, topic: str) -> None:
        with self._lock:
            topics = [topic, *[t for t in self.data["recent_topics"] if t != topic]]
            self.data["recent_topics"] = topics[:20]

    def remember_query(self, query: str) -> bool:
        with self._lock:
            is_new = query not in self.data["queries"]
            if is_new:
                self.data["queries"].append(query)
                self.data["queries"] = self.data["queries"][-200:]
            return is_new

    def remember_source(self, url: str, title: str) -> None:
        with self._lock:
            self.data["sources"][url] = title
