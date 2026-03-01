"""Unit tests for database engine and schema."""
import struct

from backend.db.engine import get_db, serialize_float32


def test_serialize_float32():
    vec = [1.0, 2.0, 3.0]
    blob = serialize_float32(vec)
    assert isinstance(blob, bytes)
    assert len(blob) == 12  # 3 floats * 4 bytes
    unpacked = struct.unpack("3f", blob)
    assert list(unpacked) == vec


def test_tables_created(fresh_db):
    db = fresh_db
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    assert "chat_messages" in tables
    assert "cron_jobs" in tables
    assert "memory" in tables
    assert "chat_embeddings" in tables
    assert "memory_embeddings" in tables


def test_insert_chat_message(fresh_db):
    db = fresh_db
    db.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
        ("sess1", "user", "hello world"),
    )
    db.commit()
    row = db.execute("SELECT * FROM chat_messages WHERE session_id = 'sess1'").fetchone()
    assert row is not None
    assert row["role"] == "user"
    assert row["content"] == "hello world"


def test_insert_cron_job(fresh_db):
    db = fresh_db
    db.execute(
        "INSERT INTO cron_jobs (name, cron_expression, command) VALUES (?, ?, ?)",
        ("test-job", "* * * * *", "echo hi"),
    )
    db.commit()
    row = db.execute("SELECT * FROM cron_jobs WHERE name = 'test-job'").fetchone()
    assert row is not None
    assert row["cron_expression"] == "* * * * *"
    assert row["job_type"] == "shell"
    assert row["enabled"] == 1


def test_insert_memory(fresh_db):
    db = fresh_db
    db.execute(
        "INSERT INTO memory (key, content, category) VALUES (?, ?, ?)",
        ("test-key", "some content", "facts"),
    )
    db.commit()
    row = db.execute("SELECT * FROM memory WHERE key = 'test-key'").fetchone()
    assert row is not None
    assert row["category"] == "facts"


def test_memory_unique_key(fresh_db):
    db = fresh_db
    db.execute("INSERT INTO memory (key, content) VALUES ('k1', 'v1')")
    db.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO memory (key, content) VALUES ('k1', 'v2')")


def test_chat_role_check(fresh_db):
    db = fresh_db
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
            ("s1", "invalid_role", "nope"),
        )


def test_vector_table_insert_and_knn(fresh_db):
    db = fresh_db
    # Insert a fake 768-dim vector
    dim = 768
    vec = [0.1] * dim
    blob = serialize_float32(vec)

    # Need a corresponding chat message first
    db.execute("INSERT INTO chat_messages (id, session_id, role, content) VALUES (1, 's1', 'user', 'test')")
    db.execute("INSERT INTO chat_embeddings (message_id, embedding) VALUES (?, ?)", (1, blob))
    db.commit()

    # KNN query
    query_blob = serialize_float32([0.1] * dim)
    rows = db.execute(
        "SELECT message_id, distance FROM chat_embeddings WHERE embedding MATCH ? AND k = 1",
        (query_blob,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["message_id"] == 1
    assert rows[0]["distance"] < 0.001  # should be ~0


import pytest  # noqa: E402 (already imported via conftest but explicit for clarity)
