"""MCP server that proxies to the mini-claude-bot FastAPI backend."""
import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mini-claude-bot")

API_BASE = os.getenv("MCB_API_BASE", "http://127.0.0.1:8000/api")
DEFAULT_TIMEOUT = int(os.getenv("MCB_MCP_TIMEOUT", "30"))
GATEWAY_TIMEOUT = int(os.getenv("MCB_GATEWAY_TIMEOUT", "960"))  # 16min, exceeds CLAUDE_TIMEOUT (15min)
DEFAULT_BOT_ID = os.getenv("MCB_BOT_ID", "default")


def _request(method: str, path: str, timeout: int = DEFAULT_TIMEOUT, **kwargs) -> dict | list:
    try:
        r = httpx.request(method, f"{API_BASE}{path}", timeout=timeout, **kwargs)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        return {"error": f"Cannot connect to backend at {API_BASE}. Is it running?"}
    except httpx.TimeoutException:
        return {"error": f"Request to {path} timed out after {timeout}s"}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:500]}"}


def _get(path: str, params: dict | None = None) -> dict | list:
    return _request("GET", path, params=params)


def _post(path: str, json: dict | None = None) -> dict:
    return _request("POST", path, json=json)


def _put(path: str, json: dict) -> dict:
    return _request("PUT", path, json=json)


def _delete(path: str) -> dict:
    return _request("DELETE", path)


async def _post_gateway_async(path: str, json: dict | None = None) -> dict:
    """Async POST for long-running gateway operations."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{API_BASE}{path}", json=json, timeout=GATEWAY_TIMEOUT)
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError:
        return {"error": f"Cannot connect to backend at {API_BASE}. Is it running?"}
    except httpx.TimeoutException:
        return {"error": f"Gateway request timed out after {GATEWAY_TIMEOUT}s"}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:500]}"}


# ── Health ────────────────────────────────────────────────────

@mcp.tool()
def health_check() -> dict:
    """Check if the mini-claude-bot backend is running and healthy."""
    return _get("/health")


# ── CRON Jobs ──────────────────────────────────────────────────

@mcp.tool()
def list_cron_jobs(bot_id: str | None = None) -> list[dict]:
    """List all scheduled CRON jobs for a bot.

    Args:
        bot_id: Bot identifier for multi-tenant isolation. Defaults to MCB_BOT_ID env or 'default'.
    """
    return _get("/cron", params={"bot_id": bot_id or DEFAULT_BOT_ID})


@mcp.tool()
def create_cron_job(name: str, cron_expression: str, command: str, job_type: str = "shell", timezone: str | None = None, bot_id: str | None = None) -> dict:
    """Create a new CRON job.

    Args:
        name: Human-readable job name
        cron_expression: Standard cron expression (e.g. '0 9 * * *' for daily 9am)
        command: Shell command or Claude prompt to run
        job_type: 'shell' for shell commands or 'claude' for Claude prompts
        timezone: IANA timezone (e.g. 'Asia/Shanghai', 'America/Los_Angeles'). Defaults to system timezone.
        bot_id: Bot identifier for multi-tenant isolation. Defaults to MCB_BOT_ID env or 'default'.
    """
    payload = {
        "name": name,
        "cron_expression": cron_expression,
        "command": command,
        "job_type": job_type,
        "enabled": True,
        "bot_id": bot_id or DEFAULT_BOT_ID,
    }
    if timezone:
        payload["timezone"] = timezone
    return _post("/cron", json=payload)


@mcp.tool()
def update_cron_job(
    job_id: int,
    name: str | None = None,
    cron_expression: str | None = None,
    command: str | None = None,
    job_type: str | None = None,
    enabled: bool | None = None,
) -> dict:
    """Update an existing CRON job. Only pass the fields you want to change.

    Args:
        job_id: The ID of the job to update
        name: New job name
        cron_expression: New cron schedule
        command: New command to run
        job_type: New job type ('shell' or 'claude')
        enabled: Enable or disable the job
    """
    payload = {}
    if name is not None:
        payload["name"] = name
    if cron_expression is not None:
        payload["cron_expression"] = cron_expression
    if command is not None:
        payload["command"] = command
    if job_type is not None:
        payload["job_type"] = job_type
    if enabled is not None:
        payload["enabled"] = enabled
    return _put(f"/cron/{job_id}", json=payload)


@mcp.tool()
def delete_cron_job(job_id: int) -> dict:
    """Delete a CRON job.

    Args:
        job_id: The ID of the job to delete
    """
    return _delete(f"/cron/{job_id}")


@mcp.tool()
def run_cron_job(job_id: int) -> dict:
    """Manually trigger a CRON job to run immediately.

    Args:
        job_id: The ID of the job to run
    """
    return _post(f"/cron/{job_id}/run")


@mcp.tool()
def get_cron_job_history(job_id: int, limit: int = 20) -> list[dict]:
    """Get execution history for a specific CRON job.

    Args:
        job_id: The ID of the job
        limit: Maximum number of history entries to return
    """
    return _get(f"/cron/{job_id}/history", params={"limit": limit})


# ── Memory ─────────────────────────────────────────────────────

@mcp.tool()
def add_memory(key: str, content: str, category: str = "general", bot_id: str | None = None) -> dict:
    """Store a memory with a unique key. Auto-embeds for vector search.

    Args:
        key: Unique identifier for this memory
        content: The memory content to store
        category: Category for organizing memories (e.g. 'preferences', 'facts', 'architecture')
        bot_id: Bot identifier for multi-tenant isolation. Defaults to MCB_BOT_ID env or 'default'.
    """
    return _post("/memory", json={"key": key, "content": content, "category": category, "bot_id": bot_id or DEFAULT_BOT_ID})


@mcp.tool()
def update_memory(memory_id: int, content: str | None = None, category: str | None = None) -> dict:
    """Update an existing memory's content or category.

    Args:
        memory_id: The ID of the memory to update
        content: New content (triggers re-embedding)
        category: New category
    """
    payload = {}
    if content is not None:
        payload["content"] = content
    if category is not None:
        payload["category"] = category
    return _put(f"/memory/{memory_id}", json=payload)


@mcp.tool()
def search_memory(query: str, limit: int = 5, bot_id: str | None = None) -> list[dict]:
    """Semantic vector search across stored memories for a bot.

    Args:
        query: Natural language search query
        limit: Maximum number of results to return
        bot_id: Bot identifier for multi-tenant isolation. Defaults to MCB_BOT_ID env or 'default'.
    """
    return _get("/memory/search", params={"q": query, "limit": limit, "bot_id": bot_id or DEFAULT_BOT_ID})


@mcp.tool()
def list_memories(category: str | None = None, bot_id: str | None = None) -> list[dict]:
    """List stored memories for a bot, optionally filtered by category.

    Args:
        category: Optional category filter
        bot_id: Bot identifier for multi-tenant isolation. Defaults to MCB_BOT_ID env or 'default'.
    """
    params = {"bot_id": bot_id or DEFAULT_BOT_ID}
    if category:
        params["category"] = category
    return _get("/memory", params=params)


@mcp.tool()
def delete_memory(memory_id: int) -> dict:
    """Delete a memory by ID.

    Args:
        memory_id: The ID of the memory to delete
    """
    return _delete(f"/memory/{memory_id}")


# ── Chat History ───────────────────────────────────────────────

@mcp.tool()
def search_chat_history(query: str, limit: int = 10, bot_id: str | None = None) -> list[dict]:
    """Semantic vector search across chat history for a bot.

    Args:
        query: Natural language search query
        limit: Maximum number of results to return
        bot_id: Bot identifier for multi-tenant isolation. If None, searches all bots.
    """
    params = {"q": query, "limit": limit}
    if bot_id:
        params["bot_id"] = bot_id
    return _get("/chat/search", params=params)


@mcp.tool()
def list_chat_sessions(bot_id: str | None = None) -> list[dict]:
    """List chat sessions with message counts.

    Args:
        bot_id: Optional bot identifier to filter sessions. If None, lists all.
    """
    params = {}
    if bot_id:
        params["bot_id"] = bot_id
    return _get("/chat/sessions", params=params if params else None)


@mcp.tool()
def get_chat_session(session_id: str) -> dict:
    """Get messages for a specific chat session.

    Args:
        session_id: The session ID (e.g. 'gw-FridayBot-6838572051')
    """
    return _get(f"/chat/sessions/{session_id}")


@mcp.tool()
def get_metrics() -> dict:
    """Get aggregated system metrics (CRON stats, memory stats, Claude usage, system info)."""
    return _get("/metrics")


# ── Gateway Sessions ──────────────────────────────────────────

@mcp.tool()
def list_gateway_sessions(bot_id: str | None = None) -> list[dict]:
    """List all active gateway sessions (multi-chat Claude CLI sessions).

    Returns session info including chat_id, bot_id, busy status, idle time.

    Args:
        bot_id: Optional filter to show only sessions for a specific bot
    """
    params = {"bot_id": bot_id} if bot_id else None
    return _get("/gateway/sessions", params=params)


@mcp.tool()
def stop_gateway_session(chat_id: str, bot_id: str = "default") -> dict:
    """Stop a gateway session and clean up its Claude CLI state.

    Args:
        chat_id: The Telegram chat ID of the session to stop
        bot_id: Bot identifier for multi-tenant isolation (default: 'default')
    """
    return _post("/gateway/stop", json={"chat_id": chat_id, "bot_id": bot_id})


@mcp.tool()
def reset_gateway_session(chat_id: str, bot_id: str = "default") -> dict:
    """Reset a gateway session's state (emergency recovery).

    Stops the session, clears Claude CLI state, and allows fresh start.

    Args:
        chat_id: The Telegram chat ID of the session to reset
        bot_id: Bot identifier for multi-tenant isolation (default: 'default')
    """
    return _request("POST", f"/gateway/sessions/{chat_id}/reset?bot_id={bot_id}")


@mcp.tool()
async def send_gateway_message(chat_id: str, message: str, bot_id: str = "default") -> dict:
    """Send a message to a specific chat via the gateway.

    Creates a new session if one doesn't exist for this chat_id.
    Uses Claude CLI with CWD-based isolation per (bot_id, chat_id).

    Args:
        chat_id: The Telegram chat ID to send to
        message: The message/prompt to send to Claude
        bot_id: Bot identifier for multi-tenant isolation (default: 'default')
    """
    return await _post_gateway_async("/gateway/send", json={"chat_id": chat_id, "message": message, "bot_id": bot_id})


@mcp.tool()
def send_background_message(chat_id: str, message: str, bot_token: str, bot_id: str = "default") -> dict:
    """Send a message to run in the background (non-blocking).

    For long-running tasks like harness loops that shouldn't block the main chat.
    Results are sent to Telegram when complete.

    Args:
        chat_id: The Telegram chat ID
        message: The message/prompt to send to Claude
        bot_token: Telegram bot token for sending results back
        bot_id: Bot identifier for multi-tenant isolation (default: 'default')
    """
    return _post("/gateway/send-background", json={"chat_id": chat_id, "message": message, "bot_token": bot_token, "bot_id": bot_id})


@mcp.tool()
def get_background_status(chat_id: str, bot_id: str = "default") -> dict:
    """Get the status of a background task for a chat.

    Args:
        chat_id: The Telegram chat ID to check
        bot_id: Bot identifier for multi-tenant isolation (default: 'default')
    """
    return _get(f"/gateway/background-status/{chat_id}", params={"bot_id": bot_id})


if __name__ == "__main__":
    mcp.run()
