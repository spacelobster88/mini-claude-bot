# mini-claude-bot

Sidecar service for [telegram-claude-hero](https://github.com/spacelobster88/telegram-claude-hero) — provides multi-session Claude CLI management, cron scheduling, semantic memory, chat history, and system monitoring.

## Architecture

```
telegram-claude-hero (Go)         mini-claude-bot (Python)
┌──────────────────────┐          ┌──────────────────────────────┐
│  Telegram Bot API    │          │  FastAPI Backend              │
│  ┌────────────────┐  │  HTTP    │  ┌──────────────────────┐    │
│  │ Gateway Client ├──┼─────────►│  │ /api/gateway/send    │    │
│  └────────────────┘  │          │  │ /api/gateway/stop    │    │
└──────────────────────┘          │  └──────┬───────────────┘    │
                                  │         │                    │
                                  │  ┌──────▼───────────────┐    │
                                  │  │ Session Manager      │    │
                                  │  │ (per-chat isolation)  │    │
                                  │  └──────┬───────────────┘    │
                                  │         │                    │
                                  │  ┌──────▼───────────────┐    │
                                  │  │ Claude CLI subprocess │    │
                                  │  │ (claude -p)           │    │
                                  │  └──────────────────────┘    │
                                  │                              │
                                  │  Other services:             │
                                  │  ├── Cron Scheduler          │
                                  │  ├── Semantic Memory (Ollama) │
                                  │  ├── Chat History            │
                                  │  ├── Daily Reports           │
                                  │  └── MCP Server              │
                                  └──────────────────────────────┘
```

## Features

- **Multi-Session Gateway** — Each Telegram chat gets its own isolated Claude CLI session via CWD-based separation
- **Cron Scheduler** — APScheduler-backed job system supporting shell commands and Claude CLI prompts, with per-job timezone support
- **Semantic Memory** — Key-value memory store with Ollama vector embeddings (sqlite-vec) for semantic search
- **Chat History** — Message storage with vector search across all conversations
- **Daily Reports** — Automated CN/EN reports: Claude CLI fetches live news, generates LaTeX, compiles to PDF via XeLaTeX, and sends via macOS Mail.app
- **Public Dashboard** — Next.js app on Vercel showing system metrics, Claude usage, scheduled jobs, memory, and daily activity
- **Metrics Pipeline** — Pushes system/app metrics to Vercel Edge Config on a schedule
- **MCP Server** — Exposes cron, memory, and chat tools to Claude Code
- **React Frontend** — Chat viewer, cron job manager, and memory browser

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
│   │   └── vector.py         # Vector search helpers
│   ├── routers/
│   │   ├── chat.py           # Chat history endpoints
│   │   ├── cron.py           # Cron job CRUD
│   │   ├── gateway.py        # Multi-session Claude CLI gateway
│   │   ├── memory.py         # Memory endpoints
│   │   └── metrics.py        # Aggregated metrics endpoint
│   ├── services/
│   │   ├── embeddings.py     # Ollama client
│   │   ├── scheduler.py      # APScheduler engine
│   │   ├── claude_session.py # Claude CLI wrapper
│   │   ├── session_manager.py # Multi-session manager
│   │   ├── claude_stats.py   # Claude CLI stats (live session parsing)
│   │   └── system_metrics.py # macOS system metrics collector
│   └── scripts/
│       └── push_metrics.py   # Push metrics to Vercel dashboard
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

# MCP server (for Claude Code integration)
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

Each Telegram chat gets its own isolated Claude CLI session with CWD-based separation.

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
| `POST /api/gateway/send` | Send message to a chat session |
| `POST /api/gateway/stop` | Stop a chat session |
| `GET /api/chat/sessions` | List chat sessions |
| `GET /api/chat/search?q=...` | Semantic search messages |
| `GET /api/cron` | List cron jobs |
| `POST /api/cron` | Create cron job |
| `POST /api/cron/{id}/run` | Trigger job now |
| `GET /api/memory` | List memories |
| `GET /api/memory/search?q=...` | Semantic search memories |
| `GET /api/metrics` | Aggregated system + app metrics |
| `GET /api/health` | Health check |

## Related

- [telegram-claude-hero](https://github.com/spacelobster88/telegram-claude-hero) — Telegram bot frontend (Go)

## License

MIT
