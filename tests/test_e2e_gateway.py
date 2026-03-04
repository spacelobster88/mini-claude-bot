"""E2E tests for the gateway: multi-chat concurrency, isolation, resume."""

import threading
import time
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
    sm_mod._manager = None
    yield
    if sm_mod._manager is not None:
        sm_mod._manager.stop_cleanup_loop()
        sm_mod._manager = None


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=True)


# ── Concurrent multi-chat ────────────────────────────────────────

@patch("backend.services.session_manager.subprocess.Popen")
def test_two_chats_concurrent(mock_popen, client):
    """Two different chats can send messages and both get responses."""
    mock_popen.return_value = _mock_popen(stdout="reply")

    r1 = client.post("/api/gateway/send", json={"chat_id": "chat_a", "message": "hello from A"})
    r2 = client.post("/api/gateway/send", json={"chat_id": "chat_b", "message": "hello from B"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["session_key"] == "chat_a"
    assert r2.json()["session_key"] == "chat_b"

    # Both have their own DB sessions
    s1 = client.get("/api/chat/sessions/gw-chat_a").json()
    s2 = client.get("/api/chat/sessions/gw-chat_b").json()
    assert s1[0]["content"] == "hello from A"
    assert s2[0]["content"] == "hello from B"


@patch("backend.services.session_manager.subprocess.Popen")
def test_context_isolation(mock_popen, client):
    """Each chat gets --continue only for its own session, not others."""
    call_args = []

    def capture_popen(*args, **kwargs):
        call_args.append((args, kwargs))
        return _mock_popen()

    mock_popen.side_effect = capture_popen

    # Chat A: first message (no --continue), second message (--continue)
    client.post("/api/gateway/send", json={"chat_id": "iso_a", "message": "a1"})
    client.post("/api/gateway/send", json={"chat_id": "iso_a", "message": "a2"})

    # Chat B: first message (no --continue)
    client.post("/api/gateway/send", json={"chat_id": "iso_b", "message": "b1"})

    # Verify: A's first call has no --continue, A's second has --continue
    assert "--continue" not in call_args[0][0][0]
    assert "--continue" in call_args[1][0][0]
    # B's first call has no --continue (isolated from A)
    assert "--continue" not in call_args[2][0][0]

    # Verify CWDs are different
    assert call_args[0][1]["cwd"] != call_args[2][1]["cwd"]


# ── Busy handling ────────────────────────────────────────────────

@patch("backend.services.session_manager.subprocess.Popen")
def test_busy_returns_immediately(mock_popen, client):
    """When a chat is busy, second message gets [BUSY] response."""
    barrier = threading.Barrier(2, timeout=5)

    def slow_popen(*args, **kwargs):
        barrier.wait()  # wait for both threads to be running
        time.sleep(0.5)
        return _mock_popen(stdout="slow reply")

    mock_popen.side_effect = slow_popen

    results = {}

    def send_msg(key, msg):
        r = client.post("/api/gateway/send", json={"chat_id": "busy_chat", "message": msg})
        results[key] = r.json()["response"]

    # First message starts processing
    t1 = threading.Thread(target=send_msg, args=("first", "msg1"))
    t1.start()

    # Wait a bit for first to start, then send second
    barrier.wait()
    r2 = client.post("/api/gateway/send", json={"chat_id": "busy_chat", "message": "msg2"})

    t1.join(timeout=10)

    assert "[BUSY]" in r2.json()["response"]
    assert results["first"] == "slow reply"


# ── Stop isolation ───────────────────────────────────────────────

@patch("backend.services.session_manager.subprocess.Popen")
def test_stop_one_chat_doesnt_affect_other(mock_popen, client):
    """Stopping one chat leaves other chats unaffected."""
    mock_popen.return_value = _mock_popen()

    client.post("/api/gateway/send", json={"chat_id": "keep", "message": "hi"})
    client.post("/api/gateway/send", json={"chat_id": "remove", "message": "hi"})

    # Stop only "remove"
    client.post("/api/gateway/stop", json={"chat_id": "remove"})

    sessions = client.get("/api/gateway/sessions").json()
    assert len(sessions) == 1
    assert sessions[0]["chat_id"] == "keep"


# ── Session resume after restart ─────────────────────────────────

@patch("backend.services.session_manager.subprocess.Popen")
def test_session_resume_after_manager_reset(mock_popen, client):
    """After manager reset, existing JSONL triggers --continue."""
    import os
    import shutil
    from pathlib import Path

    mock_popen.return_value = _mock_popen()

    # Send first message to establish session
    client.post("/api/gateway/send", json={"chat_id": "resume_test", "message": "first"})

    # Get the CWD that was used
    session = sm_mod._manager._sessions["resume_test"]
    cwd = session.cwd

    # Create fake JSONL in Claude's project dir to simulate existing session
    mangled = cwd.replace("/", "-")
    session_dir = Path.home() / ".claude" / "projects" / mangled
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "fake-session.jsonl").write_text("{}")

    try:
        # Reset the manager (simulates restart)
        sm_mod._manager.stop_cleanup_loop()
        sm_mod._manager = None

        # Send another message — should detect existing JSONL and use --continue
        call_args = []

        def capture_popen(*args, **kwargs):
            call_args.append((args, kwargs))
            return _mock_popen(stdout="resumed")

        mock_popen.side_effect = capture_popen
        r = client.post("/api/gateway/send", json={"chat_id": "resume_test", "message": "after restart"})

        assert r.json()["response"] == "resumed"
        # The call should have --continue because JSONL exists
        assert "--continue" in call_args[0][0][0]
    finally:
        shutil.rmtree(str(session_dir), ignore_errors=True)


# ── Group chat: same chat_id from different users ────────────────

@patch("backend.services.session_manager.subprocess.Popen")
def test_group_chat_shared_context(mock_popen, client):
    """Multiple users in same group (same chat_id) share one session."""
    # Use a unique chat_id that won't have leftover JSONL from real sessions
    group_id = "-999000111"
    call_args = []

    def capture_popen(*args, **kwargs):
        call_args.append((args, kwargs))
        return _mock_popen()

    mock_popen.side_effect = capture_popen

    # User A sends to group
    client.post("/api/gateway/send", json={
        "chat_id": group_id,
        "message": "user A says hi",
        "user_id": "111",
        "username": "alice",
    })

    # User B sends to same group
    client.post("/api/gateway/send", json={
        "chat_id": group_id,
        "message": "user B says hello",
        "user_id": "222",
        "username": "bob",
    })

    # Both should use same session → second call has --continue
    assert "--continue" not in call_args[0][0][0]
    assert "--continue" in call_args[1][0][0]

    # Same CWD for both
    assert call_args[0][1]["cwd"] == call_args[1][1]["cwd"]

    # Both messages in same DB session
    msgs = client.get(f"/api/chat/sessions/gw-{group_id}").json()
    assert len(msgs) == 4  # 2 user + 2 assistant
