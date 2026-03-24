# mini-claude-bot

Sidecar service for [telegram-claude-hero](https://github.com/spacelobster88/telegram-claude-hero) — provides multi-session Claude CLI management, cron scheduling, semantic memory, chat history, and system monitoring.

## Architecture

```
telegram-claude-hero (Go)         mini-claude-bot (Python)
┌──────────────────────┐          ┌──────────────────────────────────┐
│  Telegram Bot API    │          │  FastAPI Backend                  │
│  ┌────────────────┐  │  HTTP    │  ┌────────────────────────────┐  │
│  │ Gateway Client ├──┼─────────►│  │ Gateway Router             │  │
│  └────────────────┘  │          │  │  /send  /send-stream       │  │
└──────────────────────┘          │  │  /send-background  /stop   │  │
                                  │  └──────────┬─────────────────┘  │
Claude Code                       │             │                    │
┌──────────────────────┐          │  ┌──────────▼─────────────────┐  │
│  MCP Client          │  stdio   │  │ Session Manager            │  │
│  ┌────────────────┐  │◄────────►│  │  • (bot_id, chat_id) keys  │  │
│  │ Tool Calls     │  │          │  │  • idle timeout cleanup    │  │
│  └────────────────┘  │          │  │  • busy-state + queuing    │  │
└──────────────────────┘          │  │  • memory guardrails       │  │
                                  │  └──────────┬─────────────────┘  │
                                  │             │                    │
                                  │  ┌──────────▼─────────────────┐  │
                                  │  │ Claude CLI subprocess      │  │
                                  │  │  claude -p --continue      │  │
                                  │  │  (CWD-based isolation)     │  │
                                  │  └────────────────────────────┘  │
                                  │                                  │
                                  │  Other services:                 │
                                  │  ├── Cron Scheduler              │
                                  │  ├── Semantic Memory (Ollama)    │
                                  │  ├── Chat History                │
                                  │  ├── Background Task Runner      │
                                  │  └── Daily Reports               │
                                  └──────────────────────────────────┘

MCP Server (mcp_server.py)
  Proxies HTTP to FastAPI backend, exposes all services as Claude Code tools
```

## Features

- **Multi-Session Gateway** — Each Telegram chat gets its own isolated Claude CLI session, keyed by `(bot_id, chat_id)` with CWD-based separation. Supports both synchronous and SSE streaming responses.
- **Session Manager** — Manages Claude CLI process lifecycle: idle timeout cleanup (configurable, default 2h), busy-state detection with queuing, memory guardrails (minimum free MB check before spawning), and stuck-process recovery.
- **Background Tasks** — Non-blocking task runner for long-running Claude prompts. Tasks execute in the background and deliver results to Telegram when complete. Includes harness batch-chaining for multi-step workflows.
- **Cron Scheduler** — APScheduler-backed job system supporting shell commands and Claude CLI prompts, with per-job timezone support and execution history tracking.
- **Semantic Memory** — Key-value memory store with Ollama vector embeddings (sqlite-vec) for semantic search, organized by categories.
- **Chat History** — Message storage with vector search across all conversations, per-session isolation.
- **Daily Reports** — Automated CN/EN reports: Claude CLI fetches live news, generates LaTeX, compiles to PDF via XeLaTeX, and sends via macOS Mail.app (JXA).
- **Public Dashboard** — Next.js app on Vercel showing system metrics, Claude usage, scheduled jobs, memory, and daily activity.
- **Metrics Pipeline** — Pushes system/app metrics to Vercel Edge Config on a schedule.
- **MCP Server** — Exposes gateway, cron, memory, chat, and metrics tools to Claude Code via the MCP protocol (stdio transport). Supports multi-tenant `bot_id` isolation across all tools.
- **React Frontend** — Chat viewer, cron job manager, and memory browser.

## Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.12+ / FastAPI |
| Frontend | TypeScript / React / Vite |
| Dashboard | Next.js 15 / Vercel / Edge Config |
| Database | SQLite + sqlite-vec |
| Embeddings | Ollama (`nomic-embed-text`, 768-dim) |
| Scheduler | APScheduler |
| PDF | XeLaTeX |
| Email | macOS Mail.app (AppleScript) |
| Claude | Claude CLI (`claude -p`) |

## Project Structure

```
mini-claude-bot/
├── backend/
│   ├── main.py              # FastAPI entrypoint
│   ├── config.py             # Settings
│   ├── db/
│   │   ├── engine.py         # SQLite + sqlite-vec init
│   │   ├── migrations.py     # Schema migrations
│   │   └── vector.py         # Vector search helpers
│   ├── routers/
│   │   ├── chat.py           # Chat history endpoints
│   │   ├── cron.py           # Cron job CRUD
│   │   ├── gateway.py        # Multi-session Claude CLI gateway
│   │   ├── memory.py         # Memory endpoints
│   │   └── metrics.py        # Aggregated metrics endpoint
│   ├── services/
│   │   ├── embeddings.py     # Ollama embedding client
│   │   ├── scheduler.py      # APScheduler engine
│   │   ├── claude_session.py # Claude CLI wrapper (simple subprocess)
│   │   ├── session_manager.py # Multi-session gateway manager
│   │   │                      #  (process lifecycle, idle cleanup,
│   │   │                      #   busy-state, background tasks,
│   │   │                      #   harness batch-chaining)
│   │   ├── claude_stats.py   # Claude CLI stats (live session parsing)
│   │   └── system_metrics.py # macOS system metrics collector
│   └── scripts/
│       ├── push_metrics.py   # Push metrics to Vercel dashboard
│       └── refresh_vercel_token.py # Vercel token rotation
├── dashboard/                # Next.js app (deployed to Vercel)
├── frontend/                 # React + Vite (local)
├── reports/
│   ├── scripts/
│   │   └── generate_report.py
│   └── templates/
│       ├── chinese.tex
│       └── english.tex
├── mcp_server.py             # MCP interface for Claude Code
└── tests/
```

## Setup

```bash
# Backend
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

# Frontend
cd frontend && npm install && cd ..

# Dashboard
cd dashboard && npm install && cd ..

# Environment
cp .env.example .env
# Edit .env with your settings

# Ollama (for embeddings)
ollama pull nomic-embed-text

# XeLaTeX (for PDF reports, macOS)
brew install --cask mactex
```

## Running

```bash
# Backend (auto-reload)
uvicorn backend.main:app --reload --port 8000

# Frontend
cd frontend && npm run dev

# MCP server (for Claude Code integration, see MCP section below)
python mcp_server.py

# Push metrics manually
python -m backend.scripts.push_metrics
```

## Gateway Mode

When paired with [telegram-claude-hero](https://github.com/spacelobster88/telegram-claude-hero), the gateway endpoints provide multi-chat session management:

```bash
# telegram-claude-hero connects to this service
GATEWAY_URL=http://localhost:8000 ./telegram-claude-hero
```

Each Telegram chat gets its own isolated Claude CLI session, keyed by `(bot_id, chat_id)`. The session manager handles:

- **CWD-based isolation** — Each session gets a unique working directory under `~/claude-gateway-sessions/{bot_id}/{chat_id}`, so `claude --continue` resumes the correct session.
- **Idle timeout** — Sessions are automatically cleaned up after inactivity (default 2 hours, configurable via `GATEWAY_SESSION_TIMEOUT`).
- **Busy-state queuing** — If a session is already processing a message, subsequent requests wait (up to `GATEWAY_QUEUE_WAIT_TIMEOUT`) rather than spawning duplicate processes.
- **Memory guardrails** — Refuses to spawn new Claude CLI processes when free memory drops below `GATEWAY_MIN_FREE_MB` (default 512 MB).
- **Stuck-process recovery** — Detects and terminates processes that exceed `GATEWAY_BUSY_STUCK_TIMEOUT`.

### Background Tasks

For long-running operations that should not block the chat, use the `/send-background` endpoint. Background tasks run asynchronously and deliver results to Telegram when complete. The harness batch-chaining system supports multi-step workflows with progress tracking via `/harness-status/{chat_id}`.

### Streaming

The `/send-stream` endpoint returns an SSE (Server-Sent Events) stream for real-time token-by-token responses.

## Dashboard

Public monitoring dashboard deployed on Vercel. Shows real-time system metrics pushed every 5 minutes.

**Cards:** System (CPU/memory/disk), Claude Usage (tokens/sessions), Scheduled Jobs, Memory Store, Chat History, Daily Activity

**Architecture:**
```
Host (FastAPI /api/metrics)
  → push_metrics.py (cron */5)
    → Vercel /api/push (bearer auth)
      → Edge Config
        → Dashboard reads & renders
```

## MCP Integration

`mcp_server.py` exposes the full mini-claude-bot API as [MCP](https://modelcontextprotocol.io/) tools for use with Claude Code (or any MCP-compatible client). It proxies HTTP requests to the running FastAPI backend.

**Available tool groups:**
- **Gateway** — `send_gateway_message`, `send_background_message`, `get_background_status`, `list_gateway_sessions`, `stop_gateway_session`, `reset_gateway_session`
- **Cron** — `list_cron_jobs`, `create_cron_job`, `update_cron_job`, `delete_cron_job`, `run_cron_job`, `get_cron_job_history`
- **Memory** — `add_memory`, `update_memory`, `delete_memory`, `list_memories`, `search_memory`
- **Chat** — `list_chat_sessions`, `get_chat_session`, `search_chat_history`
- **System** — `health_check`, `get_metrics`

**Configuration** (environment variables):
| Variable | Default | Description |
|----------|---------|-------------|
| `MCB_API_BASE` | `http://localhost:8000/api` | Backend API base URL |
| `MCB_MCP_TIMEOUT` | `30` | Default request timeout (seconds) |
| `MCB_GATEWAY_TIMEOUT` | `960` | Gateway request timeout (seconds) |
| `MCB_BOT_ID` | `default` | Default bot ID for multi-tenant isolation |

Add to your Claude Code MCP config (e.g., `~/.claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "mini-claude-bot": {
      "command": "python",
      "args": ["/path/to/mini-claude-bot/mcp_server.py"]
    }
  }
}
```

## Daily Reports

Two automated reports generated via cron:

| Report | Schedule | Format |
|--------|----------|--------|
| Chinese | 9:00 AM Shanghai | PDF + plaintext |
| English | 9:00 AM Los Angeles | PDF + personal note |

Configure recipients via environment variables (see `.env.example`).

```bash
python reports/scripts/generate_report.py --lang cn            # send to recipients
python reports/scripts/generate_report.py --lang cn --preview   # send to cc only
```

## API

| Endpoint | Description |
|----------|-------------|
| `POST /api/gateway/send` | Send message to a chat session (blocking) |
| `POST /api/gateway/send-stream` | Send message with SSE streaming response |
| `POST /api/gateway/send-background` | Start a background task (non-blocking) |
| `POST /api/gateway/stop` | Stop a chat session |
| `GET /api/gateway/sessions` | List active gateway sessions |
| `POST /api/gateway/sessions/{id}/reset` | Reset a session (emergency recovery) |
| `GET /api/gateway/background-status/{id}` | Get background task status |
| `GET /api/gateway/harness-status/{id}` | Get harness loop progress |
| `GET /api/chat/sessions` | List chat sessions with message counts |
| `GET /api/chat/search?q=...` | Semantic search messages |
| `GET /api/cron` | List cron jobs |
| `POST /api/cron` | Create cron job |
| `POST /api/cron/{id}/run` | Trigger job now |
| `GET /api/cron/{id}/history` | Get job execution history |
| `GET /api/memory` | List memories |
| `GET /api/memory/search?q=...` | Semantic search memories |
| `GET /api/metrics` | Aggregated system + app metrics |
| `GET /api/health` | Health check |

## Related

- [telegram-claude-hero](https://github.com/spacelobster88/telegram-claude-hero) — Telegram bot frontend (Go)

## License

Apache-2.0
