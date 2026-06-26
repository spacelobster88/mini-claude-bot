"""Tests for worktree-cwd + forced-auto-mode background exec (the /away core)."""

from unittest.mock import MagicMock, patch

import pytest

from backend.services.session_manager import GatewaySession, SessionManager


@pytest.fixture
def tmp_session_dir(tmp_path):
    with patch("backend.services.session_manager.SESSION_BASE_DIR", str(tmp_path)):
        yield tmp_path


@pytest.fixture
def manager(tmp_session_dir):
    mgr = SessionManager()
    yield mgr
    mgr.stop_cleanup_loop()


def test_force_auto_selects_auto_prompt(manager):
    """force_auto=True picks the AUTO prompt regardless of the global flag."""
    auto = manager._harness_mode_prompt("just do it", force_auto=True)
    assert "AUTO MODE" in auto
    # Default (flag off, no force) stays on the legacy prompt — no AUTO.
    legacy = manager._harness_mode_prompt("just do it", force_auto=False)
    assert "AUTO MODE" not in legacy


def test_inject_context_forces_auto_without_keyword_or_harness(manager, tmp_path):
    """A force_auto_mode session injects the AUTO prompt even with no harness dir
    and no harness keyword in the message."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    session = GatewaySession(chat_id="bg-x", cwd=str(wt), force_auto_mode=True)
    injected = manager._inject_context(session, "Implement the feature in this issue")
    assert "AUTO MODE" in injected

    # Same session without force → no AUTO prompt (no harness, no keyword).
    session.force_auto_mode = False
    injected2 = manager._inject_context(session, "Implement the feature in this issue")
    assert "AUTO MODE" not in injected2


def test_send_background_uses_custom_cwd_and_force_auto(manager, tmp_path):
    """send_background(cwd=..., force_auto_mode=True) configures the bg session
    with that worktree cwd and the force flag (thread patched so claude never runs)."""
    worktree = tmp_path / "wt-issue-1"

    with patch("backend.services.session_manager.threading.Thread") as MockThread:
        MockThread.return_value = MagicMock()
        res = manager.send_background(
            "chatX", "Implement issue #1", bot_token="x",
            cwd=str(worktree), force_auto_mode=True,
        )

    assert res["status"] == "started"
    assert worktree.exists()  # os.makedirs(cwd) ran

    bg_sessions = [s for s in manager._sessions.values() if s.chat_id.startswith("bg-chatX")]
    assert bg_sessions, "background session was not created"
    sess = bg_sessions[0]
    assert sess.cwd == str(worktree)
    assert sess.force_auto_mode is True


def test_send_background_default_cwd_unchanged(manager, tmp_path):
    """Without cwd/force_auto, behavior is unchanged: bg session shares main cwd, no force."""
    with patch("backend.services.session_manager.threading.Thread") as MockThread:
        MockThread.return_value = MagicMock()
        manager.send_background("chatY", "do work", bot_token="x")

    main = manager._sessions[manager._session_key("default", "chatY")]
    bg_sessions = [s for s in manager._sessions.values() if s.chat_id.startswith("bg-chatY")]
    assert bg_sessions
    assert bg_sessions[0].cwd == main.cwd
    assert bg_sessions[0].force_auto_mode is False
