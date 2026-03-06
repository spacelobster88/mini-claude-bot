from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.db.engine import get_db
from backend.db.vector import store_chat_embedding, search_chat_messages

router = APIRouter(prefix="/api/chat", tags=["chat"])


class MessageCreate(BaseModel):
    session_id: str
    role: str
    content: str
    source: str = "telegram"
    telegram_chat_id: int | None = None
    bot_id: str = "default"


@router.get("/sessions")
def list_sessions(bot_id: str = Query(default=None)):
    db = get_db()
    if bot_id:
        rows = db.execute("""
            SELECT session_id, bot_id,
                   MIN(created_at) AS started_at,
                   MAX(created_at) AS last_message_at,
                   COUNT(*) AS message_count
            FROM chat_messages
            WHERE bot_id = ?
            GROUP BY session_id
            ORDER BY last_message_at DESC
        """, (bot_id,)).fetchall()
    else:
        rows = db.execute("""
            SELECT session_id, bot_id,
                   MIN(created_at) AS started_at,
                   MAX(created_at) AS last_message_at,
                   COUNT(*) AS message_count
            FROM chat_messages
            GROUP BY session_id
            ORDER BY last_message_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(404, "Session not found")
    return [dict(r) for r in rows]


@router.post("/messages", status_code=201)
async def create_message(msg: MessageCreate):
    db = get_db()
    cursor = db.execute(
        """INSERT INTO chat_messages (session_id, role, content, source, telegram_chat_id, bot_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (msg.session_id, msg.role, msg.content, msg.source, msg.telegram_chat_id, msg.bot_id),
    )
    db.commit()
    message_id = cursor.lastrowid

    try:
        await store_chat_embedding(message_id, msg.content)
    except Exception:
        pass

    return {"id": message_id}


@router.get("/search")
async def search_messages(q: str, limit: int = 10, bot_id: str = Query(default=None)):
    try:
        results = await search_chat_messages(q, limit, bot_id=bot_id)
        return results
    except Exception as e:
        raise HTTPException(503, f"Vector search unavailable (Ollama may be down): {e}")
