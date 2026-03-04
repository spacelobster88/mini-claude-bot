"""Gateway router: HTTP API for multi-session Claude CLI access.

telegram-claude-hero forwards messages here instead of spawning
Claude CLI directly. Each chat_id gets an isolated session.
"""

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from backend.db.engine import get_db
from backend.db.vector import store_chat_embedding
from backend.services.session_manager import get_session_manager

router = APIRouter(prefix="/api/gateway", tags=["gateway"])


class SendRequest(BaseModel):
    chat_id: str
    message: str
    user_id: str | None = None  # for audit, not isolation
    username: str | None = None  # for audit, not isolation


class SendResponse(BaseModel):
    response: str
    session_key: str


class StopRequest(BaseModel):
    chat_id: str


@router.post("/send")
async def gateway_send(req: SendRequest) -> SendResponse:
    manager = get_session_manager()
    session_id = f"gw-{req.chat_id}"

    # Store user message
    db = get_db()
    cursor = db.execute(
        """INSERT INTO chat_messages (session_id, role, content, source, telegram_chat_id)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, "user", req.message, "telegram", int(req.chat_id) if req.chat_id.lstrip("-").isdigit() else None),
    )
    db.commit()
    user_msg_id = cursor.lastrowid

    # Best-effort embedding
    try:
        await store_chat_embedding(user_msg_id, req.message)
    except Exception:
        pass

    # Send to Claude (blocking → offload to threadpool)
    response = await asyncio.to_thread(manager.send, req.chat_id, req.message)

    # Store assistant response (mark errors distinctly)
    is_error = response.startswith("[ERROR]") or response.startswith("[BUSY]")
    source = "error" if is_error else "telegram"
    cursor = db.execute(
        """INSERT INTO chat_messages (session_id, role, content, source, telegram_chat_id)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, "assistant", response, source, int(req.chat_id) if req.chat_id.lstrip("-").isdigit() else None),
    )
    db.commit()
    assistant_msg_id = cursor.lastrowid

    try:
        await store_chat_embedding(assistant_msg_id, response)
    except Exception:
        pass

    return SendResponse(response=response, session_key=req.chat_id)


@router.post("/stop")
def gateway_stop(req: StopRequest):
    manager = get_session_manager()
    stopped = manager.stop_session(req.chat_id)
    return {"stopped": stopped}


@router.get("/sessions")
def gateway_list_sessions():
    manager = get_session_manager()
    return manager.list_sessions()


@router.post("/sessions/{chat_id}/reset")
def gateway_reset_session(chat_id: str):
    """Reset a session's busy state (emergency recovery)."""
    from backend.services.session_manager import SESSION_BASE_DIR, SessionManager

    manager = get_session_manager()
    # Stop and recreate the session (also kills running processes)
    manager.stop_session(chat_id)
    # Clear any leftover Claude CLI session files (belt-and-suspenders)
    import shutil
    from pathlib import Path
    session_dir = Path(SESSION_BASE_DIR) / chat_id
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)
    mangled = SessionManager._mangle_cwd(str(session_dir))
    claude_session_dir = Path.home() / ".claude" / "projects" / mangled
    if claude_session_dir.exists():
        shutil.rmtree(str(claude_session_dir), ignore_errors=True)
    return {"reset": True, "chat_id": chat_id}
