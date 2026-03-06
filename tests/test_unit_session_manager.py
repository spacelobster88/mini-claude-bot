"""Unit tests for the gateway session manager."""

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.services.session_manager import GatewaySession, SessionManager


def _mock_popen(stdout="ok", stderr="", returncode=0, side_effect=None):
    """Create a mock Popen that returns the given stdout/stderr via communicate()."""
    mock = MagicMock()
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 12345
    if side_effect:
        mock.side_effect = side_effect
    else:
        mock.return_value = proc
    return mock


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
    with patch("backend.services.session_manager.subprocess.Popen", _mock_popen(stdout="hello")):
        result = manager.send("chat123", "hi")

    assert result == "hello"
    assert "default:chat123" in manager._sessions
    assert os.path.isdir(os.path.join(str(tmp_session_dir), "default", "chat123"))


def test_session_reuse(manager):
    """Subsequent sends reuse the same session with --continue."""
    call_args = []

    def capture_popen(*args, **kwargs):
        # Only capture claude CLI calls, not subprocess.run internals (e.g. vm_stat)
        if args and args[0] and isinstance(args[0], list) and args[0][0] == "claude":
            call_args.append((args, kwargs))
        proc = MagicMock()
        proc.communicate.return_value = ("ok", "")
        proc.returncode = 0
        proc.pid = 12345
        return proc

    with patch("backend.services.session_manager.subprocess.Popen", side_effect=capture_popen):
        manager.send("chat1", "msg1")
        manager.send("chat1", "msg2")

    # Second call should have used --continue
    assert "--continue" not in call_args[0][0][0]
    assert "--continue" in call_args[1][0][0]


def test_stop_session(manager):
    """stop_session removes session from dict."""
    with patch("backend.services.session_manager.subprocess.Popen", _mock_popen()):
        manager.send("chat1", "hi")

    assert manager.stop_session("chat1") is True
    assert "default:chat1" not in manager._sessions


def test_stop_nonexistent(manager):
    assert manager.stop_session("nope") is False


def test_list_sessions(manager):
    """list_sessions returns all active sessions."""
    with patch("backend.services.session_manager.subprocess.Popen", _mock_popen()):
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
        session.busy_since = time.time()  # recent, not stuck

    result = manager.send("chat1", "msg")
    assert "[BUSY]" in result


def test_busy_auto_recovers_after_stuck_timeout(manager):
    """If session is stuck busy beyond BUSY_STUCK_TIMEOUT, auto-recover."""
    session = manager._get_or_create("chat1")
    with session.lock:
        session.busy = True
        session.busy_since = time.time() - 99999  # way past timeout

    with patch("backend.services.session_manager.subprocess.Popen", _mock_popen(stdout="recovered")):
        result = manager.send("chat1", "msg")

    assert result == "recovered"
    assert session.busy is False


# ── Error handling ───────────────────────────────────────────────

def test_claude_error(manager):
    """Claude CLI returning non-zero with stderr surfaces the error."""
    with patch("backend.services.session_manager.subprocess.Popen",
               _mock_popen(returncode=1, stdout="", stderr="something broke")):
        result = manager.send("chat1", "hi")

    assert "[ERROR]" in result
    assert "something broke" in result


def test_claude_timeout(manager):
    """Timeout is caught and returned as error message."""
    import subprocess

    def timeout_popen(*args, **kwargs):
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=300)
        proc.pid = 12345
        proc.kill.return_value = None
        proc.wait.return_value = None
        return proc

    with patch("backend.services.session_manager.subprocess.Popen", side_effect=timeout_popen):
        with patch("backend.services.session_manager.os.killpg"):
            result = manager.send("chat1", "hi")

    assert "[ERROR]" in result
    assert "timed out" in result


def test_busy_cleared_after_error(manager):
    """busy flag is cleared even when Claude CLI fails."""
    def error_popen(*args, **kwargs):
        raise Exception("boom")

    with patch("backend.services.session_manager.subprocess.Popen", side_effect=error_popen):
        manager.send("chat1", "hi")

    session = manager._sessions["default:chat1"]
    assert session.busy is False


# ── first_done detection ─────────────────────────────────────────

def test_first_done_from_existing_jsonl(manager, tmp_session_dir):
    """If Claude has existing session files for CWD, first_done is True."""
    cwd = os.path.join(str(tmp_session_dir), "default", "chat_resume")
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
    call_args = []

    def capture_popen(*args, **kwargs):
        # Only capture claude CLI calls, not subprocess.run internals (e.g. vm_stat)
        if args and args[0] and isinstance(args[0], list) and args[0][0] == "claude":
            call_args.append((args, kwargs))
        proc = MagicMock()
        proc.communicate.return_value = ("ok", "")
        proc.returncode = 0
        proc.pid = 12345
        return proc

    with patch("backend.services.session_manager.subprocess.Popen", side_effect=capture_popen):
        manager.send("chatA", "hi")
        manager.send("chatB", "hi")

    cwd_a = call_args[0][1]["cwd"]
    cwd_b = call_args[1][1]["cwd"]
    assert cwd_a != cwd_b
    assert "chatA" in cwd_a
    assert "chatB" in cwd_b


# ── Idle cleanup ─────────────────────────────────────────────────

def test_cleanup_removes_idle_sessions(manager):
    """Idle sessions past timeout are cleaned up."""
    with patch("backend.services.session_manager.subprocess.Popen", _mock_popen()):
        manager.send("idle_chat", "hi")

    # Artificially age the session
    manager._sessions["default:idle_chat"].last_active = time.time() - 99999

    with patch("backend.services.session_manager.SESSION_IDLE_TIMEOUT", 100):
        manager._cleanup_idle()

    assert "default:idle_chat" not in manager._sessions


def test_cleanup_keeps_active_sessions(manager):
    """Recently active sessions are NOT cleaned up."""
    with patch("backend.services.session_manager.subprocess.Popen", _mock_popen()):
        manager.send("active_chat", "hi")

    manager._cleanup_idle()
    assert "default:active_chat" in manager._sessions


def test_cleanup_skips_busy_sessions(manager):
    """Busy sessions are never cleaned up even if idle."""
    session = manager._get_or_create("busy_chat")
    session.busy = True
    session.busy_since = time.time()
    session.last_active = time.time() - 99999

    with patch("backend.services.session_manager.SESSION_IDLE_TIMEOUT", 100):
        manager._cleanup_idle()

    assert "default:busy_chat" in manager._sessions


# ── Stuck recovery ───────────────────────────────────────────────

def test_recover_stuck_sessions(manager):
    """_recover_stuck_sessions resets sessions stuck beyond timeout."""
    session = manager._get_or_create("stuck_chat")
    session.busy = True
    session.busy_since = time.time() - 99999

    with patch("backend.services.session_manager.BUSY_STUCK_TIMEOUT", 100):
        manager._recover_stuck_sessions()

    assert session.busy is False
    assert session.busy_since == 0.0
