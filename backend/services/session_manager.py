"""Multi-session manager for Claude CLI gateway.

Each chat_id gets an isolated Claude CLI session via unique CWD.
Claude CLI uses CWD to determine the "project", so --continue
only resumes the session for that specific CWD.
"""

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
BUSY_STUCK_TIMEOUT = int(os.getenv("GATEWAY_BUSY_STUCK_TIMEOUT", "300"))  # 5 minutes (was 30m, too long)


@dataclass
class GatewaySession:
    chat_id: str
    cwd: str
    first_done: bool = False
    busy: bool = False
    busy_since: float = 0.0
    last_active: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _proc: subprocess.Popen | None = field(default=None, repr=False)


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, GatewaySession] = {}
        self._global_lock = threading.Lock()
        self._running = False
        self._cleanup_thread: threading.Thread | None = None
        self._load_persisted_sessions()

    def _get_db(self):
        """Lazy import to avoid circular dependency."""
        from backend.db.engine import get_db
        return get_db()

    def _get_write_lock(self):
        """Get DB write lock for thread-safe writes."""
        from backend.db.engine import db_write_lock
        return db_write_lock()

    def _load_persisted_sessions(self):
        """Load sessions from DB on startup. Reset busy flags (stale from crash)."""
        try:
            db = self._get_db()
            rows = db.execute("SELECT * FROM gateway_sessions").fetchall()
            for row in rows:
                row = dict(row)
                cwd = row["cwd"]
                os.makedirs(cwd, exist_ok=True)
                session = GatewaySession(
                    chat_id=row["chat_id"],
                    cwd=cwd,
                    first_done=bool(row["first_done"]),
                    busy=False,  # always reset on startup
                    last_active=row["last_active"],
                )
                self._sessions[row["chat_id"]] = session
                logger.info("Restored session chat_id=%s from DB", row["chat_id"])
        except Exception as e:
            # Table might not exist yet on first run
            logger.debug("Could not load persisted sessions: %s", e)

    def _persist_session(self, session: GatewaySession) -> None:
        """Write session state to DB (upsert). Thread-safe."""
        try:
            db = self._get_db()
            with self._get_write_lock():
                db.execute(
                    """INSERT OR REPLACE INTO gateway_sessions
                       (chat_id, cwd, first_done, busy, last_active)
                       VALUES (?, ?, ?, ?, ?)""",
                    (session.chat_id, session.cwd, int(session.first_done),
                     int(session.busy), session.last_active),
                )
                db.commit()
        except Exception as e:
            logger.debug("Could not persist session: %s", e)

    def _delete_persisted_session(self, chat_id: str) -> None:
        """Remove session from DB. Thread-safe."""
        try:
            db = self._get_db()
            with self._get_write_lock():
                db.execute("DELETE FROM gateway_sessions WHERE chat_id = ?", (chat_id,))
                db.commit()
        except Exception as e:
            logger.debug("Could not delete persisted session: %s", e)

    def _has_existing_claude_session(self, cwd: str) -> bool:
        """Check if Claude CLI has existing session files for this CWD."""
        mangled = self._mangle_cwd(cwd)
        session_dir = Path.home() / ".claude" / "projects" / mangled
        if session_dir.exists():
            return len(list(session_dir.glob("*.jsonl"))) > 0
        return False

    @staticmethod
    def _mangle_cwd(cwd: str) -> str:
        """Convert CWD to Claude CLI's mangled project dir name.

        Claude CLI resolves symlinks, so /tmp → /private/tmp on macOS.
        """
        resolved = str(Path(cwd).resolve())
        return resolved.replace("/", "-")

    def _cleanup_session_files(self, session: GatewaySession) -> None:
        """Clean up CWD directory and Claude CLI session files."""
        try:
            shutil.rmtree(session.cwd, ignore_errors=True)
        except Exception:
            pass
        mangled = self._mangle_cwd(session.cwd)
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
                self._persist_session(session)
                logger.info(
                    "Created session chat_id=%s cwd=%s first_done=%s",
                    chat_id, cwd, session.first_done,
                )
            return self._sessions[chat_id]

    def _kill_process(self, proc: subprocess.Popen) -> None:
        """Forcefully kill a subprocess and all its children."""
        # 1. SIGTERM the process group (graceful)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        # 2. Brief wait for graceful shutdown
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        # 3. SIGKILL the process group (force)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        # 4. Close pipes to unblock communicate() in case children hold them
        for pipe in (proc.stdout, proc.stderr, proc.stdin):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass
        # 5. Final wait (non-blocking to avoid deadlock)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("Process %d did not die after SIGKILL", proc.pid)

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
                    # Kill any lingering process
                    if session._proc and session._proc.poll() is None:
                        self._kill_process(session._proc)
                        session._proc = None
                    session.busy = False
                else:
                    return "[BUSY] Still processing the previous message, please wait."
            session.busy = True
            session.busy_since = time.time()

        try:
            args = [
                "claude", "-p",
                "--disable-slash-commands",  # Disable Claude Code skills - let /harness pass through
                "--output-format", "text",
                "--dangerously-skip-permissions",
            ]
            if session.first_done:
                args.append("--continue")
            args.append(message)

            # Clean env: remove CLAUDECODE to avoid "nested session" error
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=session.cwd,
                env=env,
                start_new_session=True,  # own process group for clean kill
            )

            with session.lock:
                session._proc = proc

            try:
                stdout, stderr = proc.communicate(timeout=CLAUDE_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._kill_process(proc)
                return f"[ERROR] Claude timed out after {CLAUDE_TIMEOUT}s"

            with session.lock:
                session.first_done = True
                session.last_active = time.time()
                session._proc = None
            self._persist_session(session)

            if proc.returncode != 0 and stderr:
                return f"[ERROR] {stderr.strip()}"

            return stdout.strip() or "(empty response)"

        except Exception as e:
            return f"[ERROR] {e}"
        finally:
            with session.lock:
                session.busy = False
                session.busy_since = 0.0
                if session._proc and session._proc.poll() is None:
                    self._kill_process(session._proc)
                session._proc = None
            self._persist_session(session)

    def stop_session(self, chat_id: str) -> bool:
        """Stop and clean up a session, killing any running process."""
        with self._global_lock:
            session = self._sessions.pop(chat_id, None)
            if session is None:
                return False

        # Kill any running Claude CLI process
        with session.lock:
            if session._proc and session._proc.poll() is None:
                logger.info("Killing running process for chat_id=%s", chat_id)
                self._kill_process(session._proc)
                session._proc = None
            session.busy = False

        self._cleanup_session_files(session)
        self._delete_persisted_session(chat_id)
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
            time.sleep(60)  # check every minute (was 5min, too slow for stuck recovery)
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
        # Clean up files and DB outside the lock
        for session in removed_sessions:
            self._cleanup_session_files(session)
            self._delete_persisted_session(session.chat_id)

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
                            # Kill any lingering process
                            if session._proc and session._proc.poll() is None:
                                self._kill_process(session._proc)
                                session._proc = None
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
