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

import httpx

logger = logging.getLogger(__name__)

SESSION_BASE_DIR = os.getenv("GATEWAY_SESSION_DIR", "/tmp/claude-gateway-sessions")
SESSION_IDLE_TIMEOUT = int(os.getenv("GATEWAY_SESSION_TIMEOUT", "7200"))  # 2 hours
CLAUDE_TIMEOUT = int(os.getenv("GATEWAY_CLAUDE_TIMEOUT", "900"))  # 15 minutes
BUSY_STUCK_TIMEOUT = int(os.getenv("GATEWAY_BUSY_STUCK_TIMEOUT", "300"))  # 5 minutes (was 30m, too long)
QUEUE_WAIT_TIMEOUT = int(os.getenv("GATEWAY_QUEUE_WAIT_TIMEOUT", "1800"))  # max 30min wait in queue

# Memory guardrails
MEMORY_MIN_FREE_MB = int(os.getenv("GATEWAY_MIN_FREE_MB", "512"))  # 512MB minimum free before spawning
MEMORY_CHECK_INTERVAL = 10  # seconds between memory checks when waiting
MEMORY_MAX_WAIT = 300  # max 5 minutes waiting for memory to free up
MAX_OOM_RETRIES = 3  # max retries when Claude is killed by OOM (exit -15)
OOM_RETRY_BACKOFF = 15  # base seconds between OOM retries (multiplied by attempt)
MAX_CLAUDE_PROCESSES = int(os.getenv("GATEWAY_MAX_CLAUDE_PROCESSES", "2"))  # Max concurrent Claude processes

# Messages matching these patterns get no timeout (they can run for hours)
NO_TIMEOUT_PATTERNS = ["/harness", "harness loop", "harness-loop"]


def _get_available_memory_mb() -> int:
    """Get available memory in MB using macOS vm_stat.

    Returns free + inactive pages converted to MB.
    On error, returns a large number to avoid blocking.
    """
    try:
        result = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5
        )
        page_size = 16384  # macOS default (16KB pages on Apple Silicon)
        free = 0
        inactive = 0
        purgeable = 0
        for line in result.stdout.splitlines():
            if "Pages free" in line:
                free = int(line.split(":")[1].strip().rstrip("."))
            elif "Pages inactive" in line:
                inactive = int(line.split(":")[1].strip().rstrip("."))
            elif "Pages purgeable" in line:
                purgeable = int(line.split(":")[1].strip().rstrip("."))
        available_bytes = (free + inactive + purgeable) * page_size
        return available_bytes // (1024 * 1024)
    except Exception:
        return 9999  # assume plenty on error


def _make_set_event() -> threading.Event:
    """Create an Event that starts in the 'set' (ready) state."""
    e = threading.Event()
    e.set()
    return e


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
    _ready: threading.Event = field(default_factory=lambda: _make_set_event())


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, GatewaySession] = {}
        self._bg_tasks: dict[str, dict] = {}
        self._global_lock = threading.Lock()
        self._running = False
        self._cleanup_thread: threading.Thread | None = None
        self._claude_process_count = 0  # Track concurrent Claude processes
        self._process_count_lock = threading.Lock()  # Lock for process counter
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

    @staticmethod
    def _is_no_timeout_message(message: str) -> bool:
        """Check if message should run without timeout (e.g. harness loops)."""
        msg_lower = message.lower()
        return any(p in msg_lower for p in NO_TIMEOUT_PATTERNS)

    def _wait_for_memory(self, chat_id: str) -> bool:
        """Wait until enough memory is available before spawning Claude CLI.

        Returns True if memory is sufficient, False if timed out waiting.
        """
        waited = 0
        while waited < MEMORY_MAX_WAIT:
            available = _get_available_memory_mb()
            if available >= MEMORY_MIN_FREE_MB:
                if waited > 0:
                    logger.info(
                        "chat_id=%s: Memory OK: %dMB available (waited %ds)",
                        chat_id, available, waited,
                    )
                return True
            logger.warning(
                "chat_id=%s: Low memory: %dMB available (need %dMB). Waiting %ds...",
                chat_id, available, MEMORY_MIN_FREE_MB, MEMORY_CHECK_INTERVAL,
            )
            time.sleep(MEMORY_CHECK_INTERVAL)
            waited += MEMORY_CHECK_INTERVAL
        logger.error(
            "chat_id=%s: Timed out waiting for memory after %ds (%dMB available)",
            chat_id, MEMORY_MAX_WAIT, _get_available_memory_mb(),
        )
        return False

    def _inject_context(self, session: GatewaySession, message: str) -> str:
        """Inject filesystem context (e.g. .harness state) into the message.

        Since Claude CLI in -p mode has no tool access, we prepend key state
        files so Claude is aware of active projects in the session's CWD.
        """
        context_parts = []
        harness_dir = Path(session.cwd) / ".harness"
        if harness_dir.exists():
            for name, max_len in [("config.json", 500), ("progress.md", 500), ("tasks.json", 2000)]:
                fp = harness_dir / name
                if fp.exists():
                    content = fp.read_text()[:max_len]
                    context_parts.append(f"[.harness/{name}]:\n{content}")
        if not context_parts:
            return message
        prefix = "Active project state in working directory:\n" + "\n\n".join(context_parts)
        return f"{prefix}\n\n---\nUser message: {message}"

    def _run_claude_cli(self, session: GatewaySession, message: str, env: dict) -> tuple[int, str, str]:
        """Run Claude CLI once and return (returncode, stdout, stderr).

        Handles process lifecycle, timeout, and session state updates.
        """
        args = [
            "claude", "-p",
            "--disable-slash-commands",
            "--output-format", "text",
            "--dangerously-skip-permissions",
        ]
        if session.first_done:
            args.append("--continue")
        args.append(self._inject_context(session, message))

        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=session.cwd,
            env=env,
            start_new_session=True,
        )

        with session.lock:
            session._proc = proc

        timeout = None if self._is_no_timeout_message(message) else CLAUDE_TIMEOUT
        if timeout is None:
            logger.info("chat_id=%s: no timeout (long-running message detected)", session.chat_id)

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_process(proc)
            with session.lock:
                session._proc = None
            return (-999, "", f"Claude timed out after {CLAUDE_TIMEOUT}s")

        with session.lock:
            session.first_done = True
            session.last_active = time.time()
            session._proc = None
        self._persist_session(session)

        if stderr and stderr.strip():
            logger.info(
                "chat_id=%s stderr (rc=%d): %s",
                session.chat_id, proc.returncode, stderr.strip()[:500],
            )

        return (proc.returncode, stdout.strip() if stdout else "", stderr.strip() if stderr else "")

    def send(self, chat_id: str, message: str) -> str:
        """Send a message to Claude CLI for the given chat. Blocking call.

        If the session is busy, queues and waits (up to QUEUE_WAIT_TIMEOUT)
        instead of immediately rejecting.

        Memory guardrails:
        - Waits for sufficient free memory before spawning Claude CLI
        - On exit code -15 (SIGTERM / OOM kill), waits and retries automatically
        - Limits concurrent Claude processes to MAX_CLAUDE_PROCESSES
        """
        # Check concurrent process limit
        waited = 0
        while waited < QUEUE_WAIT_TIMEOUT:
            with self._process_count_lock:
                if self._claude_process_count < MAX_CLAUDE_PROCESSES:
                    self._claude_process_count += 1
                    break
            logger.info(
                "chat_id=%s: Waiting for Claude process slot (%d/%d active)",
                chat_id, self._claude_process_count, MAX_CLAUDE_PROCESSES
            )
            time.sleep(5)
            waited += 5
        else:
            return "[BUSY] Too many concurrent Claude processes. Please try again later."

        session = self._get_or_create(chat_id)

        # Wait for the session to become free (queue instead of reject)
        if not session._ready.wait(timeout=QUEUE_WAIT_TIMEOUT):
            return "[BUSY] Timed out waiting in queue. The previous message is still processing."

        with session.lock:
            if session.busy:
                # Auto-recover from stuck busy state
                stuck_duration = time.time() - session.busy_since
                if stuck_duration > BUSY_STUCK_TIMEOUT:
                    logger.warning(
                        "Session chat_id=%s stuck busy for %ds, force-resetting",
                        chat_id, int(stuck_duration),
                    )
                    if session._proc and session._proc.poll() is None:
                        self._kill_process(session._proc)
                        session._proc = None
                    session.busy = False
                else:
                    return "[BUSY] Still processing the previous message, please wait."
            session.busy = True
            session.busy_since = time.time()
            session._ready.clear()

        try:
            # Clean env: remove CLAUDECODE to avoid "nested session" error
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

            # Retry loop for OOM kills (exit code -15)
            for attempt in range(MAX_OOM_RETRIES + 1):
                # Memory guardrail: wait for sufficient free memory
                if not self._wait_for_memory(chat_id):
                    logger.warning(
                        "chat_id=%s: Proceeding despite low memory (attempt %d)",
                        chat_id, attempt + 1,
                    )

                returncode, stdout, stderr = self._run_claude_cli(session, message, env)

                # Timeout sentinel
                if returncode == -999:
                    return f"[ERROR] {stderr}"

                # Success
                if returncode == 0:
                    return stdout if stdout else ""

                # Exit -15 = SIGTERM (likely OOM kill by macOS)
                if returncode == -15:
                    if attempt < MAX_OOM_RETRIES:
                        backoff = OOM_RETRY_BACKOFF * (attempt + 1)
                        available = _get_available_memory_mb()
                        logger.warning(
                            "chat_id=%s: Claude killed (SIGTERM/-15, likely OOM). "
                            "Memory: %dMB. Retry %d/%d in %ds...",
                            chat_id, available, attempt + 1, MAX_OOM_RETRIES, backoff,
                        )
                        # Wait for memory to free up before retrying
                        time.sleep(backoff)
                        continue
                    else:
                        logger.error(
                            "chat_id=%s: Claude killed (SIGTERM/-15) after %d retries. "
                            "Memory: %dMB. Giving up.",
                            chat_id, MAX_OOM_RETRIES, _get_available_memory_mb(),
                        )
                        return (
                            f"[ERROR] Claude was killed by the system {MAX_OOM_RETRIES + 1} times "
                            f"(likely out of memory). Available: {_get_available_memory_mb()}MB. "
                            f"Try again when other processes have finished."
                        )

                # Other non-zero exit codes — not OOM, don't retry
                if stderr:
                    return f"[ERROR] Claude exited with code {returncode}\nstderr: {stderr[:500]}"
                else:
                    return f"[ERROR] Claude exited with code {returncode} (no stderr output)"

            # Should not reach here, but just in case
            return "[ERROR] Unexpected state in send() retry loop"

        except Exception as e:
            return f"[ERROR] {e}"
        finally:
            with session.lock:
                session.busy = False
                session.busy_since = 0.0
                if session._proc and session._proc.poll() is None:
                    self._kill_process(session._proc)
                session._proc = None
            session._ready.set()
            self._persist_session(session)
            # Decrease process count
            with self._process_count_lock:
                self._claude_process_count = max(0, self._claude_process_count - 1)

    def send_background(self, chat_id: str, message: str, bot_token: str) -> dict:
        """Start a background Claude CLI task for the given chat.

        Uses a separate session (bg-{chat_id}) so it doesn't interfere with
        the main interactive session. Returns immediately. Only one background
        task per chat_id is allowed at a time. Sends the result to Telegram
        via bot API on completion.
        """
        # Check if a background task is already running for this chat_id
        with self._global_lock:
            existing = self._bg_tasks.get(chat_id)
            if existing and existing["status"] == "running":
                thread = existing.get("thread")
                if thread and thread.is_alive():
                    elapsed = time.time() - existing.get("started_at", 0)
                    if elapsed > BUSY_STUCK_TIMEOUT:
                        logger.warning(
                            "Force-clearing stale bg task for chat_id=%s (running %ds)",
                            chat_id, int(elapsed),
                        )
                        existing["status"] = "failed"
                        existing["result"] = f"Force-cleared: exceeded {BUSY_STUCK_TIMEOUT}s timeout"
                    else:
                        return {"status": "rejected", "reason": "already running", "elapsed": int(elapsed)}
                else:
                    logger.warning("Recovering dead bg task for chat_id=%s", chat_id)
                    existing["status"] = "failed"
                    existing["result"] = "Thread died unexpectedly"

        bg_session_key = f"bg-{chat_id}"
        # Share CWD with main session so background work is visible to main chat
        main_session = self._get_or_create(chat_id)
        bg_session = self._get_or_create(bg_session_key)
        if bg_session.cwd != main_session.cwd:
            bg_session.cwd = main_session.cwd
            self._persist_session(bg_session)
            logger.info("Synced bg session CWD to main: %s", main_session.cwd)
        started_at = time.time()

        task_info = {
            "thread": None,
            "message": message[:200],
            "started_at": started_at,
            "status": "running",
            "result": None,
        }
        self._bg_tasks[chat_id] = task_info

        def _run():
            try:
                result = self.send(bg_session_key, message)
                task_info["status"] = "completed"
                task_info["result"] = result[:500] if result else ""
            except Exception as e:
                result = f"[ERROR] Background task failed: {e}"
                task_info["status"] = "failed"
                task_info["result"] = result[:500]

            # Send result to Telegram, splitting into 4096-char chunks
            self._send_telegram_result(chat_id, result, bot_token)

        thread = threading.Thread(target=_run, daemon=True)
        task_info["thread"] = thread
        thread.start()

        logger.info(
            "Started background task for chat_id=%s (bg session: %s)",
            chat_id, bg_session_key,
        )
        return {"status": "started"}

    def _send_telegram_result(self, chat_id: str, text: str, bot_token: str) -> None:
        """Send a result message to Telegram, splitting into 4096-char chunks."""
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        max_chunk = 4096

        if not text:
            text = "(empty response)"

        chunks = [text[i:i + max_chunk] for i in range(0, len(text), max_chunk)]

        for chunk in chunks:
            try:
                resp = httpx.post(
                    url,
                    json={"chat_id": chat_id, "text": chunk},
                    timeout=30,
                )
                if resp.status_code != 200:
                    logger.warning(
                        "Telegram sendMessage failed for chat_id=%s: %d %s",
                        chat_id, resp.status_code, resp.text[:200],
                    )
            except Exception as e:
                logger.error(
                    "Failed to send Telegram message for chat_id=%s: %s",
                    chat_id, e,
                )

    def get_background_status(self, chat_id: str) -> dict:
        """Return the current background task info for this chat_id."""
        task = self._bg_tasks.get(chat_id)
        if task is None:
            return {"status": "idle"}

        now = time.time()
        elapsed = int(now - task["started_at"])

        if task["status"] == "running":
            return {
                "status": "running",
                "message": task["message"],
                "elapsed_seconds": elapsed,
                "started_at": task["started_at"],
            }

        # completed or failed
        return {
            "status": task["status"],
            "result": task["result"],
            "elapsed_seconds": elapsed,
        }

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
        session._ready.set()  # wake up any queued messages

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
                            session._ready.set()  # wake up any queued messages


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
