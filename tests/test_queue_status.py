"""Unit tests for the /queue status (get_queue_status)."""

import time
from unittest.mock import patch

import pytest

from backend.services.session_manager import (
    BUSY_STUCK_TIMEOUT,
    MAX_CLAUDE_PROCESSES,
    QUEUE_WAIT_TIMEOUT,
    SessionManager,
)


@pytest.fixture
def tmp_session_dir(tmp_path):
    with patch("backend.services.session_manager.SESSION_BASE_DIR", str(tmp_path)):
        yield tmp_path


@pytest.fixture
def manager(tmp_session_dir):
    mgr = SessionManager()
    yield mgr
    mgr.stop_cleanup_loop()


def test_queue_status_idle(manager):
    """No session (or not busy) → busy False, no in-flight detail, 0 wait."""
    st = manager.get_queue_status("nobody")
    assert st["busy"] is False
    assert st["busy_message"] == ""
    assert st["elapsed_seconds"] == 0
    assert st["queue_wait_remaining"] == 0
    assert st["slots_max"] == MAX_CLAUDE_PROCESSES
    assert st["queue_wait_timeout"] == QUEUE_WAIT_TIMEOUT


def test_queue_status_busy_reports_inflight(manager):
    """A busy session reports its in-flight message preview + elapsed + wait estimate."""
    sess = manager._get_or_create("chat-q", bot_id="default")
    sess.busy = True
    sess.busy_since = time.time() - 30
    sess.busy_message = "deploy the staging environment"

    st = manager.get_queue_status("chat-q")
    assert st["busy"] is True
    assert "deploy the staging environment" in st["busy_message"]
    assert st["elapsed_seconds"] >= 29
    # worst-case wait until the slot frees = stuck-reset minus elapsed
    assert st["queue_wait_remaining"] == max(0, BUSY_STUCK_TIMEOUT - st["elapsed_seconds"])
    assert 0 <= st["queue_wait_remaining"] <= BUSY_STUCK_TIMEOUT


def test_queue_status_bot_isolation(manager):
    """Busy session under one bot_id does not leak into another bot_id's view."""
    sess = manager._get_or_create("chat-iso", bot_id="botA")
    sess.busy = True
    sess.busy_since = time.time()
    sess.busy_message = "secret task"

    assert manager.get_queue_status("chat-iso", bot_id="botA")["busy"] is True
    assert manager.get_queue_status("chat-iso", bot_id="botB")["busy"] is False
