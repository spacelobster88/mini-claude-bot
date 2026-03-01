"""Integration tests for the FastAPI endpoints."""
import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.services.scheduler import scheduler


@pytest.fixture(autouse=True)
def stop_scheduler():
    """Prevent the background scheduler from running during tests."""
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


@pytest.fixture
def client():
    # Use the test client without lifespan to avoid scheduler start
    return TestClient(app, raise_server_exceptions=True)


# ── Health ──────────────────────────────────────────────────────

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ── Chat ────────────────────────────────────────────────────────

def test_create_and_list_messages(client):
    # Create two messages in the same session
    r = client.post("/api/chat/messages", json={
        "session_id": "test-sess-1",
        "role": "user",
        "content": "What is the meaning of life?",
        "source": "cli",
    })
    assert r.status_code == 201
    msg1_id = r.json()["id"]

    r = client.post("/api/chat/messages", json={
        "session_id": "test-sess-1",
        "role": "assistant",
        "content": "42, according to Douglas Adams.",
        "source": "cli",
    })
    assert r.status_code == 201

    # List sessions
    r = client.get("/api/chat/sessions")
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "test-sess-1"
    assert sessions[0]["message_count"] == 2

    # Get session messages
    r = client.get("/api/chat/sessions/test-sess-1")
    assert r.status_code == 200
    msgs = r.json()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


def test_get_nonexistent_session(client):
    r = client.get("/api/chat/sessions/nonexistent")
    assert r.status_code == 404


# ── CRON Jobs ───────────────────────────────────────────────────

def test_cron_crud(client):
    # Create
    r = client.post("/api/cron", json={
        "name": "test-echo",
        "cron_expression": "0 * * * *",
        "command": "echo integration_test",
        "job_type": "shell",
        "enabled": True,
    })
    assert r.status_code == 201
    job_id = r.json()["id"]

    # List
    r = client.get("/api/cron")
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1
    assert jobs[0]["name"] == "test-echo"

    # Update
    r = client.put(f"/api/cron/{job_id}", json={"name": "renamed-echo"})
    assert r.status_code == 200
    assert r.json()["name"] == "renamed-echo"

    # Run
    r = client.post(f"/api/cron/{job_id}/run")
    assert r.status_code == 200
    assert "integration_test" in r.json()["result"]

    # Delete
    r = client.delete(f"/api/cron/{job_id}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    # Verify deleted
    r = client.get("/api/cron")
    assert len(r.json()) == 0


def test_cron_toggle_enabled(client):
    r = client.post("/api/cron", json={
        "name": "toggle-me",
        "cron_expression": "* * * * *",
        "command": "echo x",
        "job_type": "shell",
        "enabled": True,
    })
    job_id = r.json()["id"]

    # Disable
    r = client.put(f"/api/cron/{job_id}", json={"enabled": False})
    assert r.json()["enabled"] == 0

    # Re-enable
    r = client.put(f"/api/cron/{job_id}", json={"enabled": True})
    assert r.json()["enabled"] == 1


def test_cron_run_nonexistent(client):
    r = client.post("/api/cron/9999/run")
    assert r.status_code == 404


# ── Memory ──────────────────────────────────────────────────────

def test_memory_crud(client):
    # Create
    r = client.post("/api/memory", json={
        "key": "user-preference",
        "content": "The user prefers dark mode",
        "category": "preferences",
    })
    assert r.status_code == 201
    mem_id = r.json()["id"]

    # List
    r = client.get("/api/memory")
    assert r.status_code == 200
    mems = r.json()
    assert len(mems) == 1
    assert mems[0]["key"] == "user-preference"

    # Update
    r = client.put(f"/api/memory/{mem_id}", json={"content": "User prefers light mode now"})
    assert r.status_code == 200
    assert "light mode" in r.json()["content"]

    # Delete
    r = client.delete(f"/api/memory/{mem_id}")
    assert r.status_code == 200

    # Verify
    r = client.get("/api/memory")
    assert len(r.json()) == 0


def test_memory_duplicate_key(client):
    client.post("/api/memory", json={"key": "dup", "content": "first", "category": "general"})
    r = client.post("/api/memory", json={"key": "dup", "content": "second", "category": "general"})
    assert r.status_code == 409


def test_memory_update_nonexistent(client):
    r = client.put("/api/memory/9999", json={"content": "nope"})
    assert r.status_code == 404
