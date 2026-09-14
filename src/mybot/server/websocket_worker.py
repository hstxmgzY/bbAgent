"""WebSocket worker for broadcasting events to connected clients."""

import logging
import time
import dataclasses
from collections import defaultdict
from typing import TYPE_CHECKING


from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect
from pydantic import ValidationError, BaseModel, Field

from .worker import SubscriberWorker
from mybot.core.events import (
    Event,
    InboundEvent,
    OutboundEvent,
    WebSocketEventSource,
)

if TYPE_CHECKING:
    from mybot.core.context import SharedContext

logger = logging.getLogger(__name__)


class WebSocketMessage(BaseModel):
    """Incoming WebSocket message from client."""

    source: str = Field(..., min_length=1, description="Client identifier")
    content: str = Field(..., min_length=1, description="Message content")
    agent_id: str | None = Field(
        None, description="Target agent ID (optional - uses routing if not specified)"
    )


class WebSocketWorker(SubscriberWorker):
    """Manages WebSocket connections and event broadcasting."""

    def __init__(self, context: "SharedContext"):
        super().__init__(context)
        self.clients: set[WebSocket] = set()
        self.clients_by_source: dict[str, set[WebSocket]] = defaultdict(set)
        self.source_by_client: dict[WebSocket, str] = {}
        self.authenticated_clients: set[WebSocket] = set()

        # Auto-subscribe to event classes
        for event_class in [
            InboundEvent,
            OutboundEvent,
        ]:
            self.context.eventbus.subscribe(event_class, self.handle_event)
        self.logger.info("WebSocketWorker subscribed to event types")

    async def handle_connection(
        self, ws: WebSocket, authenticated_source: str | None = None
    ) -> None:
        """Handle a single WebSocket connection lifecycle."""
        self.clients.add(ws)
        if authenticated_source:
            self._bind_client(ws, authenticated_source)
            self.authenticated_clients.add(ws)
        self.logger.info(
            f"WebSocket client connected. Total clients: {len(self.clients)}"
        )

        try:
            await self._run_client_loop(ws, authenticated_source)
        finally:
            self._remove_client(ws)
            self.logger.info(
                f"WebSocket client disconnected. Total clients: {len(self.clients)}"
            )

    async def _run_client_loop(
        self, ws: WebSocket, authenticated_source: str | None = None
    ) -> None:
        """Run message receiving loop for a single client."""

        while True:
            try:
                data = await ws.receive_json()
                msg = WebSocketMessage(**data)
                source_key = str(WebSocketEventSource(user_id=msg.source))
                if authenticated_source and source_key != authenticated_source:
                    await ws.close(
                        code=1008, reason="source does not match credentials"
                    )
                    return
                if not authenticated_source:
                    self._bind_client(ws, source_key)

                event = self._normalize_message(msg)

                await self.context.eventbus.publish(event)
                self.logger.debug(f"Emitted InboundEvent from WebSocket: {msg.source}")

            except WebSocketDisconnect:
                self.logger.info("Client disconnected normally")
                break
            except ValidationError as e:
                await ws.send_json(
                    {"type": "error", "message": f"Validation error: {e}"}
                )
                self.logger.warning(f"Validation error from client: {e}")
            except Exception as e:
                self.logger.error(f"Unexpected error in client loop: {e}")
                break

    def _normalize_message(self, msg: "WebSocketMessage") -> InboundEvent:
        """Normalize WebSocketMessage to InboundEvent."""
        source = WebSocketEventSource(user_id=msg.source)

        agent_id = msg.agent_id
        if agent_id is None:
            agent_id = self.context.routing_table.resolve(str(source))

        session_id = self.context.routing_table.get_or_create_session_id(source)

        return InboundEvent(
            session_id=session_id,
            source=source,
            content=msg.content,
            timestamp=time.time(),
        )

    async def handle_event(self, event: Event) -> None:
        """Send an event only to connections bound to its platform source."""
        source_key = self._event_source(event)
        if source_key is None:
            return
        clients = self.clients_by_source.get(source_key, set())
        if isinstance(event, OutboundEvent) and event.event_id:
            clients = clients.intersection(self.authenticated_clients)
        if not clients:
            return

        # Serialize event to dict with type information
        event_dict = {
            "type": event.__class__.__name__,
        }
        event_dict.update(dataclasses.asdict(event))

        # Convert EventSource to string for JSON serialization
        if "source" in event_dict and hasattr(event.source, "__str__"):
            event_dict["source"] = str(event.source)

        self.logger.debug(
            "Sending %s to %d source-bound clients",
            event.__class__.__name__,
            len(clients),
        )

        for client in list(clients):
            try:
                await client.send_json(event_dict)
            except Exception as e:
                self.logger.error(f"Failed to send to client: {e}")
                self._remove_client(client)

    def _event_source(self, event: Event) -> str | None:
        if isinstance(event, InboundEvent) and isinstance(
            event.source, WebSocketEventSource
        ):
            return str(event.source)
        if isinstance(event, OutboundEvent):
            session = self.context.history_store.get_session_info(event.session_id)
            if session and session.source.startswith("platform-ws:"):
                return session.source
        return None

    def _bind_client(self, ws: WebSocket, source_key: str) -> None:
        previous = self.source_by_client.get(ws)
        if previous == source_key:
            return
        if previous:
            self.clients_by_source[previous].discard(ws)
        self.source_by_client[ws] = source_key
        self.clients_by_source[source_key].add(ws)

    def _remove_client(self, ws: WebSocket) -> None:
        self.clients.discard(ws)
        self.authenticated_clients.discard(ws)
        source_key = self.source_by_client.pop(ws, None)
        if source_key:
            clients = self.clients_by_source.get(source_key)
            if clients is not None:
                clients.discard(ws)
                if not clients:
                    self.clients_by_source.pop(source_key, None)
