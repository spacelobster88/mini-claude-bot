from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.db.engine import get_db
from backend.routers import chat, cron, memory
from backend.services.scheduler import start_scheduler, shutdown_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_db()  # initialize DB + tables
    start_scheduler()
    yield
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
app.include_router(memory.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
