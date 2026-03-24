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


def test_reap_returns_info(manager):
    """_reap_idle_sessions returns info about reaped sessions."""
    session = manager._get_or_create("reap_test")
    session.last_active = time.time() - 99999

    with patch("backend.services.session_manager.SESSION_IDLE_TIMEOUT", 100):
        reaped = manager._reap_idle_sessions()

    assert len(reaped) == 1
    assert reaped[0]["chat_id"] == "reap_test"
    assert reaped[0]["type"] == "interactive"
    assert reaped[0]["idle_seconds"] > 99000


def test_reap_priority_interactive_before_background(manager):
    """Interactive sessions are reaped before background sessions."""
    # Create an interactive session and a background session, both idle
    interactive = manager._get_or_create("interactive_chat")
    interactive.last_active = time.time() - 99999

    bg = manager._get_or_create("bg-task-123")
    bg.last_active = time.time() - 99999

    with patch("backend.services.session_manager.SESSION_IDLE_TIMEOUT", 100), \
         patch("backend.services.session_manager.BG_SESSION_IDLE_TIMEOUT", 100):
        reaped = manager._reap_idle_sessions()

    assert len(reaped) == 2
    # Interactive should be first in the list (higher priority to close)
    assert reaped[0]["type"] == "interactive"
    assert reaped[1]["type"] == "background"


def test_bg_sessions_use_longer_timeout(manager):
    """Background sessions use BG_SESSION_IDLE_TIMEOUT (longer than regular)."""
    bg = manager._get_or_create("bg-task-456")
    bg.last_active = time.time() - 5000  # idle 5000s

    # Regular timeout is 3600, bg timeout is 14400
    # Session idle 5000s should NOT be reaped with bg timeout of 14400
    with patch("backend.services.session_manager.SESSION_IDLE_TIMEOUT", 3600), \
         patch("backend.services.session_manager.BG_SESSION_IDLE_TIMEOUT", 14400):
        reaped = manager._reap_idle_sessions()

    assert len(reaped) == 0
    assert "default:bg-task-456" in manager._sessions


def test_reap_empty_when_no_idle(manager):
    """No sessions reaped when all are recent."""
    manager._get_or_create("fresh_chat")

    reaped = manager._reap_idle_sessions()
    assert len(reaped) == 0


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


# ── get_background_status ────────────────────────────────────────

def test_bg_status_idle_when_no_tasks(manager):
    """Returns idle when no background tasks exist."""
    result = manager.get_background_status("chat1")
    assert result == {"status": "idle"}


def test_bg_status_finds_task_with_project_id(manager):
    """Finds a running task stored with a 3-part key (bot_id:chat_id:project_id)."""
    bg_key = manager._bg_task_key("default", "chat1", "abc123")
    manager._bg_tasks[bg_key] = {
        "status": "running",
        "message": "test message",
        "started_at": time.time(),
        "result": None,
        "chain_depth": 0,
        "project_id": "abc123",
        "cwd": "/tmp/test",
        "thread": None,
    }
    result = manager.get_background_status("chat1")
    assert result["status"] == "running"
    assert result["message"] == "test message"


def test_bg_status_exact_lookup_with_project_id(manager):
    """When project_id is given, does exact lookup instead of prefix search."""
    # Insert two tasks for the same chat
    for pid in ("proj1", "proj2"):
        bg_key = manager._bg_task_key("default", "chat1", pid)
        manager._bg_tasks[bg_key] = {
            "status": "running" if pid == "proj1" else "completed",
            "message": f"msg-{pid}",
            "started_at": time.time(),
            "result": None if pid == "proj1" else "done",
            "chain_depth": 0,
            "project_id": pid,
            "cwd": "/tmp/test",
            "thread": None,
        }

    # Exact lookup for proj2 should return completed, not proj1
    result = manager.get_background_status("chat1", project_id="proj2")
    assert result["status"] == "completed"


def test_bg_status_returns_most_recent_without_project_id(manager):
    """Without project_id, returns the most recently started task."""
    now = time.time()
    for pid, offset in (("old", -100), ("new", -10)):
        bg_key = manager._bg_task_key("default", "chat1", pid)
        manager._bg_tasks[bg_key] = {
            "status": "running",
            "message": f"msg-{pid}",
            "started_at": now + offset,
            "result": None,
            "chain_depth": 0,
            "project_id": pid,
            "cwd": "/tmp/test",
            "thread": None,
        }

    result = manager.get_background_status("chat1")
    assert result["message"] == "msg-new"


def test_bg_status_idle_for_nonexistent_project_id(manager):
    """Returns idle when the specified project_id doesn't exist."""
    bg_key = manager._bg_task_key("default", "chat1", "exists")
    manager._bg_tasks[bg_key] = {
        "status": "running",
        "message": "test",
        "started_at": time.time(),
        "result": None,
        "chain_depth": 0,
        "project_id": "exists",
        "cwd": "/tmp/test",
        "thread": None,
    }
    result = manager.get_background_status("chat1", project_id="doesnt_exist")
    assert result == {"status": "idle"}


# ── nirmana_mode fields ──────────────────────────────────────────

def test_nirmana_mode_defaults():
    """GatewaySession defaults nirmana_mode=False, nirmana_activated_at=0.0."""
    session = GatewaySession(chat_id="test", cwd="/tmp/test")
    assert session.nirmana_mode is False
    assert session.nirmana_activated_at == 0.0


def test_nirmana_mode_can_be_set():
    """nirmana_mode can be set to True with a timestamp."""
    session = GatewaySession(chat_id="test", cwd="/tmp/test", nirmana_mode=True, nirmana_activated_at=1234567890.0)
    assert session.nirmana_mode is True
    assert session.nirmana_activated_at == 1234567890.0


def test_nirmana_mode_persist_roundtrip(manager):
    """nirmana_mode and nirmana_activated_at survive save/load round-trip."""
    session = manager._get_or_create("nirmana_chat")
    session.nirmana_mode = True
    session.nirmana_activated_at = 1700000000.0
    manager._persist_session(session)

    # Clear in-memory sessions and reload from DB
    manager._sessions.clear()
    manager._load_persisted_sessions()

    key = manager._session_key("default", "nirmana_chat")
    restored = manager._sessions[key]
    assert restored.nirmana_mode is True
    assert restored.nirmana_activated_at == 1700000000.0
