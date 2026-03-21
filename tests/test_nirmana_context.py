"""Tests for Nirmana persona injection in _inject_context()."""

import logging
import os
from unittest.mock import patch

import pytest

from backend.services.session_manager import SessionManager, GatewaySession

_real_expanduser = os.path.expanduser


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture
def session(tmp_path):
    """Create a GatewaySession with a valid CWD."""
    return GatewaySession(chat_id="test", cwd=str(tmp_path))


def _make_mock_expanduser(tmp_path):
    """Return a mock expanduser that redirects ~ paths to tmp_path."""
    def mock_expanduser(path):
        if path.startswith("~"):
            return str(tmp_path / path[2:])  # strip "~/"
        return _real_expanduser(path)
    return mock_expanduser


class TestNirmanaContextInjection:
    def test_persona_injected_when_nirmana_mode_true(self, manager, session, tmp_path):
        """When nirmana_mode=True and PERSONA.md exists, persona is prepended."""
        session.nirmana_mode = True
        persona_dir = tmp_path / "eddie-nirmana"
        persona_dir.mkdir()
        (persona_dir / "PERSONA.md").write_text("I am Eddie-Nirmana, the night guardian.")

        with patch("backend.services.session_manager.os.path.expanduser",
                   side_effect=_make_mock_expanduser(tmp_path)):
            result = manager._inject_context(session, "hello")

        assert "[Nirmana Persona]:" in result
        assert "I am Eddie-Nirmana, the night guardian." in result
        persona_pos = result.find("[Nirmana Persona]:")
        assert persona_pos < result.find("User message:")

    def test_no_persona_when_nirmana_mode_false(self, manager, session):
        """When nirmana_mode=False, no persona is injected."""
        session.nirmana_mode = False
        result = manager._inject_context(session, "hello")
        assert "[Nirmana Persona]:" not in result

    def test_graceful_when_persona_file_missing(self, manager, session, tmp_path, caplog):
        """When nirmana_mode=True but PERSONA.md doesn't exist, log warning and continue."""
        session.nirmana_mode = True
        # tmp_path exists but has no eddie-nirmana/PERSONA.md

        with caplog.at_level(logging.WARNING, logger="backend.services.session_manager"):
            with patch("backend.services.session_manager.os.path.expanduser",
                       side_effect=_make_mock_expanduser(tmp_path)):
                result = manager._inject_context(session, "hello")

        assert "[Nirmana Persona]:" not in result
        assert "PERSONA.md not found" in caplog.text

    def test_persona_is_first_context_part(self, manager, session, tmp_path):
        """Persona should be the very first context block, before global memory."""
        session.nirmana_mode = True
        # Create persona
        persona_dir = tmp_path / "eddie-nirmana"
        persona_dir.mkdir()
        (persona_dir / "PERSONA.md").write_text("Night guardian persona")
        # Create global memory
        mem_dir = tmp_path / ".mini-claude-bot"
        mem_dir.mkdir()
        (mem_dir / "global-memory.md").write_text("Some global memory")

        with patch("backend.services.session_manager.os.path.expanduser",
                   side_effect=_make_mock_expanduser(tmp_path)):
            result = manager._inject_context(session, "hello")

        persona_pos = result.find("[Nirmana Persona]:")
        memory_pos = result.find("[Global Memory]:")
        assert persona_pos != -1
        assert memory_pos != -1
        assert persona_pos < memory_pos, "Persona should appear before global memory"
