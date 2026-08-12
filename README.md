# Integrated Research Agent Runtime

Detailed Chinese documentation: [`docs/PROJECT_GUIDE.zh-CN.md`](docs/PROJECT_GUIDE.zh-CN.md)

This project combines:

- `mybot`, the only user-facing runtime and orchestration layer
- `research_assistant`, an injectable research service for retrieval and cited reports

It is intentionally small and readable:

```text
topic
-> load scoped research memory and rewrite queries
-> search web
-> reuse fresh source versions or fetch pages
-> chunk documents
-> embed chunks
-> persist and retrieve relevant chunks
-> synthesize answer
-> evaluate claim-level citation support
-> output evidence, quality scores, and source links
```

## What It Implements

| Stage 2 requirement | Implementation |
| --- | --- |
| RAG: chunk, embed, persist, retrieve, answer with citations | `rag.py`, `embeddings.py`, `vectorstores/`, `llm.py` |
| Search / file / browser-like tools | `SearchTool`, `WebReadTool` in `tools.py` |
| Scoped memory and versioned evidence | SQLite `repositories/`, source TTL, content hashes, `refresh` |
| Tool failure / empty result / repeated call handling | `ToolResult`, warnings, per-run query/source dedupe |
| Answers with evidence and sources | `ResearchReport.to_markdown()` |
| Claim-level quality gates | `claims.py`, deterministic checks, injectable verifier |
| Traces and low-cardinality metrics | `telemetry.py`, optional OTLP and Prometheus exporters |
| Runtime integration | allowlisted `research` tool with structured JSON output |
| Optional delegation | explicit `dispatch_to`, timeout, cancellation, and `job_id` |

The default local embedding layer remains feature hashing so the project works offline. Production configuration can switch to SentenceTransformers and persistent local or remote Qdrant without changing the domain service.

## Install

From this directory:

```bash
cd project
uv sync --group dev
```

Install semantic retrieval and telemetry exporters when enabling them in `research` config:

```bash
uv sync --extra semantic-rag --extra observability --group dev
```

Or with pip:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Optional API Keys

Search works best with Brave:

```bash
export BRAVE_API_KEY="your-brave-search-api-key"
```

If `BRAVE_API_KEY` is not set, the project tries DuckDuckGo HTML search as a lightweight fallback.

LLM synthesis uses LiteLLM. For OpenAI:

```bash
export OPENAI_API_KEY="your-openai-api-key"
export LITELLM_MODEL="gpt-4.1-mini"
```

If no LLM API key is available, the assistant falls back to an extractive summary based on retrieved chunks.

## Run

```bash
uv run research-assistant "RAG evaluation best practices" -o reports/rag.md
```

Or:

```bash
python -m research_assistant "Model Context Protocol" --max-sources 5
```

Run the integrated runtime with the example assistant and researcher definitions:

```bash
export OPENAI_API_KEY="your-openai-api-key"
uv run my-bot --workspace examples/workspace chat
```

The default assistant can call `research` and dispatch only to `researcher`.
The researcher can call only `research`; neither Agent receives file or shell tools.

## Main Files

| File | Role |
| --- | --- |
| `src/mybot/` | original 18-acp agent runtime copied into this project |
| `src/research_assistant/tools.py` | search and web-read tools |
| `src/research_assistant/rag.py` | chunking and in-memory retrieval |
| `src/research_assistant/embeddings.py` | hashing and SentenceTransformers adapters |
| `src/research_assistant/repositories/sqlite.py` | runs, queries, source versions, and claim audit data |
| `src/research_assistant/vectorstores/qdrant.py` | versioned persistent vector storage |
| `src/research_assistant/claims.py` | atomic claims, citation binding, verification, and scoring |
| `src/research_assistant/telemetry.py` | trace stages and low-cardinality metrics |
| `src/research_assistant/llm.py` | LLM answer synthesis and fallback |
| `src/research_assistant/service.py` | injectable research orchestration |
| `src/research_assistant/assistant.py` | backward-compatible CLI facade |
| `src/research_assistant/cli.py` | CLI entrypoint |
| `src/mybot/tools/research_tool.py` | structured runtime adapter |

## How This Maps To `18-acp`

`18-acp` already teaches a full agent runtime with tools, sessions, memory, web search, web read, and ACP client integration.

This project keeps that full runtime under `src/mybot` and adds the Stage 2 learning target under `src/research_assistant`:

```text
18-acp ToolRegistry      -> project SearchTool / WebReadTool
18-acp AgentSession.chat -> project ResearchAssistant.research
18-acp history/memory    -> project scoped SQLite research memory
18-acp websearch/webread -> project tools.py
Stage 2 RAG layer        -> project rag.py + embeddings.py
```

Agent tools are deny-by-default. Add tool names explicitly in `AGENT.md`, for example
`tools: [research]`. File tools stay inside the configured workspace; `bash` is
disabled unless explicitly granted and has timeout/output limits.

## Run The Original 18-ACP Runtime

The original ACP command is also available:

```bash
uv run my-bot acp
```

Use `--workspace` to point it at a compatible workspace config.

## Reliability Notes

- Never cite URLs that did not come from retrieved sources.
- Treat empty search results as a normal failure mode.
- Deduplicate URLs before fetching.
- Limit readable content size before sending it to the model.
- Reject private, loopback, link-local, and reserved URL targets on every redirect.
- Keep source ids stable in a report: `[S1]`, `[S2]`, etc.
- If the answer cannot be supported by retrieved chunks, say so.
- Put secrets in environment variables and reference them as `${ENV_VAR}` in YAML.
