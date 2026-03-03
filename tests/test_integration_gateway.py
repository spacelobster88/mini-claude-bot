"""Integration tests for the gateway API endpoints."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.services.scheduler import scheduler
from backend.services import session_manager as sm_mod


def _mock_popen(stdout="ok", stderr="", returncode=0):
    """Create a mock Popen that returns the given stdout/stderr via communicate()."""
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 12345
    return proc


@pytest.fixture(autouse=True)
def stop_scheduler():
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


@pytest.fixture(autouse=True)
def reset_session_manager():
    """Reset the singleton session manager between tests."""
    sm_mod._manager = None
    yield
    if sm_mod._manager is not None:
        sm_mod._manager.stop_cleanup_loop()
        sm_mod._manager = None


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=True)


# ── POST /api/gateway/send ───────────────────────────────────────

@patch("backend.services.session_manager.subprocess.Popen")
def test_send_returns_response(mock_popen, client):
    mock_popen.return_value = _mock_popen(stdout="Hello from Claude!")

    r = client.post("/api/gateway/send", json={
        "chat_id": "12345",
        "message": "Hello!",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["response"] == "Hello from Claude!"
    assert data["session_key"] == "12345"


@patch("backend.services.session_manager.subprocess.Popen")
def test_send_stores_messages_in_db(mock_popen, client):
    mock_popen.return_value = _mock_popen(stdout="response text")

    client.post("/api/gateway/send", json={
        "chat_id": "67890",
        "message": "user message",
    })

    # Messages should be in DB under session_id = gw-67890
    r = client.get("/api/chat/sessions/gw-67890")
    assert r.status_code == 200
    msgs = r.json()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "user message"
    assert msgs[0]["session_id"] == "gw-67890"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "response text"


@patch("backend.services.session_manager.subprocess.Popen")
def test_send_stores_telegram_chat_id(mock_popen, client):
    mock_popen.return_value = _mock_popen()

    client.post("/api/gateway/send", json={
        "chat_id": "99999",
        "message": "test",
    })

    r = client.get("/api/chat/sessions/gw-99999")
    msgs = r.json()
    assert msgs[0]["telegram_chat_id"] == 99999


@patch("backend.services.session_manager.subprocess.Popen")
def test_send_with_optional_fields(mock_popen, client):
    """user_id and username are optional audit fields."""
    mock_popen.return_value = _mock_popen()

    r = client.post("/api/gateway/send", json={
        "chat_id": "111",
        "message": "hi",
        "user_id": "42",
        "username": "testuser",
    })
    assert r.status_code == 200


@patch("backend.services.session_manager.subprocess.Popen")
def test_send_multiple_chats_isolated(mock_popen, client):
    """Different chat_ids get separate DB sessions."""
    mock_popen.return_value = _mock_popen(stdout="reply")

    client.post("/api/gateway/send", json={"chat_id": "aaa", "message": "msg1"})
    client.post("/api/gateway/send", json={"chat_id": "bbb", "message": "msg2"})

    r1 = client.get("/api/chat/sessions/gw-aaa")
    r2 = client.get("/api/chat/sessions/gw-bbb")
    assert len(r1.json()) == 2
    assert len(r2.json()) == 2
    assert r1.json()[0]["content"] == "msg1"
    assert r2.json()[0]["content"] == "msg2"


@patch("backend.services.session_manager.subprocess.Popen")
def test_send_error_marked_with_source(mock_popen, client):
    """Error responses from Claude should be stored with source='error'."""
    mock_popen.return_value = _mock_popen(returncode=1, stdout="", stderr="something broke")

    client.post("/api/gateway/send", json={"chat_id": "err_test", "message": "hi"})

    r = client.get("/api/chat/sessions/gw-err_test")
    msgs = r.json()
    assistant_msg = msgs[1]
    assert "[ERROR]" in assistant_msg["content"]
    assert assistant_msg["source"] == "error"


# ── POST /api/gateway/stop ──────────────────────────────────────

@patch("backend.services.session_manager.subprocess.Popen")
def test_stop_existing_session(mock_popen, client):
    mock_popen.return_value = _mock_popen()
    client.post("/api/gateway/send", json={"chat_id": "to_stop", "message": "hi"})

    r = client.post("/api/gateway/stop", json={"chat_id": "to_stop"})
    assert r.status_code == 200
    assert r.json()["stopped"] is True


def test_stop_nonexistent_session(client):
    r = client.post("/api/gateway/stop", json={"chat_id": "nope"})
    assert r.status_code == 200
    assert r.json()["stopped"] is False


# ── GET /api/gateway/sessions ───────────────────────────────────

def test_list_sessions_empty(client):
    r = client.get("/api/gateway/sessions")
    assert r.status_code == 200
    assert r.json() == []


@patch("backend.services.session_manager.subprocess.Popen")
def test_list_sessions_with_active(mock_popen, client):
    mock_popen.return_value = _mock_popen()
    client.post("/api/gateway/send", json={"chat_id": "chat_x", "message": "hi"})

    r = client.get("/api/gateway/sessions")
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["chat_id"] == "chat_x"
    assert sessions[0]["first_done"] is True
    assert sessions[0]["busy"] is False
    assert "idle_seconds" in sessions[0]
    assert "busy_seconds" in sessions[0]
