# mini-claude-bot

A personal AI agent framework — FastAPI backend + React frontend + public dashboard — with cron scheduling, semantic memory, chat history, automated daily reports, and system monitoring.

## Features

- **Cron Scheduler** — APScheduler-backed job system supporting shell commands and Claude CLI prompts, with per-job timezone support
- **Semantic Memory** — Key-value memory store with Ollama vector embeddings (sqlite-vec) for semantic search
- **Chat History** — Message storage with vector search across all conversations
- **Daily Reports** — Automated CN/EN reports: Claude CLI fetches live news, generates LaTeX, compiles to PDF via XeLaTeX, and sends via macOS Mail.app
- **Public Dashboard** — Next.js app on Vercel showing system metrics, Claude usage, scheduled jobs, memory, and daily activity
- **Metrics Pipeline** — Mac mini pushes system/app metrics to Vercel Edge Config every 5 minutes
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
│   │   ├── memory.py         # Memory endpoints
│   │   └── metrics.py        # Aggregated metrics endpoint
│   ├── services/
│   │   ├── embeddings.py     # Ollama client
│   │   ├── scheduler.py      # APScheduler engine
│   │   ├── claude_session.py # Claude CLI wrapper
│   │   ├── claude_stats.py   # Claude CLI stats (live session parsing)
│   │   └── system_metrics.py # macOS system metrics collector
│   └── scripts/
│       └── push_metrics.py   # Push metrics to Vercel dashboard
├── dashboard/                # Next.js app (deployed to Vercel)
│   ├── app/
│   │   ├── page.tsx          # Dashboard UI
│   │   └── api/
│   │       ├── push/route.ts # Receives metrics, writes to Edge Config
│   │       └── metrics/route.ts # Reads from Edge Config
│   └── lib/types.ts          # TypeScript interfaces
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

## Dashboard

Public monitoring dashboard deployed on Vercel. Shows real-time system metrics pushed from the Mac mini every 5 minutes.

**Cards:** System (CPU/memory/disk), Claude Usage (tokens/sessions), Scheduled Jobs, Memory Store, Chat History, Daily Activity

**Architecture:**
```
Mac mini (FastAPI /api/metrics)
  → push_metrics.py (cron */5)
    → Vercel /api/push (bearer auth)
      → Edge Config
        → Dashboard reads & renders
```

## Daily Reports

Two automated reports generated via cron:

| Report | Schedule | Recipients | Format |
|--------|----------|------------|--------|
| Chinese | 9:00 AM Shanghai | Dad (cc Eddie, bcc Erin) | PDF + plaintext |
| English | 9:00 AM Los Angeles | Erin (cc Eddie) | PDF + love note |

Manual run:
```bash
python reports/scripts/generate_report.py --lang cn           # send to recipients
python reports/scripts/generate_report.py --lang cn --preview  # send to Eddie only
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/chat/sessions` | List chat sessions |
| `GET /api/chat/search?q=...` | Semantic search messages |
| `GET /api/cron` | List cron jobs |
| `POST /api/cron` | Create cron job |
| `POST /api/cron/{id}/run` | Trigger job now |
| `GET /api/memory` | List memories |
| `GET /api/memory/search?q=...` | Semantic search memories |
| `GET /api/metrics` | Aggregated system + app metrics |

## License

Private project.
