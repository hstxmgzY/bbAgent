"""FastAPI application with WebSocket support."""

import hashlib
import hmac

from fastapi import FastAPI, WebSocket, WebSocketException, status
from fastapi.middleware.cors import CORSMiddleware

from mybot.core.context import SharedContext


def create_app(context: SharedContext) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="MyBot WebSocket Server",
        description="WebSocket server for real-time agent communication",
        version="0.1.0",
    )
    app.state.context = context

    # Enable CORS for web clients
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # WebSocket endpoint
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time event streaming and chat."""
        authenticated_source: str | None = None
        auth_secret = context.config.api.websocket_auth_secret
        if auth_secret is not None:
            source = websocket.query_params.get("source")
            token = websocket.query_params.get("token")
            if not source or not token:
                raise WebSocketException(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="source and token are required",
                )
            expected = hmac.new(
                auth_secret.get_secret_value().encode(),
                source.encode(),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(token, expected):
                raise WebSocketException(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="invalid WebSocket credentials",
                )
            authenticated_source = f"platform-ws:{source}"

        await websocket.accept()

        # Check if WebSocket worker is available
        if context.websocket_worker is None:
            await websocket.close(code=1013, reason="WebSocket not available")
            return

        # Hand off to worker
        await context.websocket_worker.handle_connection(
            websocket, authenticated_source=authenticated_source
        )

    return app
