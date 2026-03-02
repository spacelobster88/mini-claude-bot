"""Unit tests for the gateway session manager."""

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.services.session_manager import GatewaySession, SessionManager


@pytest.fixture
def tmp_session_dir(tmp_path):
    """Use a temp dir for session CWDs."""
    with patch("backend.services.session_manager.SESSION_BASE_DIR", str(tmp_path)):
        yield tmp_path


@pytest.fixture
def manager(tmp_session_dir):
    mgr = SessionManager()
    yield mgr
    mgr.stop_cleanup_loop()


# ── Session lifecycle ────────────────────────────────────────────

def test_create_session(manager, tmp_session_dir):
    """First call to send creates a session and CWD directory."""
    with patch("backend.services.session_manager.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="hello", stderr="")
        result = manager.send("chat123", "hi")

    assert result == "hello"
    assert "chat123" in manager._sessions
    assert os.path.isdir(os.path.join(str(tmp_session_dir), "chat123"))


def test_session_reuse(manager):
    """Subsequent sends reuse the same session with --continue."""
    with patch("backend.services.session_manager.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="first", stderr="")
        manager.send("chat1", "msg1")

        mock_run.return_value = MagicMock(returncode=0, stdout="second", stderr="")
        manager.send("chat1", "msg2")

    # Second call should have used --continue
    calls = mock_run.call_args_list
    assert "--continue" not in calls[0][0][0]
    assert "--continue" in calls[1][0][0]


def test_stop_session(manager):
    """stop_session removes session from dict."""
    with patch("backend.services.session_manager.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        manager.send("chat1", "hi")

    assert manager.stop_session("chat1") is True
    assert "chat1" not in manager._sessions


def test_stop_nonexistent(manager):
    assert manager.stop_session("nope") is False


def test_list_sessions(manager):
    """list_sessions returns all active sessions."""
    with patch("backend.services.session_manager.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        manager.send("chatA", "hi")
        manager.send("chatB", "hi")

    sessions = manager.list_sessions()
    assert len(sessions) == 2
    chat_ids = {s["chat_id"] for s in sessions}
    assert chat_ids == {"chatA", "chatB"}


# ── Busy flag ────────────────────────────────────────────────────

def test_busy_returns_immediately(manager):
    """If session is busy, send returns [BUSY] without blocking."""
    session = manager._get_or_create("chat1")
    with session.lock:
        session.busy = True

    result = manager.send("chat1", "msg")
    assert "[BUSY]" in result


# ── Error handling ───────────────────────────────────────────────

def test_claude_error(manager):
    """Claude CLI returning non-zero with stderr surfaces the error."""
    with patch("backend.services.session_manager.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="something broke")
        result = manager.send("chat1", "hi")

    assert "[ERROR]" in result
    assert "something broke" in result


def test_claude_timeout(manager):
    """Timeout is caught and returned as error message."""
    import subprocess

    with patch("backend.services.session_manager.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=300)
        result = manager.send("chat1", "hi")

    assert "[ERROR]" in result
    assert "timed out" in result


def test_busy_cleared_after_error(manager):
    """busy flag is cleared even when Claude CLI fails."""
    with patch("backend.services.session_manager.subprocess.run") as mock_run:
        mock_run.side_effect = Exception("boom")
        manager.send("chat1", "hi")

    session = manager._sessions["chat1"]
    assert session.busy is False


# ── first_done detection ─────────────────────────────────────────

def test_first_done_from_existing_jsonl(manager, tmp_session_dir):
    """If Claude has existing session files for CWD, first_done is True."""
    cwd = os.path.join(str(tmp_session_dir), "chat_resume")
    mangled = cwd.replace("/", "-")
    session_dir = Path.home() / ".claude" / "projects" / mangled
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "abc123.jsonl").write_text("{}")

    try:
        session = manager._get_or_create("chat_resume")
        assert session.first_done is True
    finally:
        import shutil
        shutil.rmtree(str(session_dir), ignore_errors=True)


def test_first_done_false_for_new_session(manager, tmp_session_dir):
    """New session without existing JSONL has first_done=False."""
    session = manager._get_or_create("brand_new")
    assert session.first_done is False


# ── CWD isolation ────────────────────────────────────────────────

def test_different_chats_different_cwds(manager, tmp_session_dir):
    """Each chat_id gets a unique CWD."""
    with patch("backend.services.session_manager.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        manager.send("chatA", "hi")
        manager.send("chatB", "hi")

    calls = mock_run.call_args_list
    cwd_a = calls[0][1]["cwd"]
    cwd_b = calls[1][1]["cwd"]
    assert cwd_a != cwd_b
    assert "chatA" in cwd_a
    assert "chatB" in cwd_b


# ── Idle cleanup ─────────────────────────────────────────────────

def test_cleanup_removes_idle_sessions(manager):
    """Idle sessions past timeout are cleaned up."""
    with patch("backend.services.session_manager.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        manager.send("idle_chat", "hi")

    # Artificially age the session
    manager._sessions["idle_chat"].last_active = time.time() - 99999

    with patch("backend.services.session_manager.SESSION_IDLE_TIMEOUT", 100):
        manager._cleanup_idle()

    assert "idle_chat" not in manager._sessions


def test_cleanup_keeps_active_sessions(manager):
    """Recently active sessions are NOT cleaned up."""
    with patch("backend.services.session_manager.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        manager.send("active_chat", "hi")

    manager._cleanup_idle()
    assert "active_chat" in manager._sessions


def test_cleanup_skips_busy_sessions(manager):
    """Busy sessions are never cleaned up even if idle."""
    session = manager._get_or_create("busy_chat")
    session.busy = True
    session.last_active = time.time() - 99999

    with patch("backend.services.session_manager.SESSION_IDLE_TIMEOUT", 100):
        manager._cleanup_idle()

    assert "busy_chat" in manager._sessions
