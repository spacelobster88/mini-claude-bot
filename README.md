# mini-claude-bot

A personal AI agent framework — FastAPI backend + React frontend — with cron scheduling, semantic memory, chat history, and automated daily report generation.

## Features

- **Cron Scheduler** — APScheduler-backed job system supporting shell commands and Claude CLI prompts, with per-job timezone support
- **Semantic Memory** — Key-value memory store with Ollama vector embeddings (sqlite-vec) for semantic search
- **Chat History** — Message storage with vector search across all conversations
- **Daily Reports** — Automated CN/EN reports: Claude CLI fetches live news, generates LaTeX, compiles to PDF via XeLaTeX, and sends via macOS Mail.app
- **MCP Server** — Exposes cron, memory, and chat tools to Claude Code
- **React Frontend** — Chat viewer, cron job manager, and memory browser

## Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.12+ / FastAPI |
| Frontend | TypeScript / React / Vite |
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
│   │   └── memory.py         # Memory endpoints
│   └── services/
│       ├── embeddings.py     # Ollama client
│       ├── scheduler.py      # APScheduler engine
│       └── claude_session.py # Claude CLI wrapper
├── frontend/                 # React + Vite
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

## License

Private project.
