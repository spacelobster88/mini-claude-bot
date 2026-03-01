from backend.db.engine import get_db, serialize_float32
from backend.services.embeddings import embed_text
from backend.config import EMBEDDING_DIM


async def store_chat_embedding(message_id: int, content: str) -> None:
    vec = await embed_text(content)
    db = get_db()
    db.execute(
        "INSERT INTO chat_embeddings(message_id, embedding) VALUES (?, ?)",
        (message_id, serialize_float32(vec)),
    )
    db.commit()


async def search_chat_messages(query: str, limit: int = 10) -> list[dict]:
    vec = await embed_text(query)
    db = get_db()
    rows = db.execute(
        f"""
        SELECT cm.id, cm.session_id, cm.role, cm.content, cm.source,
               cm.created_at, ce.distance
        FROM chat_embeddings ce
        JOIN chat_messages cm ON cm.id = ce.message_id
        WHERE ce.embedding MATCH ?
          AND k = ?
        ORDER BY ce.distance
        """,
        (serialize_float32(vec), limit),
    ).fetchall()
    return [dict(r) for r in rows]


async def store_memory_embedding(memory_id: int, content: str) -> None:
    vec = await embed_text(content)
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO memory_embeddings(memory_id, embedding) VALUES (?, ?)",
        (memory_id, serialize_float32(vec)),
    )
    db.commit()


async def search_memory(query: str, limit: int = 10) -> list[dict]:
    vec = await embed_text(query)
    db = get_db()
    rows = db.execute(
        f"""
        SELECT m.id, m.key, m.content, m.category, m.created_at, me.distance
        FROM memory_embeddings me
        JOIN memory m ON m.id = me.memory_id
        WHERE me.embedding MATCH ?
          AND k = ?
        ORDER BY me.distance
        """,
        (serialize_float32(vec), limit),
    ).fetchall()
    return [dict(r) for r in rows]
