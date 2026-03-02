"""Multi-session manager for Claude CLI gateway.

Each chat_id gets an isolated Claude CLI session via unique CWD.
Claude CLI uses CWD to determine the "project", so --continue
only resumes the session for that specific CWD.
"""

import glob
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SESSION_BASE_DIR = os.getenv("GATEWAY_SESSION_DIR", "/tmp/claude-gateway-sessions")
SESSION_IDLE_TIMEOUT = int(os.getenv("GATEWAY_SESSION_TIMEOUT", "7200"))  # 2 hours
CLAUDE_TIMEOUT = int(os.getenv("GATEWAY_CLAUDE_TIMEOUT", "300"))  # 5 minutes


@dataclass
class GatewaySession:
    chat_id: str
    cwd: str
    first_done: bool = False
    busy: bool = False
    last_active: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, GatewaySession] = {}
        self._global_lock = threading.Lock()
        self._running = False
        self._cleanup_thread: threading.Thread | None = None

    def _has_existing_claude_session(self, cwd: str) -> bool:
        """Check if Claude CLI has existing session files for this CWD."""
        # Claude mangles CWD path: /tmp/foo/bar → -tmp-foo-bar
        mangled = cwd.replace("/", "-")
        session_dir = Path.home() / ".claude" / "projects" / mangled
        if session_dir.exists():
            return len(list(session_dir.glob("*.jsonl"))) > 0
        return False

    def _get_or_create(self, chat_id: str) -> GatewaySession:
        with self._global_lock:
            if chat_id not in self._sessions:
                cwd = os.path.join(SESSION_BASE_DIR, chat_id)
                os.makedirs(cwd, exist_ok=True)
                session = GatewaySession(chat_id=chat_id, cwd=cwd)
                session.first_done = self._has_existing_claude_session(cwd)
                self._sessions[chat_id] = session
                logger.info(
                    "Created session chat_id=%s cwd=%s first_done=%s",
                    chat_id, cwd, session.first_done,
                )
            return self._sessions[chat_id]

    def send(self, chat_id: str, message: str) -> str:
        """Send a message to Claude CLI for the given chat. Blocking call."""
        session = self._get_or_create(chat_id)

        with session.lock:
            if session.busy:
                return "[BUSY] Still processing the previous message, please wait."
            session.busy = True

        try:
            args = [
                "claude", "-p",
                "--output-format", "text",
                "--dangerously-skip-permissions",
            ]
            if session.first_done:
                args.append("--continue")
            args.append(message)

            # Clean env: remove CLAUDECODE to avoid "nested session" error
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT,
                cwd=session.cwd,
                env=env,
            )

            with session.lock:
                session.first_done = True
                session.last_active = time.time()

            if result.returncode != 0 and result.stderr:
                return f"[ERROR] {result.stderr.strip()}"

            return result.stdout.strip() or "(empty response)"

        except subprocess.TimeoutExpired:
            return f"[ERROR] Claude timed out after {CLAUDE_TIMEOUT}s"
        except Exception as e:
            return f"[ERROR] {e}"
        finally:
            with session.lock:
                session.busy = False

    def stop_session(self, chat_id: str) -> bool:
        """Stop and clean up a session."""
        with self._global_lock:
            session = self._sessions.pop(chat_id, None)
            if session is None:
                return False

        # Clean up CWD directory
        try:
            shutil.rmtree(session.cwd, ignore_errors=True)
        except Exception:
            pass

        # Clean up Claude CLI session files
        mangled = session.cwd.replace("/", "-")
        session_dir = Path.home() / ".claude" / "projects" / mangled
        try:
            shutil.rmtree(str(session_dir), ignore_errors=True)
        except Exception:
            pass

        logger.info("Stopped session chat_id=%s", chat_id)
        return True

    def list_sessions(self) -> list[dict]:
        with self._global_lock:
            now = time.time()
            return [
                {
                    "chat_id": s.chat_id,
                    "busy": s.busy,
                    "first_done": s.first_done,
                    "last_active": s.last_active,
                    "idle_seconds": int(now - s.last_active),
                }
                for s in self._sessions.values()
            ]

    def start_cleanup_loop(self):
        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True
        )
        self._cleanup_thread.start()

    def stop_cleanup_loop(self):
        self._running = False

    def _cleanup_loop(self):
        while self._running:
            time.sleep(300)  # check every 5 minutes
            self._cleanup_idle()

    def _cleanup_idle(self):
        now = time.time()
        to_remove = []
        with self._global_lock:
            for chat_id, session in self._sessions.items():
                if not session.busy and (now - session.last_active) > SESSION_IDLE_TIMEOUT:
                    to_remove.append(chat_id)
            for chat_id in to_remove:
                del self._sessions[chat_id]
                logger.info("Cleaned up idle session chat_id=%s", chat_id)


# Module-level singleton
_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager()
        _manager.start_cleanup_loop()
    return _manager


def shutdown_session_manager():
    global _manager
    if _manager is not None:
        _manager.stop_cleanup_loop()
        _manager = None
