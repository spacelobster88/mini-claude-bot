# mini-claude-bot — Implementation Plan

## Stack
- **Backend**: Python 3.12+ / FastAPI
- **Frontend**: TypeScript (React + Vite)
- **Database**: SQLite + [sqlite-vec](https://github.com/asg017/sqlite-vec) for vector search
- **Embeddings**: Ollama (local, free) — `nomic-embed-text` model (~274MB, 768-dim)
- **Scheduler**: APScheduler (cron jobs)
- **Claude integration**: `anthropic` Python SDK for direct API, plus subprocess wrapper for headless Claude CLI sessions

---

## Project Structure

```
mini-claude-bot/
├── backend/
│   ├── main.py                  # FastAPI app entrypoint
│   ├── config.py                # Settings (env vars, paths)
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py            # SQLite connection + sqlite-vec init
│   │   ├── models.py            # Table schemas (raw SQL, no ORM)
│   │   └── vector.py            # Vector search helpers (embed + query)
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── chat.py              # Chat history CRUD + search endpoints
│   │   ├── cron.py              # CRON job CRUD endpoints
│   │   └── memory.py            # Memory/vector search endpoints
│   ├── services/
│   │   ├── __init__.py
│   │   ├── embeddings.py        # Ollama embedding client
│   │   ├── scheduler.py         # APScheduler setup + job management
│   │   └── claude_session.py    # Headless Claude CLI session wrapper
│   └── requirements.txt
├── frontend/
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── api/                 # API client (fetch wrappers)
│       │   └── client.ts
│       ├── pages/
│       │   ├── Chat.tsx         # Chat history viewer + search
│       │   ├── CronJobs.tsx     # CRON job manager
│       │   └── Memory.tsx       # Semantic memory search
│       └── components/
│           ├── ChatMessage.tsx
│           ├── CronJobForm.tsx
│           └── SearchBar.tsx
├── data/                        # SQLite DB files (gitignored)
├── .env.example
├── .gitignore
└── README.md
```

---

## Database Schema (SQLite)

### `chat_messages`
```sql
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    source TEXT DEFAULT 'telegram',  -- 'telegram', 'cli', 'api'
    telegram_chat_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_chat_session ON chat_messages(session_id);
CREATE INDEX idx_chat_created ON chat_messages(created_at);
```

### `chat_embeddings` (sqlite-vec virtual table)
```sql
CREATE VIRTUAL TABLE chat_embeddings USING vec0(
    message_id INTEGER PRIMARY KEY,
    embedding FLOAT[768]
);
```

### `cron_jobs`
```sql
CREATE TABLE cron_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    command TEXT NOT NULL,          -- shell command or prompt to send to Claude
    job_type TEXT DEFAULT 'shell',  -- 'shell' or 'claude'
    enabled INTEGER DEFAULT 1,
    last_run_at TIMESTAMP,
    last_result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### `memory`
```sql
CREATE TABLE memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE memory_embeddings USING vec0(
    memory_id INTEGER PRIMARY KEY,
    embedding FLOAT[768]
);
```

---

## API Endpoints

### Chat History
- `GET  /api/chat/sessions` — list all sessions
- `GET  /api/chat/sessions/{session_id}` — get messages for a session
- `POST /api/chat/messages` — save a message (used by telegram bridge)
- `GET  /api/chat/search?q=...` — semantic vector search across all messages

### CRON Jobs
- `GET    /api/cron` — list all jobs
- `POST   /api/cron` — create a job
- `PUT    /api/cron/{id}` — update a job
- `DELETE /api/cron/{id}` — delete a job
- `POST   /api/cron/{id}/run` — trigger a job manually

### Memory
- `GET    /api/memory` — list all memories
- `POST   /api/memory` — store a memory (auto-embeds)
- `GET    /api/memory/search?q=...` — semantic search memories
- `DELETE /api/memory/{id}` — delete a memory

---

## Implementation Steps

### Phase 1: Repo + Backend skeleton
1. Create git repo, .gitignore, README
2. Set up Python venv + requirements.txt
3. FastAPI app with health endpoint
4. SQLite + sqlite-vec database init (create tables on startup)

### Phase 2: Chat history
5. Chat message CRUD endpoints
6. Ollama embedding service (call `nomic-embed-text`)
7. Auto-embed messages on insert, store in sqlite-vec
8. Semantic search endpoint (embed query → KNN in sqlite-vec)

### Phase 3: CRON jobs
9. APScheduler service (persisted job store in SQLite)
10. CRON CRUD endpoints (create/update/delete/list)
11. Job execution: shell commands + Claude prompts
12. Manual trigger endpoint

### Phase 4: Memory system
13. Memory CRUD with auto-embedding
14. Semantic memory search

### Phase 5: Frontend
15. Vite + React + TypeScript scaffold
16. Chat history page (list sessions, view messages, search)
17. CRON jobs page (list, create, edit, delete, run)
18. Memory page (search, browse)

### Phase 6: Integration
19. Claude session wrapper (subprocess bridge for headless CLI)
20. Wire telegram-claude-hero chat saving into the API
