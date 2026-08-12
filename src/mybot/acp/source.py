"""ACP event source type."""

from dataclasses import dataclass

from mybot.core.events import EventSource


@dataclass
class AcpEventSource(EventSource):
    """Source for ACP-originated events."""

    _namespace = "platform-acp"
    session_id: str

    def __str__(self) -> str:
        return f"{self._namespace}:{self.session_id}"

    @classmethod
    def from_string(cls, s: str) -> "AcpEventSource":
        _, session_id = s.split(":", 1)
        return cls(session_id=session_id)

    @property
    def platform_name(self) -> str:
        return "acp"
