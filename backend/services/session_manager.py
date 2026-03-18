"""Multi-session manager for Claude CLI gateway.

Each chat_id gets an isolated Claude CLI session via unique CWD.
Claude CLI uses CWD to determine the "project", so --continue
only resumes the session for that specific CWD.
"""

import json
import logging
import os
import re
import signal
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx

# Harness batch-chaining constants
HARNESS_BATCH_DONE_RE = re.compile(r'\[HARNESS_BATCH_DONE:([^:]+):(\d+)/(\d+)\]')
HARNESS_BLOCKED_RE = re.compile(r'\[HARNESS_BLOCKED:([^:]+):(.+)\]')
HARNESS_COMPLETE_MARKER = '[HARNESS_COMPLETE]'
MAX_HARNESS_CHAIN_DEPTH = 100
HARNESS_CHAIN_DELAY = 5

logger = logging.getLogger(__name__)

SESSION_BASE_DIR = os.getenv("GATEWAY_SESSION_DIR", os.path.expanduser("~/claude-gateway-sessions"))
SESSION_IDLE_TIMEOUT = int(os.getenv("GATEWAY_SESSION_TIMEOUT", "7200"))  # 2 hours
HARNESS_SESSION_TIMEOUT = int(os.getenv("HARNESS_SESSION_TIMEOUT", "259200"))  # 3 days for harness projects
HARNESS_ARCHIVE_DIR = os.path.expanduser("~/.claude-gateway-archives")
CLAUDE_TIMEOUT = int(os.getenv("GATEWAY_CLAUDE_TIMEOUT", "600"))  # 10 minutes for normal chat
BUSY_STUCK_TIMEOUT = int(os.getenv("GATEWAY_BUSY_STUCK_TIMEOUT", "660"))  # 11 minutes safety net (must exceed CLAUDE_TIMEOUT)
QUEUE_WAIT_TIMEOUT = int(os.getenv("GATEWAY_QUEUE_WAIT_TIMEOUT", "120"))  # max 2 min wait in queue

# Memory guardrails
MEMORY_MIN_FREE_MB = int(os.getenv("GATEWAY_MIN_FREE_MB", "512"))  # 512MB minimum free before spawning
MEMORY_CHECK_INTERVAL = 10  # seconds between memory checks when waiting
MEMORY_MAX_WAIT = 300  # max 5 minutes waiting for memory to free up
MAX_OOM_RETRIES = 3  # max retries when Claude is killed by OOM (exit -15)
OOM_RETRY_BACKOFF = 15  # base seconds between OOM retries (multiplied by attempt)
MAX_CLAUDE_PROCESSES = int(os.getenv("GATEWAY_MAX_CLAUDE_PROCESSES", "2"))  # Max concurrent Claude processes

# Messages matching these patterns get no timeout (they can run for hours)
NO_TIMEOUT_PATTERNS = ["/harness", "harness loop", "harness-loop", "后台模式", "后台运行", "后台执行"]

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

                    # Skip sessions with stale CWDs (e.g., from old /tmp base dir)
                    resolved_base = str(Path(SESSION_BASE_DIR).resolve())
                    resolved_cwd = str(Path(cwd).resolve()) if os.path.exists(cwd) else cwd
                    if not os.path.exists(cwd) or not resolved_cwd.startswith(resolved_base):
                        logger.debug("Session CWD stale or mismatched, skipping: %s (base: %s)", cwd, SESSION_BASE_DIR)
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

    def _archive_harness(self, session: GatewaySession) -> str | None:
        """Archive .harness/ directory before cleanup. Returns UUID or None."""
        harness_dir = Path(session.cwd) / ".harness"
        if not harness_dir.exists():
            return None

        archive_id = str(uuid.uuid4())
        archive_dir = Path(HARNESS_ARCHIVE_DIR)
        archive_dest = archive_dir / archive_id / ".harness"

        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(harness_dir), str(archive_dest))
        except Exception as e:
            logger.error("Failed to archive .harness/ for chat_id=%s: %s", session.chat_id, e)
            return None

        # Build index entry
        project_name = "unknown"
        status = "unknown"
        tasks_done = 0
        tasks_total = 0
        tasks_blocked = 0
        tasks_json = harness_dir / "tasks.json"
        if tasks_json.exists():
            try:
                with open(tasks_json) as f:
                    data = json.load(f)
                metadata = data.get("metadata", {})
                project_name = metadata.get("project_name", "unknown")
                tasks = data.get("tasks", [])
                tasks_total = len(tasks)
                for t in tasks:
                    s = t.get("status", "pending")
                    if s == "done":
                        tasks_done += 1
                    elif s == "blocked":
                        tasks_blocked += 1
                status = "complete" if tasks_done == tasks_total and tasks_total > 0 else "incomplete"
            except Exception:
                pass

        entry = {
            "uuid": archive_id,
            "project_name": project_name,
            "chat_id": session.chat_id,
            "bot_id": session.bot_id,
            "original_cwd": session.cwd,
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "tasks_done": tasks_done,
            "tasks_total": tasks_total,
            "tasks_blocked": tasks_blocked,
        }

        # Append to index.json
        index_path = archive_dir / "index.json"
        try:
            if index_path.exists():
                with open(index_path) as f:
                    index = json.load(f)
            else:
                index = []
            index.append(entry)
            with open(index_path, "w") as f:
                json.dump(index, f, indent=2)
            logger.info(
                "Archived harness project '%s' for chat_id=%s → %s (status=%s, %d/%d tasks)",
                project_name, session.chat_id, archive_id, status, tasks_done, tasks_total,
            )
        except Exception as e:
            logger.error("Failed to write archive index: %s", e)

        return archive_id

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

    def _prune_session_history(self, session: GatewaySession, keep: int = 2) -> None:
        """Delete old Claude CLI session JSONL files, keeping only the most recent ones.

        Each `claude -p` call creates a new .jsonl file. With --continue these
        accumulate forever, bloating the project directory and slowing down
        session resume. This keeps only the latest `keep` files.
        """
        mangled = self._mangle_cwd(session.cwd)
        if mangled == "unknown":
            return
        session_dir = Path.home() / ".claude" / "projects" / mangled
        if not session_dir.exists():
            return
        try:
            jsonl_files = sorted(
                session_dir.glob("*.jsonl"),
                key=lambda f: f.stat().st_mtime,
            )
            if len(jsonl_files) <= keep:
                return
            for old_file in jsonl_files[:-keep]:
                old_file.unlink(missing_ok=True)
                logger.info("Pruned old session file: %s", old_file.name)
        except Exception as e:
            logger.debug("Failed to prune session history: %s", e)

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
            else:
                # Ensure CWD directory exists (may have been deleted by reset/cleanup)
                session = self._sessions[key]
                if not os.path.exists(session.cwd):
                    os.makedirs(session.cwd, exist_ok=True)
                    logger.info("Recreated missing CWD for session %s: %s", chat_id, session.cwd)
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

        # Global memory — shared across all sessions/channels
        global_memory_path = Path(os.path.expanduser("~/.mini-claude-bot/global-memory.md"))
        if global_memory_path.exists():
            try:
                content = global_memory_path.read_text()[:2000]
                context_parts.append(f"[Global Memory]:\n{content}")
            except Exception as e:
                logger.debug("Could not read global memory: %s", e)

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

        # Check Centurion status for harness-loop messages
        msg_lower = message.lower()
        if any(kw in msg_lower for kw in ["harness", "后台模式", "后台运行", "后台执行"]):
            try:
                import httpx
                centurion_port = os.getenv("CENTURION_PORT", "8100")
                resp = httpx.get(
                    f"http://localhost:{centurion_port}/api/centurion/hardware",
                    timeout=3,
                )
                if resp.status_code == 200:
                    hw = resp.json()
                    context_parts.append(
                        f"[Centurion Status (localhost:{centurion_port})]:\n"
                        f"Memory pressure: {hw.get('system', {}).get('memory_pressure', 'unknown')}\n"
                        f"Active agents: {hw.get('allocated', {}).get('active_agents', 0)}\n"
                        f"Recommended max agents: {hw.get('recommended_max_agents', 0)}\n"
                        f"RAM available: {hw.get('system', {}).get('ram_available_mb', 0)}MB"
                    )
            except Exception as e:
                context_parts.append(f"[Centurion Status]: NOT RUNNING (localhost:{centurion_port}) - {e}")

        # Also inject TELEGRAM_BOT_MODE if .harness/ exists (covers resume scenarios
        # where user says "resume harness" without the exact "harness-loop" keyword)
        harness_exists = (Path(session.cwd) / ".harness").exists()
        if harness_exists or any(kw in msg_lower for kw in ["harness-loop", "harness loop", "后台模式", "后台运行", "后台执行"]):
            context_parts.append(
                "[TELEGRAM_BOT_MODE]\n"
                "You are running via a Telegram bot in pipe mode (claude -p).\n"
                "For harness-loop tasks:\n"
                "- Phase 1 (foreground): Ask clarifying questions, gather requirements, propose a design, and get explicit user confirmation.\n"
                "  This MUST take multiple turns. Do NOT skip the Q&A phase. On the FIRST message, always ask clarifying questions.\n"
                "- NEVER output [HARNESS_EXEC_READY] on the first response. The user must explicitly confirm the plan first\n"
                "  (e.g., say 'approved', 'confirmed', 'go ahead', 'looks good', 'yes', '可以', '确认', '开始').\n"
                "- Only AFTER the user has explicitly confirmed the plan in a SUBSEQUENT message:\n"
                "  Output the final plan summary, then output the marker [HARNESS_EXEC_READY] at the END of your response.\n"
                "  Do NOT start executing tasks. The bot will automatically start background execution.\n"
                "- Phase 2 runs in a separate background session with --continue."
            )

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
        prompt = self._inject_context(session, message)
        if session.first_done:
            args.append("--continue")
        args.append(prompt)

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

        # Prune old session files to prevent unbounded growth
        if proc.returncode == 0:
            self._prune_session_history(session)

        return (proc.returncode, stdout.strip() if stdout else "", stderr.strip() if stderr else "")

    def _run_claude_cli_streaming(self, session: GatewaySession, message: str, env: dict) -> Iterator[dict]:
        """Run Claude CLI in streaming mode and yield events as dicts.

        Yields dicts with keys 'type' and 'content':
        - {"type": "thinking", "content": "..."} for thinking content
        - {"type": "text", "content": "..."} for text content deltas
        - {"type": "done", "content": "full accumulated response"} at the end
        - {"type": "error", "content": "error message"} on error
        """
        args = [
            "claude", "-p",
            "--disable-slash-commands",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        prompt = self._inject_context(session, message)
        if session.first_done:
            args.append("--continue")
        args.append(prompt)

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

        # Determine timeout
        is_background = _is_background_session(session.chat_id)
        is_long_message = self._is_no_timeout_message(message)
        timeout = None if (is_background or is_long_message) else CLAUDE_TIMEOUT

        if timeout is None:
            reason = "background session" if is_background else "long-running message"
            logger.info("chat_id=%s: no timeout (%s)", session.chat_id, reason)

        # Watchdog thread for timeout
        timed_out = threading.Event()
        watchdog_cancel = threading.Event()

        def _watchdog():
            if watchdog_cancel.wait(timeout=timeout):
                return  # cancelled before timeout
            # Timeout reached — kill the process
            timed_out.set()
            logger.warning("chat_id=%s: streaming timeout after %ds, killing process", session.chat_id, timeout)
            self._kill_process(proc)

        if timeout is not None:
            wd_thread = threading.Thread(target=_watchdog, daemon=True)
            wd_thread.start()

        full_response = ""
        error_occurred = False

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("chat_id=%s: non-JSON stream line: %s", session.chat_id, line[:200])
                    continue

                event_type = event.get("type", "")

                # assistant message with content blocks (thinking and/or text)
                if event_type == "assistant":
                    content_blocks = event.get("message", {}).get("content", [])
                    for block in content_blocks:
                        if block.get("type") == "thinking":
                            thinking_text = block.get("thinking", "")
                            if thinking_text:
                                yield {"type": "thinking", "content": thinking_text}
                        elif block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                full_response += text
                                yield {"type": "text", "content": text}

                # content_block_delta with text or thinking
                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    delta_type = delta.get("type", "")
                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            full_response += text
                            yield {"type": "text", "content": text}
                    elif delta_type == "thinking_delta":
                        thinking = delta.get("thinking", "")
                        if thinking:
                            yield {"type": "thinking", "content": thinking}

                # result event — contains the full text
                elif event_type == "result":
                    result_text = event.get("result", "")
                    if result_text:
                        full_response = result_text

            # Wait for process to finish
            proc.wait()

        except Exception as e:
            error_occurred = True
            logger.error("chat_id=%s: streaming error: %s", session.chat_id, e)
            yield {"type": "error", "content": str(e)}

        finally:
            # Cancel watchdog
            watchdog_cancel.set()

        # Check for timeout
        if timed_out.is_set():
            yield {"type": "error", "content": f"Claude timed out after {timeout}s"}
            with session.lock:
                session._proc = None
            return

        # Update session state (same as _run_claude_cli)
        with session.lock:
            session.first_done = True
            session.last_active = time.time()
            session._proc = None
        self._persist_session(session)

        # Check stderr
        stderr = ""
        try:
            stderr = proc.stderr.read().strip() if proc.stderr else ""
        except Exception:
            pass

        if stderr:
            logger.info(
                "chat_id=%s stderr (rc=%d): %s",
                session.chat_id, proc.returncode, stderr[:500],
            )

        # Prune old session files on success
        if proc.returncode == 0:
            self._prune_session_history(session)

        # Handle non-zero exit code
        if proc.returncode != 0 and not error_occurred:
            yield {"type": "error", "content": f"Claude exited with code {proc.returncode}: {stderr[:500]}"}
            return

        # Final done event
        yield {"type": "done", "content": full_response}

    @staticmethod
    def _make_project_id(cwd: str) -> str:
        """Generate a short project_id from a CWD path (first 8 chars of md5)."""
        import hashlib
        return hashlib.md5(cwd.encode()).hexdigest()[:8]

    def _bg_task_key(self, bot_id: str, chat_id: str, project_id: str) -> str:
        """Construct a background task key: bot_id:chat_id:project_id."""
        return f"{bot_id}:{chat_id}:{project_id}"

    def _find_bg_tasks_for_chat(self, bot_id: str, chat_id: str) -> dict[str, dict]:
        """Find all background tasks matching a (bot_id, chat_id) pair."""
        prefix = f"{bot_id}:{chat_id}:"
        return {k: v for k, v in self._bg_tasks.items() if k.startswith(prefix)}

    def _has_running_bg_task(self, bot_id: str, chat_id: str) -> bool:
        """Check if ANY background task is running for this chat_id."""
        for key, task in self._find_bg_tasks_for_chat(bot_id, chat_id).items():
            if task["status"] in ("running", "chaining"):
                thread = task.get("thread")
                if thread and thread.is_alive():
                    return True
                else:
                    # Fix B1: Dead thread detected — auto-recover
                    logger.warning("Dead bg thread detected for %s (status=%s), auto-recovering", key, task["status"])
                    task["status"] = "failed"
                    task["result"] = (task.get("result") or "") + " [AUTO-RECOVERED: thread died]"
        return False

    def _should_route_to_fg(self, chat_id: str, bot_id: str) -> bool:
        """Check if a foreground message should be routed to a separate fg session.

        When a background task is running for this chat_id, the main session's CWD
        has an active Claude CLI process with --continue. Spawning another --continue
        in the same CWD would block on Claude CLI's internal session lock. Route
        foreground messages to a separate fg-{chat_id} session with its own CWD.
        """
        if chat_id.startswith("bg-") or chat_id.startswith("fg-"):
            return False
        return self._has_running_bg_task(bot_id, chat_id)

    def send(self, chat_id: str, message: str, bot_id: str = "default") -> str:
        """Send a message to Claude CLI for the given chat. Blocking call.

        If the session is busy, queues and waits (up to QUEUE_WAIT_TIMEOUT)
        instead of immediately rejecting.

        Memory guardrails:
        - Waits for sufficient free memory before spawning Claude CLI
        - On exit code -15 (SIGTERM / OOM kill), waits and retries automatically
        - Limits concurrent Claude processes to MAX_CLAUDE_PROCESSES
        """
        # Route to separate fg session if background is running (avoid CWD lock conflict)
        if self._should_route_to_fg(chat_id, bot_id):
            fg_chat_id = f"fg-{chat_id}"
            logger.info("Routing foreground message to fg session (bg running): chat_id=%s → %s", chat_id, fg_chat_id)
            return self.send(fg_chat_id, message, bot_id=bot_id)

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

    def send_streaming(self, chat_id: str, message: str, bot_id: str = "default") -> Iterator[dict]:
        """Send a message to Claude CLI and stream events back as a generator.

        Parallel to send() but yields event dicts instead of returning a string.
        No OOM retry loop — if the process dies, yields an error event.
        """
        # Route to separate fg session if background is running (avoid CWD lock conflict)
        if self._should_route_to_fg(chat_id, bot_id):
            fg_chat_id = f"fg-{chat_id}"
            logger.info("Routing streaming to fg session (bg running): chat_id=%s → %s", chat_id, fg_chat_id)
            yield from self.send_streaming(fg_chat_id, message, bot_id=bot_id)
            return

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
            yield {"type": "error", "content": "Too many concurrent Claude processes. Please try again later."}
            return

        session = self._get_or_create(chat_id, bot_id=bot_id)

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
                    # Release process slot since we never started
                    with self._process_count_lock:
                        self._claude_process_count = max(0, self._claude_process_count - 1)
                    yield {"type": "error", "content": "Still processing the previous message, please wait."}
                    return
            session.busy = True
            session.busy_since = time.time()
            session._ready.clear()

        try:
            # Memory guardrail: wait for sufficient free memory
            if not self._wait_for_memory(chat_id):
                logger.warning(
                    "chat_id=%s: Proceeding despite low memory (streaming)",
                    chat_id,
                )

            # Clean env: remove CLAUDECODE to avoid "nested session" error
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

            yield from self._run_claude_cli_streaming(session, message, env)

        except Exception as e:
            yield {"type": "error", "content": str(e)}
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

    def send_background(self, chat_id: str, message: str, bot_token: str, bot_id: str = "default", chain_depth: int = 0, project_id: str = "") -> dict:
        """Start a background Claude CLI task for the given chat.

        Uses a separate session (bg-{chat_id}-{project_id}) so it doesn't interfere with
        the main interactive session. Returns immediately. Only one background
        task per (chat_id, project_id) is allowed at a time. Sends the result to
        Telegram via bot API on completion.
        """
        # Resolve project_id from main session CWD if not provided
        main_session = self._get_or_create(chat_id, bot_id=bot_id)
        if not project_id:
            project_id = self._make_project_id(main_session.cwd)

        bg_key = self._bg_task_key(bot_id, chat_id, project_id)

        # Check if a background task is already running for this project
        with self._global_lock:
            existing = self._bg_tasks.get(bg_key)
            if existing and existing["status"] in ("running", "chaining"):
                thread = existing.get("thread")
                if thread and thread.is_alive():
                    elapsed = time.time() - existing.get("started_at", 0)
                    if elapsed > BUSY_STUCK_TIMEOUT:
                        logger.warning(
                            "Force-clearing stale bg task for chat_id=%s project=%s (running %ds)",
                            chat_id, project_id, int(elapsed),
                        )
                        existing["status"] = "failed"
                        existing["result"] = f"Force-cleared: exceeded {BUSY_STUCK_TIMEOUT}s timeout"
                    else:
                        return {"status": "rejected", "reason": "already running", "elapsed": int(elapsed)}
                else:
                    logger.warning("Recovering dead bg task for chat_id=%s project=%s", chat_id, project_id)
                    existing["status"] = "failed"
                    existing["result"] = "Thread died unexpectedly"

        bg_session_key = f"bg-{chat_id}-{project_id}"
        # Share CWD with main session so background work is visible to main chat
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
            "chain_depth": chain_depth,
            "project_id": project_id,
            "cwd": main_session.cwd,
        }
        self._bg_tasks[bg_key] = task_info

        def _run():
            try:
                try:
                    result = self.send(bg_session_key, message, bot_id=bot_id)
                    task_info["result"] = result[:500] if result else ""
                except Exception as e:
                    result = f"[ERROR] Background task failed: {e}"
                    task_info["status"] = "failed"
                    task_info["result"] = result[:500]
                    self._send_telegram_result(chat_id, result, bot_token)
                    return

                # Check for harness batch-chaining markers
                marker = self._parse_harness_marker(result)

                if marker is None:
                    # No marker — check if this is a harness session that should auto-chain
                    task_info["status"] = "completed"

                    cwd = task_info.get("cwd")
                    harness_progress = self._read_harness_progress(cwd) if cwd else None

                    if harness_progress and harness_progress.get("total", 0) > 0:
                        done = harness_progress.get("done", 0)
                        total = harness_progress.get("total", 0)

                        if done >= total:
                            # All done — treat as HARNESS_COMPLETE
                            self._send_telegram_result(
                                chat_id,
                                f"✅ Harness loop complete! All {total} tasks finished. (Marker was missing but tasks.json confirms completion.)",
                                bot_token,
                            )
                        elif done > 0 or harness_progress.get("in_progress", 0) == 0:
                            # Tasks remain — auto-chain even without marker
                            logger.warning(
                                "No harness marker but tasks.json shows %d/%d done. Auto-chaining.",
                                done, total,
                            )
                            self._send_telegram_result(
                                chat_id,
                                f"📊 Batch completed (no marker). tasks.json: {done}/{total} done. Auto-chaining...",
                                bot_token,
                            )
                            if chain_depth + 1 < MAX_HARNESS_CHAIN_DEPTH:
                                time.sleep(HARNESS_CHAIN_DELAY)
                                chain_message = "Resume the harness-loop. Continue the Execute Loop — pick up the next batch of ready tasks."
                                chain_result = self.send_background(
                                    chat_id, chain_message, bot_token,
                                    bot_id=bot_id, chain_depth=chain_depth + 1,
                                    project_id=project_id,
                                )
                                if chain_result.get("status") != "started":
                                    self._send_telegram_result(
                                        chat_id,
                                        f"⚠️ Auto-chain failed: {chain_result.get('reason', 'unknown')}. Resume manually.",
                                        bot_token,
                                    )
                        else:
                            # No progress — might be genuinely stuck
                            self._send_telegram_result(
                                chat_id,
                                f"⚠️ Harness batch completed but no progress detected ({done}/{total}). Check /status.",
                                bot_token,
                            )
                    else:
                        # Not a harness session — send full result normally
                        self._send_telegram_result(chat_id, result, bot_token)
                elif marker["type"] == "batch_done":
                    phase = marker["phase"]
                    done = marker["done"]
                    total = marker["total"]
                    # Keep status as "running" during chain window to prevent
                    # concurrent overwrites. Will be set to "completed" after
                    # the chain call succeeds (or fails).
                    task_info["status"] = "chaining"
                    # Send short progress to Telegram
                    self._send_telegram_result(
                        chat_id,
                        f"📊 Batch done — {phase}: {done}/{total} tasks complete. Chaining next batch (depth {chain_depth + 1})...",
                        bot_token,
                    )
                    # Check chain depth limit
                    if chain_depth + 1 >= MAX_HARNESS_CHAIN_DEPTH:
                        task_info["status"] = "completed"
                        self._send_telegram_result(
                            chat_id,
                            f"⚠️ Harness chain depth limit reached ({MAX_HARNESS_CHAIN_DEPTH}). Stopping auto-chain.",
                            bot_token,
                        )
                        return
                    # Delay then chain next batch
                    time.sleep(HARNESS_CHAIN_DELAY)
                    chain_message = "Resume the harness-loop. Continue the Execute Loop — pick up the next batch of ready tasks."
                    chain_result = self.send_background(
                        chat_id, chain_message, bot_token,
                        bot_id=bot_id, chain_depth=chain_depth + 1,
                        project_id=project_id,
                    )
                    # Fix A2: Only mark completed if chain actually started
                    if chain_result.get("status") == "started":
                        task_info["status"] = "completed"
                    else:
                        task_info["status"] = "chain_failed"
                        reason = chain_result.get("reason", "unknown")
                        logger.error(
                            "Harness chain dispatch FAILED for chat_id=%s depth=%d: %s",
                            chat_id, chain_depth + 1, chain_result,
                        )
                        self._send_telegram_result(
                            chat_id,
                            f"⚠️ Harness chain dispatch failed: {reason}\n"
                            f"Batch {done}/{total} done but next batch could not start.\n"
                            f"Use /status to check, then resume manually.",
                            bot_token,
                        )
                elif marker["type"] == "blocked":
                    task_info["status"] = "completed"
                    task_id = marker["task_id"]
                    reason = marker["reason"]
                    self._send_telegram_result(
                        chat_id,
                        f"🚫 Harness blocked on task {task_id}: {reason}\nSend a message to unblock.",
                        bot_token,
                    )
                elif marker["type"] == "complete":
                    task_info["status"] = "completed"
                    self._send_telegram_result(
                        chat_id,
                        "✅ Harness loop complete! All tasks finished.",
                        bot_token,
                    )
            except Exception as e:
                # Fix A1: Catch ANY exception in the entire _run() body
                logger.error("Background task crashed for chat_id=%s: %s", chat_id, e, exc_info=True)
                task_info["status"] = "failed"
                task_info["result"] = f"[CRASH] {e}"[:500]
                try:
                    self._send_telegram_result(chat_id, f"⚠️ Background task crashed: {e}", bot_token)
                except Exception:
                    pass
            finally:
                # Fix A1: Ensure status is ALWAYS terminal
                if task_info["status"] in ("running", "chaining"):
                    logger.warning(
                        "Background task for chat_id=%s ended with non-terminal status '%s', forcing to 'failed'",
                        chat_id, task_info["status"],
                    )
                    task_info["status"] = "failed"

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

    def _parse_harness_marker(self, result: str) -> dict | None:
        """Search the last 2000 chars of result for harness batch-chaining markers."""
        if not result:
            return None
        tail = result[-2000:]

        if HARNESS_COMPLETE_MARKER in tail:
            return {"type": "complete"}

        m = HARNESS_BATCH_DONE_RE.search(tail)
        if m:
            return {
                "type": "batch_done",
                "phase": m.group(1),
                "done": int(m.group(2)),
                "total": int(m.group(3)),
            }

        m = HARNESS_BLOCKED_RE.search(tail)
        if m:
            return {
                "type": "blocked",
                "task_id": m.group(1),
                "reason": m.group(2),
            }

        return None

    @staticmethod
    def _read_harness_progress(cwd: str) -> dict | None:
        """Read .harness/tasks.json from a CWD and return structured progress."""
        tasks_path = Path(cwd) / ".harness" / "tasks.json"
        if not tasks_path.exists():
            return None
        try:
            with open(tasks_path) as f:
                tasks_data = json.load(f)

            metadata = tasks_data.get("metadata", {})
            tasks = tasks_data.get("tasks", [])
            total = len(tasks)

            status_counts: dict[str, int] = {}
            for t in tasks:
                s = t.get("status", "pending")
                status_counts[s] = status_counts.get(s, 0) + 1

            phase_counts: dict[str, dict] = {}
            for t in tasks:
                phase = t.get("phase", "unknown")
                s = t.get("status", "pending")
                if phase not in phase_counts:
                    phase_counts[phase] = {"total": 0, "done": 0, "in_progress": 0, "blocked": 0, "pending": 0}
                phase_counts[phase]["total"] += 1
                if s in phase_counts[phase]:
                    phase_counts[phase][s] += 1

            return {
                "project_name": metadata.get("project_name", "unknown"),
                "current_phase": metadata.get("current_phase", "unknown"),
                "total": total,
                "done": status_counts.get("done", 0),
                "in_progress": status_counts.get("in_progress", 0),
                "blocked": status_counts.get("blocked", 0),
                "pending": status_counts.get("pending", 0),
                "phases": phase_counts,
            }
        except Exception as e:
            logger.warning("Failed to read harness tasks.json from %s: %s", cwd, e)
            return None

    def get_all_harness_status(self, chat_id: str, bot_id: str = "default") -> list[dict]:
        """Return structured harness progress for ALL background tasks for this chat_id."""
        tasks = self._find_bg_tasks_for_chat(bot_id, chat_id)
        if not tasks:
            # No bg tasks — check main session for harness data anyway
            main_key = self._session_key(bot_id, chat_id)
            session = self._sessions.get(main_key)
            if session:
                harness = self._read_harness_progress(session.cwd)
                if harness:
                    return [{
                        "bg_status": "idle",
                        "elapsed_seconds": 0,
                        "chain_depth": 0,
                        "project_id": self._make_project_id(session.cwd),
                        "cwd": session.cwd,
                        "harness": harness,
                    }]
            return []

        jobs = []
        for bg_key, task in tasks.items():
            bg_status = task["status"]
            chain_depth = task.get("chain_depth", 0)
            project_id = task.get("project_id", "unknown")
            elapsed = int(time.time() - task.get("started_at", time.time()))
            cwd = task.get("cwd")

            harness = self._read_harness_progress(cwd) if cwd else None

            jobs.append({
                "bg_status": bg_status,
                "elapsed_seconds": elapsed,
                "chain_depth": chain_depth,
                "project_id": project_id,
                "cwd": cwd,
                "harness": harness,
            })
        return jobs

    def get_harness_status(self, chat_id: str, bot_id: str = "default") -> dict:
        """Backward-compatible single-job harness status. Returns the first job or idle."""
        jobs = self.get_all_harness_status(chat_id, bot_id=bot_id)
        if not jobs:
            return {"bg_status": "idle", "elapsed_seconds": 0, "chain_depth": 0, "cwd": None, "harness": None}
        return jobs[0]

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

    def cleanup_stale_bg_tasks(self, chat_id: str, bot_id: str = "default") -> dict:
        """Clean up completed/failed background tasks for a chat_id.

        - Running tasks with live threads are skipped.
        - Completed/failed tasks get their harness data archived and are removed.
        - Returns summary dict with cleaned/archived/skipped counts.
        """
        tasks = self._find_bg_tasks_for_chat(bot_id, chat_id)

        cleaned = 0
        archived = 0
        skipped = 0
        details = []

        for bg_key, task in list(tasks.items()):
            status = task["status"]
            thread = task.get("thread")
            cwd = task.get("cwd")
            project_id = task.get("project_id", "unknown")
            harness = None
            harness_incomplete = False

            if cwd:
                harness = self._read_harness_progress(cwd)
                if harness and harness.get("total", 0) > 0:
                    done = harness.get("done", 0)
                    total = harness.get("total", 0)
                    if done < total:
                        harness_incomplete = True

            # Skip running tasks with live threads
            if status == "running" and thread and thread.is_alive():
                skipped += 1
                details.append(f"Skipped running: {project_id}")
                continue

            if harness_incomplete:
                skipped += 1
                details.append(
                    f"Skipped active harness: {project_id} ({harness.get('done', 0)}/{harness.get('total', 0)})"
                )
                continue

            # If status is "running" but thread is dead, treat as failed
            if status == "running":
                task["status"] = "failed"
                task["result"] = "Thread died (cleaned up)"
                status = "failed"

            # Archive harness data if session exists and fully complete
            if cwd and harness and harness.get("total", 0) > 0 and harness.get("done", 0) == harness.get("total", 0):
                harness_dir = Path(cwd) / ".harness"
                if harness_dir.exists():
                    session_key = self._session_key(bot_id, f"bg-{chat_id}-{project_id}")
                    session = self._sessions.get(session_key)
                    if session:
                        archive_id = self._archive_harness(session)
                        if archive_id:
                            archived += 1
                            details.append(f"Archived: {project_id}")

            # Remove the bg task entry
            with self._global_lock:
                self._bg_tasks.pop(bg_key, None)
            cleaned += 1
            details.append(f"Cleaned ({status}): {project_id}")

        # Also check the main session's CWD for completed harness projects
        main_key = self._session_key(bot_id, chat_id)
        main_session = self._sessions.get(main_key)
        if main_session:
            harness_dir = Path(main_session.cwd) / ".harness"
            if harness_dir.exists():
                harness = self._read_harness_progress(main_session.cwd)
                if harness and harness.get("total", 0) > 0 and harness["done"] == harness["total"]:
                    archive_id = self._archive_harness(main_session)
                    if archive_id:
                        archived += 1
                        details.append(f"Archived completed: {harness.get('project_name', 'unknown')}")
                        # Remove the .harness/ directory after archiving
                        import shutil
                        shutil.rmtree(str(harness_dir), ignore_errors=True)
                        details.append(f"Cleaned CWD: {main_session.cwd}")
                        cleaned += 1

        return {
            "cleaned": cleaned,
            "archived": archived,
            "skipped": skipped,
            "details": details,
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

        # Clean up fg/bg session files
        self._cleanup_session_files(session)
        self._delete_persisted_session(chat_id, bot_id=bot_id)

        # Also stop any background tasks tied to this chat
        bg_tasks = self._find_bg_tasks_for_chat(bot_id, chat_id)
        for bg_key in list(bg_tasks.keys()):
            task = self._bg_tasks.pop(bg_key, None)
            if task:
                thread = task.get("thread")
                if thread and thread.is_alive():
                    # Best effort: mark as cancelled; daemon thread will exit when send finishes
                    task["status"] = "cancelled"
                cwd = task.get("cwd")
                if cwd:
                    self._cleanup_session_files(GatewaySession(chat_id=bg_key, bot_id=bot_id, cwd=cwd))
        logger.info("Stopped session bot_id=%s chat_id=%s (bg tasks removed=%d)", bot_id, chat_id, len(bg_tasks))
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
                if session.busy:
                    continue
                idle_duration = now - session.last_active
                # Tiered retention: harness sessions get 3 days, regular sessions get 2 hours
                has_harness = (Path(session.cwd) / ".harness").exists() if os.path.exists(session.cwd) else False
                timeout = HARNESS_SESSION_TIMEOUT if has_harness else SESSION_IDLE_TIMEOUT
                if idle_duration > timeout:
                    to_remove.append(key)
            for key in to_remove:
                removed_sessions.append(self._sessions.pop(key))
                logger.info("Cleaned up idle session bot_id=%s chat_id=%s", removed_sessions[-1].bot_id, removed_sessions[-1].chat_id)
        # Archive harness data, then clean up files and DB outside the lock
        for session in removed_sessions:
            self._archive_harness(session)
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
                        is_bg = _is_background_session(session.chat_id)

                        if is_bg:
                            # Background sessions: never kill via cleanup thread
                            logger.debug(
                                "Skipping background session bot_id=%s chat_id=%s (busy for %ds)",
                                session.bot_id, session.chat_id, int(stuck_duration),
                            )
                        elif stuck_duration > BUSY_STUCK_TIMEOUT:
                            # Foreground session stuck beyond timeout: kill and recover
                            logger.warning(
                                "Auto-recovering stuck session bot_id=%s chat_id=%s (busy for %ds)",
                                session.bot_id, session.chat_id, int(stuck_duration),
                            )
                            if session._proc and session._proc.poll() is None:
                                self._kill_process(session._proc)
                                session._proc = None
                            session.busy = False
                            session.busy_since = 0.0
                            session._ready.set()


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
