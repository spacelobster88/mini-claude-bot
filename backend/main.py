import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.db.engine import get_db
from backend.routers import chat, cron, gateway, memory, metrics
from backend.services.scheduler import start_scheduler, shutdown_scheduler
from backend.services.session_manager import get_session_manager, shutdown_session_manager

_start_time: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _start_time
    _start_time = time.monotonic()
    get_db()  # initialize DB + tables
    start_scheduler()
    get_session_manager()  # initialize gateway session manager
    yield
    shutdown_session_manager()
    shutdown_scheduler()


app = FastAPI(title="mini-claude-bot", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(cron.router)
app.include_router(gateway.router)
app.include_router(memory.router)
app.include_router(metrics.router)


@app.get("/health")
def health_root():
    """Root-level health endpoint for watchdog / LaunchAgent checks."""
    sm = get_session_manager()
    uptime = round(time.monotonic() - _start_time, 1)
    return {
        "status": "ok",
        "uptime_seconds": uptime,
        "active_sessions": len(sm._sessions),
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}
