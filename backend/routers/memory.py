from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.db.engine import get_db
from backend.db.vector import store_memory_embedding, search_memory

router = APIRouter(prefix="/api/memory", tags=["memory"])


class MemoryCreate(BaseModel):
    key: str
    content: str
    category: str = "general"
    bot_id: str = "default"


class MemoryUpdate(BaseModel):
    content: str | None = None
    category: str | None = None


@router.get("")
def list_memories(category: str | None = None, bot_id: str = Query(default="default")):
    db = get_db()
    if category:
        rows = db.execute(
            "SELECT * FROM memory WHERE bot_id = ? AND category = ? ORDER BY updated_at DESC",
            (bot_id, category),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM memory WHERE bot_id = ? ORDER BY updated_at DESC",
            (bot_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_memory(mem: MemoryCreate):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    try:
        cursor = db.execute(
            "INSERT INTO memory (key, content, category, bot_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (mem.key, mem.content, mem.category, mem.bot_id, now, now),
        )
        db.commit()
    except Exception:
        raise HTTPException(409, f"Memory with key '{mem.key}' already exists")

    memory_id = cursor.lastrowid
    try:
        await store_memory_embedding(memory_id, mem.content)
    except Exception:
        pass

    return {"id": memory_id}


@router.put("/{memory_id}")
async def update_memory(memory_id: int, update: MemoryUpdate):
    db = get_db()
    row = db.execute("SELECT * FROM memory WHERE id = ?", (memory_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Memory not found")

    fields = update.model_dump(exclude_none=True)
    if not fields:
        return dict(row)

    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [memory_id]
    db.execute(f"UPDATE memory SET {set_clause} WHERE id = ?", values)
    db.commit()

    if "content" in fields:
        try:
            await store_memory_embedding(memory_id, fields["content"])
        except Exception:
            pass

    updated = db.execute("SELECT * FROM memory WHERE id = ?", (memory_id,)).fetchone()
    return dict(updated)


@router.delete("/{memory_id}")
def delete_memory(memory_id: int):
    db = get_db()
    db.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
    db.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
    db.commit()
    return {"deleted": True}


@router.get("/search")
async def search_memories(q: str, limit: int = 10, bot_id: str = Query(default="default")):
    try:
        results = await search_memory(q, limit, bot_id=bot_id)
        return results
    except Exception as e:
        raise HTTPException(503, f"Vector search unavailable (Ollama may be down): {e}")
