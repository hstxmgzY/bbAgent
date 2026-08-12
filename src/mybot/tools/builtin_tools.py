"""Built-in tools for agent capabilities."""

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from mybot.tools.base import tool

if TYPE_CHECKING:
    from mybot.core.agent import AgentSession


def _resolve_path(path: str, session: "AgentSession") -> Path:
    """Resolve a path and require it to remain inside the configured workspace."""
    workspace = session.shared_context.config.workspace.resolve()
    working_directory = getattr(session, "working_directory", None)
    base = Path(working_directory).resolve() if working_directory else workspace
    if base != workspace and workspace not in base.parents:
        raise ValueError("working directory is outside the configured workspace")

    requested = Path(path).expanduser()
    target = (requested if requested.is_absolute() else base / requested).resolve()
    if target != workspace and workspace not in target.parents:
        raise ValueError("path is outside the configured workspace")
    return target


# Filesystem tools


@tool(
    name="read",
    description="Read the contents of a text file",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read"},
        },
        "required": ["path"],
    },
)
async def read_file(path: str, session: "AgentSession") -> str:
    """Read and return the contents of a file at the given path."""
    target = _resolve_path(path, session)
    try:
        return target.read_text()
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except PermissionError:
        return f"Error: Permission denied reading: {path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {path}"
    except Exception as e:
        return f"Error reading file: {e}"


@tool(
    name="write",
    description="Write content to a file",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write"},
            "content": {
                "type": "string",
                "description": "Content to write to the file",
            },
        },
        "required": ["path", "content"],
    },
)
async def write_file(path: str, content: str, session: "AgentSession") -> str:
    """Write content to a file at the given path."""
    target = _resolve_path(path, session)
    try:
        target.write_text(content)
        return f"Successfully wrote to: {path}"
    except PermissionError:
        return f"Error: Permission denied writing to: {path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {path}"
    except Exception as e:
        return f"Error writing file: {e}"


@tool(
    name="edit",
    description="Edit a file by replacing a string with new content",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit"},
            "old_text": {"type": "string", "description": "The text to replace"},
            "new_text": {
                "type": "string",
                "description": "The new text to replace with",
            },
        },
        "required": ["path", "old_text", "new_text"],
    },
)
async def edit_file(
    path: str, old_text: str, new_text: str, session: "AgentSession"
) -> str:
    """Edit a file by replacing old_text with new_text."""
    target = _resolve_path(path, session)
    try:
        content = target.read_text()
        if old_text not in content:
            return f"Error: '{old_text}' not found in {path}"
        new_content = content.replace(old_text, new_text)
        target.write_text(new_content)
        return f"Successfully edited {path}"
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except PermissionError:
        return f"Error: Permission denied editing: {path}"
    except Exception as e:
        return f"Error editing file: {e}"


# Shell tool


@tool(
    name="bash",
    description="Execute a bash shell command",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute",
            },
        },
        "required": ["command"],
    },
)
async def bash(command: str, session: "AgentSession") -> str:
    """Execute an explicitly authorized shell command with resource limits."""
    try:
        working_directory = getattr(session, "working_directory", None)
        workspace = session.shared_context.config.workspace.resolve()
        cwd = Path(working_directory).resolve() if working_directory else workspace
        if cwd != workspace and workspace not in cwd.parents:
            return "Error: working directory is outside the configured workspace"
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env={
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "LANG", "LC_ALL", "TERM", "TMPDIR"}
            },
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return "Error: command timed out after 30 seconds"
        output = stdout.decode(errors="replace") if stdout else ""
        error = stderr.decode(errors="replace") if stderr else ""
        output = output[:20000]
        error = error[:20000]
        if output and error:
            return f"{output}\n{error}"
        return output or error or "Command completed with no output"
    except Exception as e:
        return f"Error executing command: {e}"
