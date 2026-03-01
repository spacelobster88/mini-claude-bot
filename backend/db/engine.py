import sqlite3
import struct
from pathlib import Path

import sqlite_vec

from backend.config import DATABASE_PATH, EMBEDDING_DIM

_connection: sqlite3.Connection | None = None


def serialize_float32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def get_db() -> sqlite3.Connection:
    global _connection
    if _connection is None:
        Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        _connection = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        _connection.enable_load_extension(True)
        sqlite_vec.load(_connection)
        _connection.enable_load_extension(False)
        _init_tables(_connection)
    return _connection


def _init_tables(db: sqlite3.Connection) -> None:
    db.executescript(f"""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            source TEXT DEFAULT 'telegram',
            telegram_chat_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_messages(created_at);

        CREATE TABLE IF NOT EXISTS cron_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            command TEXT NOT NULL,
            job_type TEXT DEFAULT 'shell',
            enabled INTEGER DEFAULT 1,
            last_run_at TIMESTAMP,
            last_result TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # sqlite-vec virtual tables (cannot use IF NOT EXISTS, so check manually)
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    if "chat_embeddings" not in tables:
        db.execute(f"""
            CREATE VIRTUAL TABLE chat_embeddings USING vec0(
                message_id INTEGER PRIMARY KEY,
                embedding FLOAT[{EMBEDDING_DIM}]
            )
        """)

    if "memory_embeddings" not in tables:
        db.execute(f"""
            CREATE VIRTUAL TABLE memory_embeddings USING vec0(
                memory_id INTEGER PRIMARY KEY,
                embedding FLOAT[{EMBEDDING_DIM}]
            )
        """)

    # Migration: add timezone column if missing
    cols = [r[1] for r in db.execute("PRAGMA table_info(cron_jobs)").fetchall()]
    if "timezone" not in cols:
        db.execute("ALTER TABLE cron_jobs ADD COLUMN timezone TEXT")

    db.commit()
