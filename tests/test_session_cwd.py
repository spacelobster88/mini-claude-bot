"""Tests for _get_or_create CWD recreation when directory is deleted."""

import os
import tempfile
import threading

import pytest

from backend.services.session_manager import GatewaySession, SessionManager


@pytest.fixture(autouse=True)
def fresh_db():
    """Override the conftest fresh_db fixture — these tests don't need a DB."""
    yield None


def _make_manager() -> SessionManager:
    """Create a SessionManager without running __init__ (avoids DB/cleanup)."""
    mgr = SessionManager.__new__(SessionManager)
    mgr._sessions = {}
    mgr._bg_tasks = {}
    mgr._global_lock = threading.Lock()
    mgr._running = False
    mgr._cleanup_thread = None
    mgr._claude_process_count = 0
    mgr._process_count_lock = threading.Lock()
    return mgr


class TestGetOrCreateCwdRecreation:
    """Verify _get_or_create recreates a deleted CWD for existing sessions."""

    def test_existing_session_deleted_cwd_is_recreated(self, tmp_path):
        """Session exists in _sessions, CWD directory deleted -> recreated."""
        mgr = _make_manager()
        chat_id = "test-chat-1"
        bot_id = "default"
        cwd = str(tmp_path / "session-cwd")
        os.makedirs(cwd)

        session = GatewaySession(chat_id=chat_id, cwd=cwd, bot_id=bot_id)
        key = SessionManager._session_key(bot_id, chat_id)
        mgr._sessions[key] = session

        # Delete the CWD directory
        os.rmdir(cwd)
        assert not os.path.exists(cwd)

        # _get_or_create should recreate it
        result = mgr._get_or_create(chat_id, bot_id=bot_id)
        assert result is session
        assert os.path.isdir(cwd)

    def test_existing_session_cwd_exists_no_error(self, tmp_path):
        """Session exists in _sessions, CWD directory still exists -> no error."""
        mgr = _make_manager()
        chat_id = "test-chat-2"
        bot_id = "default"
        cwd = str(tmp_path / "session-cwd")
        os.makedirs(cwd)

        session = GatewaySession(chat_id=chat_id, cwd=cwd, bot_id=bot_id)
        key = SessionManager._session_key(bot_id, chat_id)
        mgr._sessions[key] = session

        result = mgr._get_or_create(chat_id, bot_id=bot_id)
        assert result is session
        assert os.path.isdir(cwd)

    def test_new_session_creates_directory(self, tmp_path, monkeypatch):
        """New session (not in _sessions) -> directory created normally."""
        mgr = _make_manager()
        chat_id = "test-chat-3"
        bot_id = "default"

        # Point SESSION_BASE_DIR to a temp location so the new session CWD
        # is created under tmp_path instead of the real home directory.
        import backend.services.session_manager as sm
        monkeypatch.setattr(sm, "SESSION_BASE_DIR", str(tmp_path / "sessions"))

        # Mock helpers that touch DB / filesystem for Claude sessions
        monkeypatch.setattr(mgr, "_has_existing_claude_session", lambda cwd: False)
        monkeypatch.setattr(mgr, "_persist_session", lambda session: None)

        result = mgr._get_or_create(chat_id, bot_id=bot_id)

        expected_cwd = os.path.join(str(tmp_path / "sessions"), bot_id, chat_id)
        assert result.cwd == expected_cwd
        assert os.path.isdir(expected_cwd)
        assert result.chat_id == chat_id
        assert result.bot_id == bot_id
