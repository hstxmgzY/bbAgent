"""ACP stdio command."""

import asyncio

import typer

from mybot.acp.server import run_acp_server
from mybot.utils.logging import setup_logging


def acp_command(ctx: typer.Context) -> None:
    """Run my-bot as an ACP agent over stdio."""
    config = ctx.obj.get("config")
    setup_logging(config, console_output=False)
    asyncio.run(run_acp_server(config))
