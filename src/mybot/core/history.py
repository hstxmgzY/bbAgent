"""JSONL file-based conversation history backend."""

import os
import threading
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

from mybot.core.events import EventSource
from litellm.types.completion import ChatCompletionMessageParam as Message


if TYPE_CHECKING:
    from mybot.utils.config import Config


def _now_iso() -> str:
    """Return current datetime as ISO format string."""
    return datetime.now().isoformat()


class HistorySession(BaseModel):
    """Session metadata - stored in index.jsonl."""

    id: str
    agent_id: str
    source: str  # Serialized EventSource (e.g., "platform-telegram:123:456")
    title: str | None = None
    message_count: int = 0
    created_at: str
    updated_at: str

    @field_validator("source", mode="before")
    @classmethod
    def parse_source(cls, v: Any) -> str:
        if hasattr(v, "__str__"):
            return str(v)
        return v

    def get_source(self) -> EventSource:
        """Get the session's EventSource."""
        return EventSource.from_string(self.source)


class HistoryMessage(BaseModel):
    """Single message - stored in session.jsonl."""

    timestamp: str = Field(default_factory=_now_iso)
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_message(
        cls, message: Message, metadata: dict[str, Any] | None = None
    ) -> "HistoryMessage":
        """Create HistoryMessage from litellm Message format."""
        tool_calls = None
        if message.get("tool_calls"):
            tool_calls = [
                {
                    "id": tc.get("id"),
                    "type": tc.get("type", "function"),
                    "function": tc.get("function", {}),
                }
                for tc in message["tool_calls"]
            ]

        tool_call_id = message.get("tool_call_id")

        return cls(
            role=message["role"],
            content=str(message.get("content", "")),
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            metadata=metadata or {},
        )

    def to_message(self) -> Message:
        """Convert HistoryMessage to litellm Message format."""
        base: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }

        if self.role == "assistant" and self.tool_calls:
            return {
                "role": "assistant",
                "content": self.content,
                "tool_calls": self.tool_calls,
            }

        if self.role == "tool" and self.tool_call_id:
            base["tool_call_id"] = self.tool_call_id
            return base

        return base


class HistoryStore:
    """JSONL file-based history storage."""

    @staticmethod
    def from_config(config: "Config") -> "HistoryStore":
        return HistoryStore(config.history_path)

    def __init__(self, base_path: Path):
        self.base_path = Path(base_path)
        self.sessions_path = self.base_path / "sessions"
        self.index_path = self.base_path / "index.jsonl"
        self._lock = threading.RLock()

        self.base_path.mkdir(parents=True, exist_ok=True)
        self.sessions_path.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        """Get the file path for a session."""
        return self.sessions_path / f"{session_id}.jsonl"

    def _read_index(self) -> list[HistorySession]:
        """Read all session entries from index.jsonl."""
        with self._lock:
            if not self.index_path.exists():
                return []

            sessions = []
            with open(self.index_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            sessions.append(HistorySession.model_validate_json(line))
                        except Exception:
                            continue
            return sessions

    def _write_index(self, sessions: list[HistorySession]) -> None:
        """Atomically replace all session entries in index.jsonl."""
        with self._lock:
            temporary_path: Path | None = None
            try:
                with NamedTemporaryFile(
                    "w", encoding="utf-8", dir=self.base_path, delete=False
                ) as temporary:
                    for session in sessions:
                        temporary.write(session.model_dump_json() + "\n")
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = Path(temporary.name)
                os.replace(temporary_path, self.index_path)
            finally:
                if temporary_path and temporary_path.exists():
                    temporary_path.unlink()

    def _find_session_index(
        self, sessions: list[HistorySession], session_id: str
    ) -> int:
        """Find the index of a session in the list."""
        for i, s in enumerate(sessions):
            if s.id == session_id:
                return i
        return -1

    def create_session(
        self,
        agent_id: str,
        session_id: str,
        source: "EventSource",
    ) -> dict[str, Any]:
        """Create a new conversation session."""
        with self._lock:
            sessions = self._read_index()
            existing_index = self._find_session_index(sessions, session_id)
            if existing_index >= 0:
                return sessions[existing_index].model_dump()

            now = _now_iso()
            session = HistorySession(
                id=session_id,
                agent_id=agent_id,
                source=source,
                title=None,
                message_count=0,
                created_at=now,
                updated_at=now,
            )
            sessions.append(session)
            self._write_index(sessions)
            self._session_path(session_id).touch()
            return session.model_dump()

    def save_message(self, session_id: str, message: HistoryMessage) -> None:
        """Save a message to history."""
        with self._lock:
            sessions = self._read_index()
            idx = self._find_session_index(sessions, session_id)
            if idx < 0:
                raise ValueError(f"Session not found: {session_id}")

            session = sessions[idx]
            session_file = self._session_path(session_id)
            with open(session_file, "a", encoding="utf-8") as f:
                f.write(message.model_dump_json() + "\n")
                f.flush()
                os.fsync(f.fileno())

            session.message_count += 1
            session.updated_at = _now_iso()
            if session.title is None and message.role == "user":
                title = message.content[:50]
                if len(message.content) > 50:
                    title += "..."
                session.title = title

            sessions.sort(key=lambda s: s.updated_at, reverse=True)
            self._write_index(sessions)

    def list_sessions(self) -> list[HistorySession]:
        """List all sessions, most recently updated first."""
        sessions = self._read_index()
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    def get_messages(self, session_id: str) -> list[HistoryMessage]:
        """Get all messages for a session."""
        session_file = self._session_path(session_id)
        if not session_file.exists():
            return []

        messages: list[HistoryMessage] = []
        with self._lock, open(session_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(HistoryMessage.model_validate_json(line))
                    except Exception:
                        continue

        return messages

    def get_session_info(self, session_id: str) -> HistorySession | None:
        """Get session metadata without loading messages."""
        sessions = self._read_index()
        for session in sessions:
            if session.id == session_id:
                return session
        return None
