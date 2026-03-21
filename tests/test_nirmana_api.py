"""Tests for Nirmana API endpoints (away/back mode)."""
import os
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.services.scheduler import scheduler
from backend.services.session_manager import get_session_manager, SessionManager


@pytest.fixture(autouse=True)
def stop_scheduler():
    """Prevent the background scheduler from running during tests."""
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def manager():
    return get_session_manager()


@pytest.fixture(autouse=True)
def cleanup_nirmana_dir():
    """Clean up state snapshot files after tests."""
    yield
    import shutil
    state_dir = os.path.expanduser("~/eddie-nirmana/state")
    if os.path.exists(state_dir):
        for f in os.listdir(state_dir):
            if f.startswith("session-") and f.endswith(".md"):
                os.remove(os.path.join(state_dir, f))


# ── POST /api/gateway/nirmana  action=away ──────────────────────


def test_nirmana_away_sets_mode(client, manager):
    """action=away should set nirmana_mode=True and return success."""
    r = client.post("/api/gateway/nirmana", json={
        "chat_id": "test-123",
        "bot_id": "default",
        "action": "away",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "Nirmana activated" in data["message"]

    # Verify session state
    key = SessionManager._session_key("default", "test-123")
    session = manager._sessions.get(key)
    assert session is not None
    assert session.nirmana_mode is True
    assert session.nirmana_activated_at > 0


def test_nirmana_away_idempotent(client, manager):
    """Calling /away twice should be idempotent — no error, mode stays True."""
    payload = {"chat_id": "test-idem", "bot_id": "default", "action": "away"}

    r1 = client.post("/api/gateway/nirmana", json=payload)
    assert r1.status_code == 200

    key = SessionManager._session_key("default", "test-idem")
    first_activated_at = manager._sessions[key].nirmana_activated_at
    assert first_activated_at > 0

    # Second call — should succeed without error
    r2 = client.post("/api/gateway/nirmana", json=payload)
    assert r2.status_code == 200
    data = r2.json()
    assert data["status"] == "ok"

    # Mode should still be True
    session = manager._sessions[key]
    assert session.nirmana_mode is True
    # activated_at may be updated (re-activation) — that's fine, just must be > 0
    assert session.nirmana_activated_at > 0


def test_nirmana_away_creates_snapshot(client):
    """action=away should write a state snapshot file."""
    r = client.post("/api/gateway/nirmana", json={
        "chat_id": "test-snap",
        "bot_id": "default",
        "action": "away",
    })
    assert r.status_code == 200

    state_dir = os.path.expanduser("~/eddie-nirmana/state")
    assert os.path.exists(state_dir)
    # Should have at least one session-*.md file
    snapshots = [f for f in os.listdir(state_dir) if f.startswith("session-") and f.endswith(".md")]
    assert len(snapshots) >= 1


# ── POST /api/gateway/nirmana  action=back ──────────────────────


def test_nirmana_back_not_in_mode(client):
    """action=back when not in nirmana mode should return gracefully."""
    # First create a session without nirmana
    r = client.post("/api/gateway/nirmana", json={
        "chat_id": "test-back-none",
        "bot_id": "default",
        "action": "back",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["briefing"] == ""


def test_nirmana_back_clears_mode_and_returns_briefing(client, manager, fresh_db):
    """action=back should clear nirmana_mode and return briefing of missed messages."""
    # Set up: go away first
    r = client.post("/api/gateway/nirmana", json={
        "chat_id": "test-brief",
        "bot_id": "default",
        "action": "away",
    })
    assert r.status_code == 200

    # Simulate messages arriving while away — use the manager's DB connection
    # so the data is visible within the same thread-local connection
    from backend.db.engine import get_db
    db = get_db()
    session_id = "gw-default-test-brief"
    # Insert with explicit future timestamp to ensure they're after nirmana_activated_at
    from datetime import datetime, timezone, timedelta
    future_ts = (datetime.now(timezone.utc) + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        """INSERT INTO chat_messages (session_id, role, content, source, bot_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, "user", "Hey are you there?", "telegram", "default", future_ts),
    )
    db.execute(
        """INSERT INTO chat_messages (session_id, role, content, source, bot_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, "user", "Important update: deploy is done", "telegram", "default", future_ts),
    )
    db.commit()

    # Come back
    r = client.post("/api/gateway/nirmana", json={
        "chat_id": "test-brief",
        "bot_id": "default",
        "action": "back",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "Hey are you there?" in data["briefing"]
    assert "Important update: deploy is done" in data["briefing"]

    # Verify mode cleared
    key = SessionManager._session_key("default", "test-brief")
    session = manager._sessions.get(key)
    assert session.nirmana_mode is False
    assert session.nirmana_activated_at == 0.0


# ── GET /api/gateway/nirmana/{chat_id} ──────────────────────────


def test_get_nirmana_state_default(client):
    """GET should return nirmana_mode=False for unknown session."""
    r = client.get("/api/gateway/nirmana/unknown-chat")
    assert r.status_code == 200
    data = r.json()
    assert data["nirmana_mode"] is False
    assert data["nirmana_activated_at"] == 0.0
    assert data["away_duration_seconds"] is None


def test_get_nirmana_state_when_away(client):
    """GET should return nirmana_mode=True and duration when away."""
    # Go away
    client.post("/api/gateway/nirmana", json={
        "chat_id": "test-get-state",
        "bot_id": "default",
        "action": "away",
    })

    r = client.get("/api/gateway/nirmana/test-get-state", params={"bot_id": "default"})
    assert r.status_code == 200
    data = r.json()
    assert data["nirmana_mode"] is True
    assert data["nirmana_activated_at"] > 0
    assert data["away_duration_seconds"] is not None
    assert data["away_duration_seconds"] >= 0


# ── Validation ──────────────────────────────────────────────────


def test_nirmana_invalid_action(client):
    """Invalid action should return 422."""
    r = client.post("/api/gateway/nirmana", json={
        "chat_id": "test-invalid",
        "bot_id": "default",
        "action": "invalid",
    })
    assert r.status_code == 422


# ── Full flow integration test ───────────────────────────────


def test_nirmana_full_flow(client, manager, fresh_db):
    """End-to-end: away → verify state → simulate messages → back → verify cleared & briefing."""
    chat_id = "test-full-flow"
    bot_id = "default"

    # Step 1: Go away
    r = client.post("/api/gateway/nirmana", json={
        "chat_id": chat_id, "bot_id": bot_id, "action": "away",
    })
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    # Step 2: Verify state via GET endpoint
    r = client.get(f"/api/gateway/nirmana/{chat_id}", params={"bot_id": bot_id})
    assert r.status_code == 200
    state = r.json()
    assert state["nirmana_mode"] is True
    assert state["away_duration_seconds"] is not None

    # Step 3: Verify snapshot file was created
    state_dir = os.path.expanduser("~/eddie-nirmana/state")
    snapshots = [f for f in os.listdir(state_dir) if f.startswith("session-")]
    assert len(snapshots) >= 1

    # Step 4: Simulate messages while away
    from backend.db.engine import get_db
    from datetime import datetime, timezone, timedelta
    db = get_db()
    session_id = f"gw-{bot_id}-{chat_id}"
    future_ts = (datetime.now(timezone.utc) + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        """INSERT INTO chat_messages (session_id, role, content, source, bot_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, "user", "First missed message", "telegram", bot_id, future_ts),
    )
    db.execute(
        """INSERT INTO chat_messages (session_id, role, content, source, bot_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, "assistant", "Auto-reply from Nirmana", "gateway", bot_id, future_ts),
    )
    db.commit()

    # Step 5: Come back
    r = client.post("/api/gateway/nirmana", json={
        "chat_id": chat_id, "bot_id": bot_id, "action": "back",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "First missed message" in data["briefing"]
    assert "Auto-reply from Nirmana" in data["briefing"]

    # Step 6: Verify mode cleared
    key = SessionManager._session_key(bot_id, chat_id)
    session = manager._sessions[key]
    assert session.nirmana_mode is False
    assert session.nirmana_activated_at == 0.0

    # Step 7: Verify GET state also reflects cleared
    r = client.get(f"/api/gateway/nirmana/{chat_id}", params={"bot_id": bot_id})
    assert r.status_code == 200
    state = r.json()
    assert state["nirmana_mode"] is False
    assert state["away_duration_seconds"] is None


def test_nirmana_state_toggles_correctly(client):
    """State should transition: False → away → True → back → False."""
    chat_id = "test-toggle"
    bot_id = "default"

    # Initially not in nirmana mode
    r = client.get(f"/api/gateway/nirmana/{chat_id}", params={"bot_id": bot_id})
    assert r.json()["nirmana_mode"] is False

    # After away
    client.post("/api/gateway/nirmana", json={
        "chat_id": chat_id, "bot_id": bot_id, "action": "away",
    })
    r = client.get(f"/api/gateway/nirmana/{chat_id}", params={"bot_id": bot_id})
    assert r.json()["nirmana_mode"] is True

    # After back
    client.post("/api/gateway/nirmana", json={
        "chat_id": chat_id, "bot_id": bot_id, "action": "back",
    })
    r = client.get(f"/api/gateway/nirmana/{chat_id}", params={"bot_id": bot_id})
    assert r.json()["nirmana_mode"] is False
