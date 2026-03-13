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

# Harness-Loop State Machine
class HarnessState:
    IDLE = "idle"
    COLLECTING = "collecting"
    PLANNING = "planning"
    PLAN_READY = "plan_ready"
    AWAITING_PARALLEL = "awaiting_parallel"
    RUNNING_FOREGROUND = "running_foreground"
    RUNNING_BACKGROUND = "running_background"
    COMPLETED = "completed"
    FAILED = "failed"

# Centurion State Machine
class CenturionState:
    IDLE = "idle"
    AWAITING_TASK = "awaiting_task"
    RUNNING_FOREGROUND = "running_foreground"
    RUNNING_BACKGROUND = "running_background"
    COMPLETED = "completed"
    FAILED = "failed"

# Background task sessions (chat_id starts with "bg-") inherit no timeout
def _is_background_session(chat_id: str) -> bool:
    """Check if this is a background task session."""
    return chat_id.startswith("bg-")


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
    bot_id: str = "default"
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

    @staticmethod
    def _session_key(bot_id: str, chat_id: str) -> str:
        """Composite key for session dict: '{bot_id}:{chat_id}'."""
        return f"{bot_id}:{chat_id}"

    def _load_persisted_sessions(self):
        """Load sessions from DB on startup. Reset busy flags (stale from crash)."""
        try:
            db = self._get_db()
            rows = db.execute("SELECT * FROM gateway_sessions").fetchall()
            for row in rows:
                try:
                    row = dict(row)
                    cwd = row["cwd"]
                    bot_id = row.get("bot_id", "default") or "default"

                    # Check if CWD still exists, if not, skip this session
                    if not os.path.exists(cwd):
                        logger.debug("Session CWD no longer exists, skipping: %s", cwd)
                        # Clean up the stale DB record
                        self._delete_persisted_session(row["chat_id"], bot_id)
                        continue

                    session = GatewaySession(
                        chat_id=row["chat_id"],
                        cwd=cwd,
                        bot_id=bot_id,
                        first_done=bool(row["first_done"]),
                        busy=False,  # always reset on startup
                        last_active=row["last_active"],
                    )
                    key = self._session_key(bot_id, row["chat_id"])
                    self._sessions[key] = session
                    logger.info("Restored session bot_id=%s chat_id=%s from DB", bot_id, row["chat_id"])
                except Exception as e:
                    logger.warning("Could not restore session %s: %s", row.get("chat_id"), e)
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
                       (chat_id, bot_id, cwd, first_done, busy, last_active)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (session.chat_id, session.bot_id, session.cwd,
                     int(session.first_done), int(session.busy), session.last_active),
                )
                db.commit()
        except Exception as e:
            logger.debug("Could not persist session: %s", e)

    def _delete_persisted_session(self, chat_id: str, bot_id: str = "default") -> None:
        """Remove session from DB. Thread-safe."""
        try:
            db = self._get_db()
            with self._get_write_lock():
                db.execute(
                    "DELETE FROM gateway_sessions WHERE chat_id = ? AND bot_id = ?",
                    (chat_id, bot_id),
                )
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
        try:
            # Check if cwd exists before resolving
            if not os.path.exists(cwd):
                logger.debug("CWD does not exist for mangling: %s", cwd)
                return "unknown"
            resolved = str(Path(cwd).resolve())
            return resolved.replace("/", "-")
        except Exception as e:
            logger.debug("Error mangling CWD %s: %s", cwd, e)
            return "unknown"

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

    def _get_or_create(self, chat_id: str, bot_id: str = "default") -> GatewaySession:
        key = self._session_key(bot_id, chat_id)
        with self._global_lock:
            if key not in self._sessions:
                cwd = os.path.join(SESSION_BASE_DIR, bot_id, chat_id)
                os.makedirs(cwd, exist_ok=True)
                session = GatewaySession(chat_id=chat_id, cwd=cwd, bot_id=bot_id)
                session.first_done = self._has_existing_claude_session(cwd)
                self._sessions[key] = session
                self._persist_session(session)
                logger.info(
                    "Created session bot_id=%s chat_id=%s cwd=%s first_done=%s",
                    bot_id, chat_id, cwd, session.first_done,
                )
            return self._sessions[key]

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
        # Check if CWD exists before accessing it
        if not os.path.exists(session.cwd):
            logger.debug("Session CWD does not exist, skipping context: %s", session.cwd)
            return message

        context_parts = []
        try:
            harness_dir = Path(session.cwd) / ".harness"
            if harness_dir.exists():
                for name, max_len in [("config.json", 500), ("progress.md", 500), ("tasks.json", 2000)]:
                    fp = harness_dir / name
                    if fp.exists():
                        try:
                            content = fp.read_text()[:max_len]
                            context_parts.append(f"[.harness/{name}]:\n{content}")
                        except Exception as e:
                            logger.debug("Could not read .harness file %s: %s", name, e)
        except Exception as e:
            logger.debug("Error accessing harness directory: %s", e)

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

        # No timeout for background sessions (chat_id starts with "bg-")
        is_background = _is_background_session(session.chat_id)
        is_long_message = self._is_no_timeout_message(message)

        timeout = None if (is_background or is_long_message) else CLAUDE_TIMEOUT

        if timeout is None:
            reason = "background session" if is_background else "long-running message"
            logger.info("chat_id=%s: no timeout (%s)", session.chat_id, reason)

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

    def send(self, chat_id: str, message: str, bot_id: str = "default") -> str:
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

        session = self._get_or_create(chat_id, bot_id=bot_id)

        # Check if this is a harness-loop task
        message_lower = message.lower()
        is_harness_loop = any(keyword in message_lower for keyword in ["harness-loop", "harness loop", "centurion"])
        harness_task_started = False

        if is_harness_loop:
            # Initialize or check harness state
            current_state = self._get_harness_state(chat_id, bot_id)

            if not current_state or current_state["state"] == HarnessState.IDLE:
                # New harness-loop task
                task_type = "harness-loop" if "harness" in message_lower else "centurion"
                self._set_harness_state(
                    chat_id,
                    HarnessState.COLLECTING,
                    bot_id,
                    task_type=task_type,
                )
                harness_task_started = True
                logger.info("New harness task for chat=%s: %s", chat_id, task_type)
            # Continue to normal processing for interaction

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
            # Update harness state if we were in collecting/planning phase
            if harness_task_started:
                current_state = self._get_harness_state(chat_id, bot_id)
                if current_state and current_state["state"] in [HarnessState.COLLECTING, HarnessState.PLANNING]:
                    # Transition to plan_ready
                    self._transition_harness_state(chat_id, current_state["state"], HarnessState.PLAN_READY, bot_id)
                    logger.info("Harness task completed plan phase for chat=%s", chat_id)

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

    def _init_pending_plans_table(self):
        """Initialize the pending_plans table."""
        db = self._get_db()
        db.execute(
            """CREATE TABLE IF NOT EXISTS pending_plans (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               chat_id TEXT NOT NULL,
               bot_id TEXT NOT NULL DEFAULT 'default',
               plan_id TEXT NOT NULL,
               plan TEXT NOT NULL,
               created_at REAL NOT NULL,
               UNIQUE(chat_id, bot_id, plan_id)
           )"""
        )
        db.commit()

    def _init_harness_states_table(self):
        """Initialize the harness_states table."""
        db = self._get_db()
        db.execute(
            """CREATE TABLE IF NOT EXISTS harness_states (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               chat_id TEXT NOT NULL,
               bot_id TEXT NOT NULL DEFAULT 'default',
               state TEXT NOT NULL,
               plan_id TEXT,
               task_type TEXT,
               duration_estimate INTEGER,
               parallel_enabled INTEGER DEFAULT 0,
               foreground INTEGER DEFAULT 0,
               created_at REAL NOT NULL,
               updated_at REAL NOT NULL,
               UNIQUE(chat_id, bot_id)
           )"""
        )
        db.commit()

    def _init_harness_plans_table(self):
        """Initialize the harness_plans table."""
        db = self._get_db()
        db.execute(
            """CREATE TABLE IF NOT EXISTS harness_plans (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               chat_id TEXT NOT NULL,
               bot_id TEXT NOT NULL DEFAULT 'default',
               plan_id TEXT NOT NULL UNIQUE,
               plan TEXT NOT NULL,
               state TEXT NOT NULL,
               task_type TEXT NOT NULL,
               created_at REAL NOT NULL,
               confirmed_at REAL,
               started_at REAL,
               completed_at REAL
           )"""
        )
        db.commit()

    def _store_pending_plan(self, chat_id: str, plan_id: str, plan: str, bot_id: str = "default"):
        """Store a pending plan."""
        self._init_pending_plans_table()
        db = self._get_db()
        try:
            with self._get_write_lock():
                db.execute(
                    """INSERT INTO pending_plans
                       (chat_id, bot_id, plan_id, plan, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (chat_id, bot_id, plan_id, plan, time.time()),
                )
                db.commit()
                logger.info("Stored pending plan %s for chat=%s", plan_id, chat_id)
        except Exception as e:
            logger.error("Failed to store pending plan: %s", e)

    def _get_pending_plan(self, chat_id: str, plan_id: str, bot_id: str = "default"):
        """Get a specific pending plan."""
        self._init_pending_plans_table()
        db = self._get_db()
        try:
            row = db.execute(
                """SELECT plan_id, plan FROM pending_plans
                   WHERE chat_id = ? AND bot_id = ? AND plan_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (chat_id, bot_id, plan_id),
            ).fetchone()
            if row:
                return {"plan_id": row[0], "plan": row[1]}
        except Exception as e:
            logger.error("Failed to get pending plan: %s", e)
        return None

    def _remove_pending_plan(self, chat_id: str, plan_id: str, bot_id: str = "default"):
        """Remove a pending plan."""
        self._init_pending_plans_table()
        db = self._get_db()
        try:
            with self._get_write_lock():
                db.execute(
                    """DELETE FROM pending_plans
                       WHERE chat_id = ? AND bot_id = ? AND plan_id = ?""",
                    (chat_id, bot_id, plan_id),
                )
                db.commit()
                logger.info("Removed pending plan %s for chat=%s", plan_id, chat_id)
        except Exception as e:
            logger.error("Failed to remove pending plan: %s", e)

    # ========== Harness State Machine Methods ==========

    def _get_harness_state(self, chat_id: str, bot_id: str = "default") -> dict:
        """Get current harness state for a chat."""
        self._init_harness_states_table()
        db = self._get_db()
        try:
            row = db.execute(
                """SELECT state, plan_id, task_type, duration_estimate,
                          parallel_enabled, foreground
                   FROM harness_states
                   WHERE chat_id = ? AND bot_id = ?""",
                (chat_id, bot_id),
            ).fetchone()
            if row:
                return {
                    "state": row[0],
                    "plan_id": row[1],
                    "task_type": row[2],
                    "duration_estimate": row[3],
                    "parallel_enabled": row[4],
                    "foreground": row[5],
                }
        except Exception as e:
            logger.error("Failed to get harness state: %s", e)
        return None

    def _set_harness_state(
        self,
        chat_id: str,
        state: str,
        bot_id: str = "default",
        plan_id: str = "",
        task_type: str = "",
        duration_estimate: int = 0,
        parallel_enabled: int = 0,
        foreground: int = 0,
    ):
        """Set harness state for a chat."""
        self._init_harness_states_table()
        db = self._get_db()
        try:
            now = time.time()
            with self._get_write_lock():
                db.execute(
                    """INSERT INTO harness_states
                       (chat_id, bot_id, state, plan_id, task_type,
                        duration_estimate, parallel_enabled, foreground, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(chat_id, bot_id) DO UPDATE SET
                       state = excluded.state,
                       plan_id = excluded.plan_id,
                       task_type = excluded.task_type,
                       duration_estimate = excluded.duration_estimate,
                       parallel_enabled = excluded.parallel_enabled,
                       foreground = excluded.foreground,
                       updated_at = excluded.updated_at""",
                    (
                        chat_id, bot_id, state, plan_id, task_type,
                        duration_estimate, parallel_enabled, foreground, now, now,
                    ),
                )
                db.commit()
                logger.info("Set harness state for chat=%s: %s", chat_id, state)
        except Exception as e:
            logger.error("Failed to set harness state: %s", e)

    def _transition_harness_state(
        self,
        chat_id: str,
        from_state: str,
        to_state: str,
        bot_id: str = "default",
    ) -> bool:
        """Transition harness state with validation."""
        current = self._get_harness_state(chat_id, bot_id)
        if not current:
            logger.warning("No current state for chat=%s, setting to %s", chat_id, to_state)
            self._set_harness_state(chat_id, to_state, bot_id)
            return True

        if current["state"] != from_state:
            logger.warning(
                "State transition failed for chat=%s: expected %s, got %s",
                chat_id, from_state, current["state"],
            )
            return False

        self._set_harness_state(chat_id, to_state, bot_id)
        logger.info("State transition for chat=%s: %s -> %s", chat_id, from_state, to_state)
        return True

    def _store_harness_plan(
        self,
        chat_id: str,
        plan_id: str,
        plan: str,
        task_type: str,
        bot_id: str = "default",
    ):
        """Store a harness plan."""
        self._init_harness_plans_table()
        db = self._get_db()
        try:
            with self._get_write_lock():
                db.execute(
                    """INSERT INTO harness_plans
                       (chat_id, bot_id, plan_id, plan, state, task_type, created_at)
                       VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                    (chat_id, bot_id, plan_id, plan, task_type, time.time()),
                )
                db.commit()
                logger.info("Stored harness plan %s for chat=%s", plan_id, chat_id)
        except Exception as e:
            logger.error("Failed to store harness plan: %s", e)

    def _get_harness_plan(self, chat_id: str, plan_id: str, bot_id: str = "default") -> dict:
        """Get a harness plan."""
        self._init_harness_plans_table()
        db = self._get_db()
        try:
            row = db.execute(
                """SELECT plan, task_type FROM harness_plans
                   WHERE chat_id = ? AND bot_id = ? AND plan_id = ?""",
                (chat_id, bot_id, plan_id),
            ).fetchone()
            if row:
                return {"plan": row[0], "task_type": row[1]}
        except Exception as e:
            logger.error("Failed to get harness plan: %s", e)
        return None

    def _confirm_harness_plan(self, chat_id: str, plan_id: str, bot_id: str = "default"):
        """Mark a harness plan as confirmed."""
        self._init_harness_plans_table()
        db = self._get_db()
        try:
            with self._get_write_lock():
                db.execute(
                    """UPDATE harness_plans SET state = 'confirmed', confirmed_at = ?
                       WHERE chat_id = ? AND bot_id = ? AND plan_id = ?""",
                    (time.time(), chat_id, bot_id, plan_id),
                )
                db.commit()
                logger.info("Confirmed harness plan %s for chat=%s", plan_id, chat_id)
        except Exception as e:
            logger.error("Failed to confirm harness plan: %s", e)

    def _start_harness_plan(self, chat_id: str, plan_id: str, bot_id: str = "default"):
        """Mark a harness plan as running."""
        self._init_harness_plans_table()
        db = self._get_db()
        try:
            with self._get_write_lock():
                db.execute(
                    """UPDATE harness_plans SET state = 'running', started_at = ?
                       WHERE chat_id = ? AND bot_id = ? AND plan_id = ?""",
                    (time.time(), chat_id, bot_id, plan_id),
                )
                db.commit()
                logger.info("Started harness plan %s for chat=%s", plan_id, chat_id)
        except Exception as e:
            logger.error("Failed to start harness plan: %s", e)

    def _complete_harness_plan(self, chat_id: str, plan_id: str, bot_id: str = "default"):
        """Mark a harness plan as completed."""
        self._init_harness_plans_table()
        db = self._get_db()
        try:
            with self._get_write_lock():
                db.execute(
                    """UPDATE harness_plans SET state = 'completed', completed_at = ?
                       WHERE chat_id = ? AND bot_id = ? AND plan_id = ?""",
                    (time.time(), chat_id, bot_id, plan_id),
                )
                db.commit()
                logger.info("Completed harness plan %s for chat=%s", plan_id, chat_id)
        except Exception as e:
            logger.error("Failed to complete harness plan: %s", e)

    def _estimate_duration(self, plan: str) -> int:
        """Estimate task duration in seconds based on plan content."""
        plan_lower = plan.lower()

        # Simple heuristics based on keywords and plan length
        duration = 0

        # Base duration based on plan length
        word_count = len(plan.split())
        duration += word_count * 2  # 2 seconds per word base

        # Adjust based on task type
        if "安装" in plan_lower or "upgrade" in plan_lower:
            duration += 120  # Install/upgrade tasks: +2 min
        if "测试" in plan_lower or "test" in plan_lower:
            duration += 180  # Testing tasks: +3 min
        if "部署" in plan_lower or "deploy" in plan_lower:
            duration += 300  # Deploy tasks: +5 min
        if "迁移" in plan_lower or "migrate" in plan_lower:
            duration += 600  # Migration tasks: +10 min
        if "重构" in plan_lower or "refactor" in plan_lower:
            duration += 900  # Refactoring: +15 min

        # Clamp to reasonable range
        duration = max(60, min(duration, 3600))  # 1 min to 1 hour

        logger.info("Estimated duration for plan: %ds", duration)
        return duration

    def _is_short_task(self, plan: str) -> bool:
        """Check if task is short (< 5 minutes)."""
        duration = self._estimate_duration(plan)
        return duration < 300  # 5 minutes

    def send_background(self, chat_id: str, message: str, bot_token: str, bot_id: str = "default", plan_id: str = "") -> dict:
        """Start a background Claude CLI task for the given chat.

        Uses a separate session (bg-{chat_id}) so it doesn't interfere with
        the main interactive session. Returns immediately. Only one background
        task per chat_id is allowed at a time. Sends the result to Telegram
        via bot API on completion.

        If plan_id is provided, retrieves the stored plan instead of using message.
        """
        bg_key = self._session_key(bot_id, chat_id)

        # Check if plan_id is provided (confirm command)
        if plan_id:
            pending_plan = self._get_pending_plan(chat_id, plan_id, bot_id)
            if pending_plan:
                # Use the stored plan
                message = pending_plan["plan"]
                logger.info("Using pending plan %s for chat=%s", plan_id, chat_id)
                # Remove the plan after confirming
                self._remove_pending_plan(chat_id, plan_id, bot_id)
            else:
                logger.warning("Pending plan %s not found for chat=%s", plan_id, chat_id)

        # Check if a background task is already running for this chat_id
        with self._global_lock:
            existing = self._bg_tasks.get(bg_key)
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
        main_session = self._get_or_create(chat_id, bot_id=bot_id)
        bg_session = self._get_or_create(bg_session_key, bot_id=bot_id)
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
        self._bg_tasks[bg_key] = task_info

        def _run():
            try:
                result = self.send(bg_session_key, message, bot_id=bot_id)
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

    def get_background_status(self, chat_id: str, bot_id: str = "default") -> dict:
        """Return the current background task info for this chat_id."""
        bg_key = self._session_key(bot_id, chat_id)
        task = self._bg_tasks.get(bg_key)
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

    def stop_session(self, chat_id: str, bot_id: str = "default") -> bool:
        """Stop and clean up a session, killing any running process."""
        key = self._session_key(bot_id, chat_id)
        with self._global_lock:
            session = self._sessions.pop(key, None)
            if session is None:
                return False

        # Kill any running Claude CLI process
        with session.lock:
            if session._proc and session._proc.poll() is None:
                logger.info("Killing running process for bot_id=%s chat_id=%s", bot_id, chat_id)
                self._kill_process(session._proc)
                session._proc = None
            session.busy = False
        session._ready.set()  # wake up any queued messages

        self._cleanup_session_files(session)
        self._delete_persisted_session(chat_id, bot_id=bot_id)
        logger.info("Stopped session bot_id=%s chat_id=%s", bot_id, chat_id)
        return True

    def list_sessions(self, bot_id: str | None = None) -> list[dict]:
        with self._global_lock:
            now = time.time()
            sessions = self._sessions.values()
            if bot_id is not None:
                sessions = [s for s in sessions if s.bot_id == bot_id]
            return [
                {
                    "chat_id": s.chat_id,
                    "bot_id": s.bot_id,
                    "busy": s.busy,
                    "first_done": s.first_done,
                    "last_active": s.last_active,
                    "idle_seconds": int(now - s.last_active),
                    "busy_seconds": int(now - s.busy_since) if s.busy else 0,
                }
                for s in sessions
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
            for key, session in self._sessions.items():
                if not session.busy and (now - session.last_active) > SESSION_IDLE_TIMEOUT:
                    to_remove.append(key)
            for key in to_remove:
                removed_sessions.append(self._sessions.pop(key))
                logger.info("Cleaned up idle session bot_id=%s chat_id=%s", removed_sessions[-1].bot_id, removed_sessions[-1].chat_id)
        # Clean up files and DB outside the lock
        for session in removed_sessions:
            self._cleanup_session_files(session)
            self._delete_persisted_session(session.chat_id, bot_id=session.bot_id)

    def _recover_stuck_sessions(self):
        """Auto-recover sessions stuck in busy state beyond the timeout."""
        now = time.time()
        with self._global_lock:
            for key, session in self._sessions.items():
                with session.lock:
                    if session.busy and session.busy_since > 0:
                        stuck_duration = now - session.busy_since
                        # ✅ Check if this is a background task before killing
                        is_bg = _is_background_session(session.chat_id)

                        # ✅ Only kill foreground tasks that are stuck
                        if not is_bg and stuck_duration > BUSY_STUCK_TIMEOUT:
                                logger.warning(
                                    "Auto-recovering stuck session bot_id=%s chat_id=%s (busy for %ds)",
                                    session.bot_id, session.chat_id, int(stuck_duration),
                                )
                                # Kill any lingering process
                                if session._proc and session._proc.poll() is None:
                                    self._kill_process(session._proc)
                                    session._proc = None
                                session.busy = False
                                session.busy_since = 0.0
                            else:
                                # ⚠️ Background task: Skip killing, let it run
                                logger.debug(
                                    "Skipping background session bot_id=%s chat_id=%s (stuck for %ds)",
                                    session.bot_id, session.chat_id, int(stuck_duration),
                                )
                        else:
                            # ✅ Foreground task or not stuck: Kill and recover
                            logger.warning(
                                "Auto-recovering stuck session bot_id=%s chat_id=%s (busy for %ds)",
                                session.bot_id, session.chat_id, int(stuck_duration),
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
