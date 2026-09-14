"""Minimal ACP stdio adapter for my-bot.

ACP uses newline-delimited JSON-RPC over stdio. This module keeps stdout
reserved for protocol messages and sends logs/errors through stderr/log files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from mybot.acp.source import AcpEventSource
from mybot.core.agent import Agent, AgentSession
from mybot.core.context import SharedContext
from mybot.core.history import HistoryMessage
from mybot.server import (
    AgentWorker,
    CompletionOutboxWorker,
    CompletionRouter,
    DispatchJobWorker,
    Worker,
)
from mybot.utils.config import Config

JSON = dict[str, Any]
SUPPORTED_PROTOCOL_VERSIONS = {1, 2}
DEFAULT_PROTOCOL_VERSION = 2

logger = logging.getLogger(__name__)


@dataclass
class AcpRuntime:
    """Owns the my-bot runtime needed behind an ACP connection."""

    config: Config
    context: SharedContext = field(init=False)
    workers: list[Worker] = field(default_factory=list, init=False)
    sessions: dict[str, AgentSession] = field(default_factory=dict)
    session_cwds: dict[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.context = SharedContext(config=self.config, channels=[])
        self.workers = [
            self.context.eventbus,
            AgentWorker(self.context),
            DispatchJobWorker(self.context),
            CompletionOutboxWorker(self.context),
            CompletionRouter(self.context),
        ]

    def start(self) -> None:
        for worker in self.workers:
            worker.start()

    async def stop(self) -> None:
        for worker in self.workers:
            await worker.stop()
        self.context.dispatch_repository.close()

    def new_session(self, cwd: Path, agent_id: str | None = None) -> AgentSession:
        session_id = str(uuid.uuid4())
        agent_def = self.context.agent_loader.load(
            agent_id or self.config.default_agent
        )
        agent = Agent(agent_def, self.context)
        source = AcpEventSource(session_id=session_id)
        session = agent.new_session(source, session_id=session_id)
        self._register_session(session, cwd)
        return session

    def load_session(self, session_id: str, cwd: Path) -> AgentSession:
        session_info = self.context.history_store.get_session_info(session_id)
        if session_info is None:
            raise ValueError(f"Session not found: {session_id}")

        agent_def = self.context.agent_loader.load(session_info.agent_id)
        agent = Agent(agent_def, self.context)
        session = agent.resume_session(session_id)
        self._register_session(session, cwd)
        return session

    def get_session(self, session_id: str) -> AgentSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        return session

    def close_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        self.session_cwds.pop(session_id, None)

    def _register_session(self, session: AgentSession, cwd: Path) -> None:
        self.sessions[session.session_id] = session
        self.session_cwds[session.session_id] = cwd
        setattr(session, "working_directory", cwd)


class AcpJsonRpcServer:
    """Newline-delimited JSON-RPC server for ACP stdio transport."""

    def __init__(
        self,
        runtime: AcpRuntime,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        self.runtime = runtime
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._write_lock = asyncio.Lock()
        self._message_tasks: set[asyncio.Task[None]] = set()
        self._prompt_tasks: dict[str, asyncio.Task[Any]] = {}
        self.protocol_version = DEFAULT_PROTOCOL_VERSION

    async def serve(self) -> None:
        """Read JSON-RPC messages from stdin until EOF."""
        self.runtime.start()
        try:
            while True:
                line = await asyncio.to_thread(self.stdin.readline)
                if line == "":
                    break
                line = line.strip()
                if not line:
                    continue

                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    await self._send_error(None, -32700, f"Parse error: {exc}")
                    continue

                self._schedule_message(message)

            if self._message_tasks:
                await asyncio.gather(*self._message_tasks, return_exceptions=True)
        finally:
            await self.runtime.stop()

    def _schedule_message(self, message: Any) -> None:
        if isinstance(message, list):
            for item in message:
                self._schedule_message(item)
            return

        task = asyncio.create_task(self._handle_message(message))
        self._message_tasks.add(task)
        task.add_done_callback(self._message_tasks.discard)

    async def _handle_message(self, message: Any) -> None:
        if not isinstance(message, dict):
            await self._send_error(None, -32600, "Invalid Request")
            return

        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if not isinstance(method, str):
            await self._send_error(request_id, -32600, "Invalid Request")
            return
        if not isinstance(params, dict):
            await self._send_error(request_id, -32602, "Invalid params")
            return

        try:
            result = await self._dispatch(method, params)
        except MethodNotFound:
            await self._send_error(request_id, -32601, f"Method not found: {method}")
            return
        except Exception as exc:
            logger.exception("ACP method failed: %s", method)
            await self._send_error(request_id, -32000, str(exc))
            return

        if request_id is not None:
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _dispatch(self, method: str, params: JSON) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "session/new":
            return self._new_session(params)
        if method == "session/load":
            return await self._load_session(params)
        if method == "session/resume":
            return self._resume_session(params)
        if method == "session/prompt":
            return await self._prompt(params)
        if method == "session/cancel":
            self._cancel(params)
            return None
        if method == "session/close":
            self._close(params)
            return {}
        raise MethodNotFound()

    def _initialize(self, params: JSON) -> JSON:
        requested_version = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
        if requested_version in SUPPORTED_PROTOCOL_VERSIONS:
            self.protocol_version = requested_version
        else:
            self.protocol_version = DEFAULT_PROTOCOL_VERSION

        if self.protocol_version == 2:
            return {
                "protocolVersion": self.protocol_version,
                "capabilities": {
                    "session": {
                        "prompt": {
                            "embeddedContext": {},
                        },
                    },
                },
                "info": {
                    "name": "my-bot-acp",
                    "title": "my-bot ACP Adapter",
                    "version": "0.1.0",
                },
                "authMethods": [],
            }

        return {
            "protocolVersion": self.protocol_version,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {
                    "embeddedContext": True,
                },
                "sessionCapabilities": {
                    "resume": {},
                    "close": {},
                },
            },
            "agentInfo": {
                "name": "my-bot-acp",
                "title": "my-bot ACP Adapter",
                "version": "0.1.0",
            },
            "authMethods": [],
        }

    def _new_session(self, params: JSON) -> JSON:
        cwd = self._parse_cwd(params)
        agent_id = self._read_meta(params).get("agentId")
        session = self.runtime.new_session(cwd, agent_id=agent_id)
        return {"sessionId": session.session_id}

    async def _load_session(self, params: JSON) -> None:
        session_id = self._require_session_id(params)
        cwd = self._parse_cwd(params)
        session = self.runtime.load_session(session_id, cwd)

        messages = self.runtime.context.history_store.get_messages(session.session_id)
        for index, message in enumerate(messages):
            await self._replay_message(session.session_id, index, message)

    def _resume_session(self, params: JSON) -> JSON:
        session_id = self._require_session_id(params)
        cwd = self._parse_cwd(params)
        self.runtime.load_session(session_id, cwd)
        return {}

    async def _prompt(self, params: JSON) -> JSON:
        session_id = self._require_session_id(params)
        prompt = params.get("prompt")
        if not isinstance(prompt, list):
            raise ValueError("session/prompt requires a prompt array")

        session = self.runtime.get_session(session_id)
        message = self._prompt_to_text(prompt)

        if self.protocol_version == 2:
            self._start_v2_prompt(session_id, session, message)
            return {}

        return await self._run_v1_prompt(session_id, session, message)

    async def _run_v1_prompt(
        self,
        session_id: str,
        session: AgentSession,
        message: str,
    ) -> JSON:
        turn_task = asyncio.create_task(session.chat(message))
        self._prompt_tasks[session_id] = turn_task
        try:
            response = await turn_task
        except asyncio.CancelledError:
            await self._send_agent_text(session_id, "Cancelled.")
            return {"stopReason": "cancelled"}
        finally:
            self._prompt_tasks.pop(session_id, None)

        await self._send_agent_text(session_id, response)
        return {"stopReason": "end_turn"}

    def _start_v2_prompt(
        self,
        session_id: str,
        session: AgentSession,
        message: str,
    ) -> None:
        async def run_turn() -> None:
            await self._send_state(session_id, "running")
            try:
                response = await session.chat(message)
            except asyncio.CancelledError:
                await self._send_agent_text(session_id, "Cancelled.")
                await self._send_state(session_id, "idle", stop_reason="cancelled")
            except Exception as exc:
                logger.exception("ACP v2 prompt failed")
                await self._send_agent_text(session_id, f"Error: {exc}")
                await self._send_state(session_id, "idle", stop_reason="error")
            else:
                await self._send_agent_text(session_id, response)
                await self._send_state(session_id, "idle", stop_reason="end_turn")
            finally:
                self._prompt_tasks.pop(session_id, None)

        turn_task = asyncio.create_task(run_turn())
        self._prompt_tasks[session_id] = turn_task

    def _cancel(self, params: JSON) -> None:
        session_id = self._require_session_id(params)
        task = self._prompt_tasks.get(session_id)
        if task is not None:
            task.cancel()

    def _close(self, params: JSON) -> None:
        session_id = self._require_session_id(params)
        self._cancel(params)
        self.runtime.close_session(session_id)

    async def _replay_message(
        self,
        session_id: str,
        index: int,
        message: HistoryMessage,
    ) -> None:
        if message.role == "user":
            update_type = "user_message"
        elif message.role == "assistant" and message.content:
            update_type = "agent_message"
        else:
            return

        await self._send_text_update(
            session_id,
            update_type,
            f"history-{index}",
            message.content,
        )

    async def _send_agent_text(self, session_id: str, text: str) -> None:
        update_type = (
            "agent_message" if self.protocol_version == 2 else "agent_message_chunk"
        )
        await self._send_text_update(
            session_id,
            update_type,
            f"msg-{uuid.uuid4()}",
            text,
        )

    async def _send_text_update(
        self,
        session_id: str,
        update_type: str,
        message_id: str,
        text: str,
    ) -> None:
        content = {"type": "text", "text": text}
        if self.protocol_version == 2:
            payload: JSON = {
                "sessionUpdate": update_type,
                "messageId": message_id,
                "content": [content],
            }
        else:
            payload = {
                "sessionUpdate": f"{update_type}_chunk"
                if not update_type.endswith("_chunk")
                else update_type,
                "messageId": message_id,
                "content": content,
            }

        await self._send_update(session_id, payload)

    async def _send_state(
        self,
        session_id: str,
        state: str,
        stop_reason: str | None = None,
    ) -> None:
        update: JSON = {
            "sessionUpdate": "state_update",
            "state": state,
        }
        if stop_reason is not None:
            update["stopReason"] = stop_reason

        await self._send_update(session_id, update)

    async def _send_update(self, session_id: str, update: JSON) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": update,
                },
            }
        )

    async def _send_error(
        self,
        request_id: Any,
        code: int,
        message: str,
    ) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    async def _send(self, message: JSON) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            self.stdout.write(data + "\n")
            self.stdout.flush()

    def _prompt_to_text(self, blocks: list[Any]) -> str:
        parts: list[str] = []
        for block in blocks:
            block_type = self._get(block, "type")
            if block_type == "text":
                parts.append(str(self._get(block, "text", "")))
            elif block_type == "resource":
                resource = self._get(block, "resource", {})
                uri = self._get(resource, "uri", "embedded-resource")
                text = self._get(resource, "text")
                blob = self._get(resource, "blob")
                if text is not None:
                    parts.append(f"[Resource: {uri}]\n{text}")
                elif blob is not None:
                    parts.append(f"[Binary resource: {uri}]")
            elif block_type == "resource_link":
                uri = self._get(block, "uri", "")
                name = self._get(block, "name", uri)
                parts.append(f"[Resource link: {name}] {uri}")
            else:
                parts.append(f"[Unsupported ACP content block: {block_type}]")

        return "\n\n".join(part for part in parts if part).strip()

    def _parse_cwd(self, params: JSON) -> Path:
        raw_cwd = params.get("cwd")
        if not isinstance(raw_cwd, str) or not raw_cwd:
            raise ValueError("cwd is required")

        cwd = Path(raw_cwd).expanduser()
        if not cwd.is_absolute():
            raise ValueError("cwd must be an absolute path")
        return cwd

    def _require_session_id(self, params: JSON) -> str:
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("sessionId is required")
        return session_id

    def _read_meta(self, params: JSON) -> JSON:
        meta = params.get("_meta")
        return meta if isinstance(meta, dict) else {}

    def _get(self, obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)


class MethodNotFound(Exception):
    """Raised when a JSON-RPC method is unsupported."""


async def run_acp_server(config: Config) -> None:
    """Run my-bot as an ACP agent over stdio."""
    runtime = AcpRuntime(config)
    server = AcpJsonRpcServer(runtime)
    await server.serve()
