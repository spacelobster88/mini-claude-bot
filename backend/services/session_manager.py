"""Multi-session manager for Claude CLI gateway.

Each chat_id gets an isolated Claude CLI session via unique CWD.
Claude CLI uses CWD to determine the "project", so --continue
only resumes the session for that specific CWD.
"""

import glob
import logging
import os
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SESSION_BASE_DIR = os.getenv("GATEWAY_SESSION_DIR", "/tmp/claude-gateway-sessions")
SESSION_IDLE_TIMEOUT = int(os.getenv("GATEWAY_SESSION_TIMEOUT", "7200"))  # 2 hours
CLAUDE_TIMEOUT = int(os.getenv("GATEWAY_CLAUDE_TIMEOUT", "900"))  # 15 minutes
BUSY_STUCK_TIMEOUT = int(os.getenv("GATEWAY_BUSY_STUCK_TIMEOUT", "1800"))  # 30 minutes


@dataclass
class GatewaySession:
    chat_id: str
    cwd: str
    first_done: bool = False
    busy: bool = False
    busy_since: float = 0.0
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

    def _cleanup_session_files(self, session: GatewaySession) -> None:
        """Clean up CWD directory and Claude CLI session files."""
        try:
            shutil.rmtree(session.cwd, ignore_errors=True)
        except Exception:
            pass
        mangled = session.cwd.replace("/", "-")
        session_dir = Path.home() / ".claude" / "projects" / mangled
        try:
            shutil.rmtree(str(session_dir), ignore_errors=True)
        except Exception:
            pass

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
                # Auto-recover from stuck busy state
                stuck_duration = time.time() - session.busy_since
                if stuck_duration > BUSY_STUCK_TIMEOUT:
                    logger.warning(
                        "Session chat_id=%s stuck busy for %ds, force-resetting",
                        chat_id, int(stuck_duration),
                    )
                    session.busy = False
                else:
                    return "[BUSY] Still processing the previous message, please wait."
            session.busy = True
            session.busy_since = time.time()

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

            # Use Popen instead of run() to ensure proper cleanup on timeout
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=session.cwd,
                env=env,
                start_new_session=True,  # own process group for clean kill
            )

            try:
                stdout, stderr = proc.communicate(timeout=CLAUDE_TIMEOUT)
            except subprocess.TimeoutExpired:
                # Kill the entire process group to avoid zombie children
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
                try:
                    proc.kill()
                except OSError:
                    pass
                proc.wait(timeout=10)
                return f"[ERROR] Claude timed out after {CLAUDE_TIMEOUT}s"

            with session.lock:
                session.first_done = True
                session.last_active = time.time()

            if proc.returncode != 0 and stderr:
                return f"[ERROR] {stderr.strip()}"

            return stdout.strip() or "(empty response)"

        except Exception as e:
            return f"[ERROR] {e}"
        finally:
            with session.lock:
                session.busy = False
                session.busy_since = 0.0

    def stop_session(self, chat_id: str) -> bool:
        """Stop and clean up a session."""
        with self._global_lock:
            session = self._sessions.pop(chat_id, None)
            if session is None:
                return False

        self._cleanup_session_files(session)
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
                    "busy_seconds": int(now - s.busy_since) if s.busy else 0,
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
            self._recover_stuck_sessions()

    def _cleanup_idle(self):
        now = time.time()
        removed_sessions = []
        with self._global_lock:
            to_remove = []
            for chat_id, session in self._sessions.items():
                if not session.busy and (now - session.last_active) > SESSION_IDLE_TIMEOUT:
                    to_remove.append(chat_id)
            for chat_id in to_remove:
                removed_sessions.append(self._sessions.pop(chat_id))
                logger.info("Cleaned up idle session chat_id=%s", chat_id)
        # Clean up files outside the lock
        for session in removed_sessions:
            self._cleanup_session_files(session)

    def _recover_stuck_sessions(self):
        """Auto-recover sessions stuck in busy state beyond the timeout."""
        now = time.time()
        with self._global_lock:
            for chat_id, session in self._sessions.items():
                with session.lock:
                    if session.busy and session.busy_since > 0:
                        stuck_duration = now - session.busy_since
                        if stuck_duration > BUSY_STUCK_TIMEOUT:
                            logger.warning(
                                "Auto-recovering stuck session chat_id=%s (busy for %ds)",
                                chat_id, int(stuck_duration),
                            )
                            session.busy = False
                            session.busy_since = 0.0


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
