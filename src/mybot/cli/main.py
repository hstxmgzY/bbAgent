"""CLI interface for my-bot using Typer."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from mybot.cli.acp import acp_command
from mybot.cli.chat import chat_command
from mybot.cli.server import server_command
from mybot.utils.config import Config

app = typer.Typer(
    name="my-bot",
    help="my-bot: Personal AI Assistant",
    no_args_is_help=True,
    add_completion=True,
)

error_console = Console(stderr=True)


def workspace_callback(ctx: typer.Context, workspace: str) -> Path:
    """Store workspace path in context for later use."""
    ctx.ensure_object(dict)
    ctx.obj["workspace"] = Path(workspace)
    return Path(workspace)


@app.callback()
def main(
    ctx: typer.Context,
    workspace: str = typer.Option(
        "examples/workspace",
        "--workspace",
        "-w",
        help="Path to workspace directory",
        callback=workspace_callback,
    ),
) -> None:
    """Configuration is loaded from workspace/config.user.yaml by default."""
    workspace_path = ctx.obj["workspace"]
    config_file = workspace_path / "config.user.yaml"

    if not config_file.exists():
        error_console.print(f"[yellow]No configuration found at {config_file}[/yellow]")
        raise typer.Exit(1)

    try:
        cfg = Config.load(workspace_path)
        ctx.obj["config"] = cfg
    except Exception as e:
        error_console.print(f"[red]Error loading config: {e}[/red]")
        raise typer.Exit(1)


@app.command("chat")
def chat(
    ctx: typer.Context,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            "-a",
            help="Agent ID to use (overrides default_agent from config)",
        ),
    ] = None,
) -> None:
    """Start interactive chat session."""
    chat_command(ctx, agent_id=agent)


@app.command("server")
def server(ctx: typer.Context) -> None:
    """Start the 24/7 server for cron and messagebus execution."""
    server_command(ctx)


@app.command("acp")
def acp(ctx: typer.Context) -> None:
    """Run ACP stdio server for editor/client integrations."""
    acp_command(ctx)


if __name__ == "__main__":
    app()
