"""Gateway router: HTTP API for multi-session Claude CLI access.

telegram-claude-hero forwards messages here instead of spawning
Claude CLI directly. Each (bot_id, chat_id) gets an isolated session.
"""

import asyncio
import json
import threading

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.db.engine import get_db
from backend.db.vector import store_chat_embedding
from backend.services.session_manager import get_session_manager

router = APIRouter(prefix="/api/gateway", tags=["gateway"])


class SendRequest(BaseModel):
    chat_id: str
    message: str
    bot_id: str = "default"
    user_id: str | None = None  # for audit, not isolation
    username: str | None = None  # for audit, not isolation


class SendResponse(BaseModel):
    response: str
    session_key: str


class StopRequest(BaseModel):
    chat_id: str
    bot_id: str = "default"


class BackgroundSendRequest(BaseModel):
    chat_id: str
    message: str
    bot_token: str
    bot_id: str = "default"
    plan_id: str = ""  # For pending plan confirmation


@router.post("/send")
async def gateway_send(req: SendRequest) -> SendResponse:
    manager = get_session_manager()
    session_id = f"gw-{req.bot_id}-{req.chat_id}"

    # Store user message
    db = get_db()
    tg_chat_id = int(req.chat_id) if req.chat_id.lstrip("-").isdigit() else None
    cursor = db.execute(
        """INSERT INTO chat_messages (session_id, role, content, source, telegram_chat_id, bot_id, user_id, username)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, "user", req.message, "telegram", tg_chat_id, req.bot_id, req.user_id, req.username),
    )
    db.commit()
    user_msg_id = cursor.lastrowid

    # Best-effort embedding
    try:
        await store_chat_embedding(user_msg_id, req.message)
    except Exception:
        pass

    # Send to Claude (blocking → offload to threadpool)
    response = await asyncio.to_thread(manager.send, req.chat_id, req.message, req.bot_id)

    # Store assistant response (mark errors distinctly)
    is_error = response.startswith("[ERROR]") or response.startswith("[BUSY]")
    source = "error" if is_error else "telegram"
    cursor = db.execute(
        """INSERT INTO chat_messages (session_id, role, content, source, telegram_chat_id, bot_id, user_id, username)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, "assistant", response, source, tg_chat_id, req.bot_id, None, None),
    )
    db.commit()
    assistant_msg_id = cursor.lastrowid

    try:
        await store_chat_embedding(assistant_msg_id, response)
    except Exception:
        pass

    return SendResponse(response=response, session_key=req.chat_id)


@router.post("/send-stream")
async def gateway_send_stream(req: SendRequest):
    manager = get_session_manager()
    session_id = f"gw-{req.bot_id}-{req.chat_id}"

    # Store user message in DB
    db = get_db()
    tg_chat_id = int(req.chat_id) if req.chat_id.lstrip("-").isdigit() else None
    db.execute(
        """INSERT INTO chat_messages (session_id, role, content, source, telegram_chat_id, bot_id, user_id, username)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, "user", req.message, "telegram", tg_chat_id, req.bot_id, req.user_id, req.username),
    )
    db.commit()

    # Best-effort embedding
    try:
        user_msg_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        await store_chat_embedding(user_msg_id, req.message)
    except Exception:
        pass

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _produce():
        try:
            for event in manager.send_streaming(req.chat_id, req.message, req.bot_id):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "content": str(e)})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

    thread = threading.Thread(target=_produce, daemon=True)
    thread.start()

    full_response = ""

    async def _generate():
        nonlocal full_response
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=330)
            except asyncio.TimeoutError:
                timeout_event = {"type": "error", "content": "Stream timeout"}
                yield f"data: {json.dumps(timeout_event)}\n\n"
                break
            if event is None:
                break
            if event.get("type") == "done":
                full_response = event.get("content", "")
            yield f"data: {json.dumps(event)}\n\n"

        # Store assistant response in DB after stream completes
        is_error = full_response.startswith("[ERROR]") or full_response.startswith("[BUSY]")
        source = "error" if is_error else "telegram"
        db.execute(
            """INSERT INTO chat_messages (session_id, role, content, source, telegram_chat_id, bot_id, user_id, username)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, "assistant", full_response, source, tg_chat_id, req.bot_id, None, None),
        )
        db.commit()

    return StreamingResponse(_generate(), media_type="text/event-stream")


@router.post("/stop")
def gateway_stop(req: StopRequest):
    manager = get_session_manager()
    stopped = manager.stop_session(req.chat_id, bot_id=req.bot_id)
    return {"stopped": stopped}


@router.get("/sessions")
def gateway_list_sessions(bot_id: str | None = Query(default=None)):
    manager = get_session_manager()
    return manager.list_sessions(bot_id=bot_id)


class ResetRequest(BaseModel):
    bot_id: str = "default"


@router.post("/sessions/{chat_id}/reset")
def gateway_reset_session(chat_id: str, req: ResetRequest | None = None, bot_id: str = Query(default=None)):
    """Reset a session's busy state (emergency recovery)."""
    from backend.services.session_manager import SESSION_BASE_DIR, SessionManager

    # Accept bot_id from JSON body or query param (body takes precedence)
    resolved_bot_id = "default"
    if req and req.bot_id:
        resolved_bot_id = req.bot_id
    elif bot_id:
        resolved_bot_id = bot_id
    bot_id = resolved_bot_id

    manager = get_session_manager()
    # Stop and recreate the session (also kills running processes)
    manager.stop_session(chat_id, bot_id=bot_id)
    # Clear any leftover Claude CLI session files (belt-and-suspenders)
    import shutil
    from pathlib import Path
    session_dir = Path(SESSION_BASE_DIR) / bot_id / chat_id
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)
    mangled = SessionManager._mangle_cwd(str(session_dir))
    claude_session_dir = Path.home() / ".claude" / "projects" / mangled
    if claude_session_dir.exists():
        shutil.rmtree(str(claude_session_dir), ignore_errors=True)

    # Force a fresh Claude session by clearing session state
    db = get_db()
    db.execute("DELETE FROM gateway_sessions WHERE chat_id = ? AND bot_id = ?", (chat_id, bot_id))
    db.commit()

    return {"reset": True, "chat_id": chat_id, "bot_id": bot_id}


@router.post("/send-background")
def gateway_send_background(req: BackgroundSendRequest):
    """Start a background Claude CLI task. Returns immediately."""
    manager = get_session_manager()
    session_id = f"gw-{req.bot_id}-{req.chat_id}"

    # Store user message in DB (like the regular send endpoint)
    db = get_db()
    tg_chat_id = int(req.chat_id) if req.chat_id.lstrip("-").isdigit() else None
    db.execute(
        """INSERT INTO chat_messages (session_id, role, content, source, telegram_chat_id, bot_id, user_id, username)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, "user", req.message, "telegram", tg_chat_id, req.bot_id, None, None),
    )
    db.commit()

    result = manager.send_background(req.chat_id, req.message, req.bot_token, bot_id=req.bot_id, plan_id=req.plan_id)
    return result


@router.get("/background-status/{chat_id}")
def gateway_background_status(chat_id: str, bot_id: str = Query(default="default")):
    """Get the status of a background task for the given chat_id."""
    manager = get_session_manager()
    return manager.get_background_status(chat_id, bot_id=bot_id)


class StorePendingPlanRequest(BaseModel):
    chat_id: str
    plan_id: str
    plan: str
    bot_id: str = "default"


@router.post("/store-pending-plan")
def gateway_store_pending_plan(req: StorePendingPlanRequest):
    """Store a pending plan for later confirmation."""
    manager = get_session_manager()
    manager._store_pending_plan(req.chat_id, req.plan_id, req.plan, req.bot_id)
    return {"status": "ok", "plan_id": req.plan_id}


