"""MCP server that proxies to the mini-claude-bot FastAPI backend."""
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mini-claude-bot")

API_BASE = "http://localhost:8000/api"


def _get(path: str, params: dict | None = None) -> dict | list:
    r = httpx.get(f"{API_BASE}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path: str, json: dict | None = None) -> dict:
    r = httpx.post(f"{API_BASE}{path}", json=json, timeout=30)
    r.raise_for_status()
    return r.json()


def _put(path: str, json: dict) -> dict:
    r = httpx.put(f"{API_BASE}{path}", json=json, timeout=30)
    r.raise_for_status()
    return r.json()


def _delete(path: str) -> dict:
    r = httpx.delete(f"{API_BASE}{path}", timeout=30)
    r.raise_for_status()
    return r.json()


# ── CRON Jobs ──────────────────────────────────────────────────

@mcp.tool()
def list_cron_jobs() -> list[dict]:
    """List all scheduled CRON jobs."""
    return _get("/cron")


@mcp.tool()
def create_cron_job(name: str, cron_expression: str, command: str, job_type: str = "shell", timezone: str | None = None) -> dict:
    """Create a new CRON job.

    Args:
        name: Human-readable job name
        cron_expression: Standard cron expression (e.g. '0 9 * * *' for daily 9am)
        command: Shell command or Claude prompt to run
        job_type: 'shell' for shell commands or 'claude' for Claude prompts
        timezone: IANA timezone (e.g. 'Asia/Shanghai', 'America/Los_Angeles'). Defaults to system timezone.
    """
    payload = {
        "name": name,
        "cron_expression": cron_expression,
        "command": command,
        "job_type": job_type,
        "enabled": True,
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


# ── Memory ─────────────────────────────────────────────────────

@mcp.tool()
def add_memory(key: str, content: str, category: str = "general") -> dict:
    """Store a memory with a unique key. Auto-embeds for vector search.

    Args:
        key: Unique identifier for this memory
        content: The memory content to store
        category: Category for organizing memories (e.g. 'preferences', 'facts', 'architecture')
    """
    return _post("/memory", json={"key": key, "content": content, "category": category})


@mcp.tool()
def search_memory(query: str, limit: int = 5) -> list[dict]:
    """Semantic vector search across all stored memories.

    Args:
        query: Natural language search query
        limit: Maximum number of results to return
    """
    return _get("/memory/search", params={"q": query, "limit": limit})


@mcp.tool()
def list_memories(category: str | None = None) -> list[dict]:
    """List all stored memories, optionally filtered by category.

    Args:
        category: Optional category filter
    """
    params = {"category": category} if category else None
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
def search_chat_history(query: str, limit: int = 10) -> list[dict]:
    """Semantic vector search across all chat history.

    Args:
        query: Natural language search query
        limit: Maximum number of results to return
    """
    return _get("/chat/search", params={"q": query, "limit": limit})


@mcp.tool()
def list_chat_sessions() -> list[dict]:
    """List all chat sessions with message counts."""
    return _get("/chat/sessions")


if __name__ == "__main__":
    mcp.run()
