"""Unit tests for the gateway session manager."""

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.services.session_manager import (
    BUSY_STUCK_TIMEOUT,
    GatewaySession,
    MAX_DECOMPOSITION_RETRIES,
    QUEUE_WAIT_TIMEOUT,
    SessionManager,
    _busy_detail,
    _detect_harness_auto_mode,
    _harness_auto_mode_enabled,
)


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


# ── Issue #9: empty tasks detection on chain/resume ─────────────


def test_read_harness_progress_empty_tasks(manager, tmp_session_dir):
    """_read_harness_progress returns total=0 when tasks array is empty."""
    import json

    cwd = str(tmp_session_dir / "default" / "test_empty")
    harness_dir = Path(cwd) / ".harness"
    harness_dir.mkdir(parents=True)
    tasks_file = harness_dir / "tasks.json"
    tasks_file.write_text(json.dumps({
        "metadata": {"project_name": "test-proj", "current_phase": "init"},
        "tasks": [],
    }))

    progress = SessionManager._read_harness_progress(cwd)
    assert progress is not None
    assert progress["total"] == 0
    assert progress["done"] == 0
    assert progress["project_name"] == "test-proj"


def test_read_harness_progress_with_tasks(manager, tmp_session_dir):
    """_read_harness_progress returns correct counts for non-empty tasks."""
    import json

    cwd = str(tmp_session_dir / "default" / "test_with_tasks")
    harness_dir = Path(cwd) / ".harness"
    harness_dir.mkdir(parents=True)
    tasks_file = harness_dir / "tasks.json"
    tasks_file.write_text(json.dumps({
        "metadata": {"project_name": "test-proj"},
        "tasks": [
            {"id": "1", "status": "done", "phase": "build"},
            {"id": "2", "status": "pending", "phase": "build"},
        ],
    }))

    progress = SessionManager._read_harness_progress(cwd)
    assert progress is not None
    assert progress["total"] == 2
    assert progress["done"] == 1
    assert progress["pending"] == 1


def test_max_decomposition_retries_constant():
    """MAX_DECOMPOSITION_RETRIES is a positive integer for circuit-breaker."""
    assert isinstance(MAX_DECOMPOSITION_RETRIES, int)
    assert MAX_DECOMPOSITION_RETRIES > 0


# ── [BUSY] message enrichment (#4) and queue timeout (#5) ────────────

def test_busy_detail_empty_when_idle():
    """A fresh session with no in-flight work yields no detail suffix."""
    s = GatewaySession(chat_id="c", cwd="/tmp")
    assert _busy_detail(s) == ""


def test_busy_detail_includes_message_preview():
    """The detail suffix names the message currently being processed."""
    s = GatewaySession(chat_id="c", cwd="/tmp")
    s.busy = True
    s.busy_since = time.time() - 12
    s.busy_message = "deploy the staging environment"
    detail = _busy_detail(s)
    assert 'processing: "deploy the staging environment"' in detail
    assert "running" in detail  # elapsed time is reported
    # Suffix is parenthesized so it appends cleanly to the base [BUSY] text.
    assert detail.startswith(" (") and detail.endswith(")")


def test_busy_detail_elapsed_only_without_message():
    """If we have a start time but no preview, still report elapsed time."""
    s = GatewaySession(chat_id="c", cwd="/tmp")
    s.busy_since = time.time() - 5
    detail = _busy_detail(s)
    assert "running" in detail
    assert "processing" not in detail


def test_send_records_busy_message_preview(manager):
    """send() stamps the session with a truncated preview of the in-flight message."""
    long_msg = "x" * 200
    with patch("backend.services.session_manager.subprocess.Popen", _mock_popen(stdout="done")):
        manager.send("chat-busy", long_msg)
    session = manager._sessions[manager._session_key("default", "chat-busy")]
    assert session.busy_message == "x" * 80  # truncated to 80 chars


def test_queue_wait_timeout_configurable_and_sane():
    """Queue wait is env-configurable (#5) and defaults to a value > the old 120s."""
    assert isinstance(QUEUE_WAIT_TIMEOUT, int)
    assert QUEUE_WAIT_TIMEOUT >= 120
    # Must stay below the stuck-busy safety net so queued waiters don't outlive recovery.
    assert QUEUE_WAIT_TIMEOUT <= BUSY_STUCK_TIMEOUT


# ── Issue #12: background status reliability ──────────────────────

def _fake_bg_task(manager, chat_id, project_id, *, thread, status="running", cwd=None):
    """Insert a synthetic bg task and return its key."""
    bg_key = manager._bg_task_key("default", chat_id, project_id)
    manager._bg_tasks[bg_key] = {
        "thread": thread,
        "message": "doing work",
        "started_at": time.time() - 5,
        "status": status,
        "result": None,
        "chain_depth": 0,
        "project_id": project_id,
        "cwd": cwd,
    }
    return bg_key


def test_get_background_status_dead_thread_reported_failed(manager):
    """A task stuck at 'running' with a dead worker thread is never reported running."""
    import threading
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()  # thread is now finished → not alive
    bg_key = _fake_bg_task(manager, "chatX", "proj", thread=dead)

    status = manager.get_background_status("chatX", project_id="proj")
    assert status["status"] == "failed"
    # The stale entry is repaired in place so future reads agree.
    assert manager._bg_tasks[bg_key]["status"] == "failed"


def test_get_background_status_live_thread_stays_running(manager):
    """A genuinely-alive worker thread is still reported as running."""
    import threading
    ev = threading.Event()
    alive = threading.Thread(target=ev.wait)
    alive.start()
    try:
        _fake_bg_task(manager, "chatY", "proj", thread=alive)
        status = manager.get_background_status("chatY", project_id="proj")
        assert status["status"] == "running"
        assert status["message"] == "doing work"
    finally:
        ev.set()
        alive.join()


def test_get_background_status_merges_harness_progress(manager, tmp_session_dir):
    """When a .harness/tasks.json exists, /status data includes phase/done/total."""
    import json
    import threading
    cwd = str(tmp_session_dir / "default" / "bgproj")
    harness_dir = Path(cwd) / ".harness"
    harness_dir.mkdir(parents=True)
    (harness_dir / "tasks.json").write_text(json.dumps({
        "metadata": {"project_name": "p", "current_phase": "build"},
        "tasks": [
            {"id": "1", "status": "done", "phase": "build"},
            {"id": "2", "status": "pending", "phase": "build"},
        ],
    }))
    ev = threading.Event()
    alive = threading.Thread(target=ev.wait)
    alive.start()
    try:
        _fake_bg_task(manager, "chatZ", "proj", thread=alive, cwd=cwd)
        status = manager.get_background_status("chatZ", project_id="proj")
        assert "harness" in status
        assert status["harness"]["total"] == 2
        assert status["harness"]["done"] == 1
        assert status["harness"]["current_phase"] == "build"
    finally:
        ev.set()
        alive.join()


def test_send_background_always_reaches_terminal_status(manager):
    """_run() wraps work in try/finally so status is never left at 'running' (Fix A1)."""
    with patch.object(manager, "send", side_effect=RuntimeError("boom")):
        result = manager.send_background("chatT", "do it", bot_token="")
    assert result["status"] == "started"
    tasks = manager._find_bg_tasks_for_chat("default", "chatT")
    assert tasks
    task = next(iter(tasks.values()))
    task["thread"].join(timeout=5)
    assert task["status"] == "failed"
    assert task["status"] != "running"


# ── Issue #11: harness auto vs normal mode ───────────────────────

@pytest.mark.parametrize("msg", [
    "auto mode: build me a thing",
    "AUTO MODE please",
    "automode",
    "run /auto now",
    "auto-mode",
    "auto_mode",
    "自动模式启动",
])
def test_detect_auto_mode_positive(msg):
    """Explicit auto-mode tokens are detected."""
    assert _detect_harness_auto_mode(msg) is True


@pytest.mark.parametrize("msg", [
    "please automate the deployment",
    "set up auto-commit hooks",
    "make it automatic",
    "this is an automobile",
    "a normal harness task",
    "autonomous agents",
    "",
])
def test_detect_auto_mode_negative(msg):
    """Look-alikes ('automate', 'auto-commit', 'automatic') must NOT trigger auto mode."""
    assert _detect_harness_auto_mode(msg) is False


def test_harness_auto_mode_disabled_by_default(monkeypatch):
    """With the env flag unset, auto-mode routing is off (safe default)."""
    monkeypatch.delenv("GATEWAY_HARNESS_AUTO_MODE", raising=False)
    assert _harness_auto_mode_enabled() is False


def test_harness_prompt_legacy_when_flag_off(manager, monkeypatch):
    """Flag OFF reproduces the original prompt verbatim, even with an auto token present."""
    monkeypatch.delenv("GATEWAY_HARNESS_AUTO_MODE", raising=False)
    prompt = manager._harness_mode_prompt("auto mode build it")
    assert prompt == SessionManager._HARNESS_PROMPT_LEGACY
    assert "NEVER output [HARNESS_EXEC_READY] on the first response" in prompt


def test_harness_prompt_normal_when_flag_on_no_token(manager, monkeypatch):
    """Flag ON + plain text → grill-me normal interview, confirmation gate intact."""
    monkeypatch.setenv("GATEWAY_HARNESS_AUTO_MODE", "1")
    prompt = manager._harness_mode_prompt("build me a normal feature")
    assert "NORMAL MODE" in prompt
    assert "grill-me" in prompt.lower()
    assert "NEVER output [HARNESS_EXEC_READY] on the first response" in prompt


def test_harness_prompt_auto_when_flag_on_with_token(manager, monkeypatch):
    """Flag ON + auto token → self-grill, first-response marker, no confirm gate."""
    monkeypatch.setenv("GATEWAY_HARNESS_AUTO_MODE", "1")
    prompt = manager._harness_mode_prompt("auto mode: ship the fix")
    assert "AUTO MODE" in prompt
    assert "SELF-GRILL" in prompt
    assert "FIRST response" in prompt
    assert "NEVER output [HARNESS_EXEC_READY] on the first response" not in prompt


# ── Issue #10: faster context reload after a reap ────────────────

def test_prune_keeps_newest_and_spares_inflight(manager, tmp_path, monkeypatch):
    """Pruning keeps the newest `keep` transcripts; the in-flight (newest) survives."""
    monkeypatch.setenv("HOME", str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    session = GatewaySession(chat_id="c", cwd=str(work))
    mangled = manager._mangle_cwd(str(work))
    proj = tmp_path / ".claude" / "projects" / mangled
    proj.mkdir(parents=True)
    for i in range(5):
        fp = proj / f"sess{i}.jsonl"
        fp.write_text("{}")
        os.utime(fp, (1000 + i, 1000 + i))  # increasing mtime → sess4 is newest

    manager._prune_session_history(session, keep=2)

    remaining = sorted(p.name for p in proj.glob("*.jsonl"))
    assert remaining == ["sess3.jsonl", "sess4.jsonl"]  # newest two kept, in-flight safe


def test_prune_runs_even_on_failure(manager):
    """_prune_session_history is called regardless of CLI return code (issue #10)."""
    with patch.object(manager, "_prune_session_history") as prune, \
         patch("backend.services.session_manager.subprocess.Popen",
               _mock_popen(stdout="partial", stderr="boom", returncode=1)):
        manager.send("chat-fail", "hi")
    assert prune.called


def test_static_cache_serves_unchanged_mtime(manager, tmp_path):
    """A static file is read once and served from cache while its mtime is unchanged."""
    f = tmp_path / "static.md"
    f.write_text("v1")
    st = os.stat(str(f))

    assert manager._read_static_cached(str(f)) == "v1"
    # Rewrite content but restore the original mtime → cache must still return v1.
    f.write_text("v2")
    os.utime(str(f), (st.st_atime, st.st_mtime))
    assert manager._read_static_cached(str(f)) == "v1"


def test_static_cache_reloads_on_mtime_change(manager, tmp_path):
    """The cache is invalidated when the file's mtime advances."""
    f = tmp_path / "static.md"
    f.write_text("v1")
    assert manager._read_static_cached(str(f)) == "v1"

    f.write_text("v2")
    st = os.stat(str(f))
    os.utime(str(f), (st.st_atime, st.st_mtime + 10))  # bump mtime forward
    assert manager._read_static_cached(str(f)) == "v2"


def test_static_cache_missing_returns_none(manager, tmp_path):
    """Missing files yield None (and don't raise)."""
    assert manager._read_static_cached(str(tmp_path / "nope.md")) is None


def test_static_cache_respects_max_len(manager, tmp_path):
    """max_len truncates the returned content (used to cap global-memory)."""
    f = tmp_path / "big.md"
    f.write_text("x" * 100)
    assert manager._read_static_cached(str(f), max_len=10) == "x" * 10


def test_reload_perf_logs_once_for_cold_start(manager, caplog):
    """RELOAD_PERF is emitted only on the first post-reap message, then suppressed."""
    import logging
    session = GatewaySession(chat_id="c", cwd="/tmp")
    session.created_from_disk = True  # simulate a reaped→reloaded session
    with caplog.at_level(logging.INFO):
        manager._log_reload_perf(session, inject_s=0.01, cli_s=1.5)
    assert any("RELOAD_PERF" in r.message for r in caplog.records)
    assert session.created_from_disk is False  # flag cleared

    caplog.clear()
    with caplog.at_level(logging.INFO):
        manager._log_reload_perf(session, inject_s=0.01, cli_s=1.5)
    assert not any("RELOAD_PERF" in r.message for r in caplog.records)


def test_idle_timeout_raised_for_fewer_reloads():
    """Interactive idle timeout default was raised to reduce cold reloads (issue #10)."""
    from backend.services.session_manager import SESSION_IDLE_TIMEOUT
    assert SESSION_IDLE_TIMEOUT >= 7200
