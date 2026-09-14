import asyncio
import json

from mybot.core.agent import Agent
from mybot.core.context import SharedContext
from mybot.core.events import CliEventSource
from mybot.tools.subagent_tool import create_subagent_dispatch_tool
from mybot.utils.config import Config, LLMConfig


def make_context(tmp_path):
    agents_path = tmp_path / "agents"
    for agent_id in ("assistant", "researcher"):
        directory = agents_path / agent_id
        directory.mkdir(parents=True)
        tools = (
            "[research, subagent_dispatch]" if agent_id == "assistant" else "[research]"
        )
        dispatch = "[researcher]" if agent_id == "assistant" else "[]"
        (directory / "AGENT.md").write_text(
            f"""---
name: {agent_id.title()}
description: test agent
tools: {tools}
dispatch_to: {dispatch}
---
Use authorized tools only.
""",
            encoding="utf-8",
        )
    config = Config(
        workspace=tmp_path,
        llm=LLMConfig(provider="openai", model="test-model", api_key="secret"),
        default_agent="assistant",
    )
    return SharedContext(config=config, channels=[])


def test_agent_registers_only_allowlisted_tools(tmp_path):
    context = make_context(tmp_path)
    assistant_def = context.agent_loader.load("assistant")
    researcher_def = context.agent_loader.load("researcher")

    assistant_tools = {
        tool.name
        for tool in Agent(assistant_def, context)._build_tools(False).list_all()
    }
    researcher_tools = {
        tool.name
        for tool in Agent(researcher_def, context)._build_tools(False).list_all()
    }

    assert assistant_tools == {"research", "subagent_dispatch"}
    assert researcher_tools == {"research"}


def test_dispatch_sync_wait_returns_accepted_without_cancelling(tmp_path):
    context = make_context(tmp_path)
    parent = Agent(context.agent_loader.load("assistant"), context).new_session(
        CliEventSource()
    )
    dispatch = create_subagent_dispatch_tool(
        "assistant", context, dispatch_to=["researcher"], timeout_seconds=0.01
    )
    assert dispatch is not None

    raw = asyncio.run(dispatch.execute(parent, agent_id="researcher", task="research"))
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["status"] == "queued"
    assert context.dispatch_repository.get_job(result["job_id"]).status == "queued"
