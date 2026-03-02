#!/usr/bin/env python3
"""Collect metrics directly and push to Vercel Edge Config.

Imports backend modules directly — no HTTP server required.
Uses Vercel CLI's auth token (with auto-refresh) to write to Edge Config.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path so backend imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

EDGE_CONFIG_ID = "ecfg_f46v94r95ijeifeq3n0xv0yai1ia"
VERCEL_CLI_AUTH = Path.home() / "Library" / "Application Support" / "com.vercel.cli" / "auth.json"


def collect_db_health(db) -> dict:
    """Collect SQLite + vector search health stats."""
    from backend.config import DATABASE_PATH

    db_size = os.path.getsize(DATABASE_PATH) if os.path.exists(DATABASE_PATH) else 0
    chat_embeds = db.execute("SELECT COUNT(*) as c FROM chat_embeddings").fetchone()["c"]
    mem_embeds = db.execute("SELECT COUNT(*) as c FROM memory_embeddings").fetchone()["c"]
    return {
        "db_size_mb": round(db_size / (1024 * 1024), 1),
        "chat_embeddings": chat_embeds,
        "memory_embeddings": mem_embeds,
    }


def collect_ollama_models() -> list[dict]:
    """List locally available Ollama models."""
    try:
        out = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        models = []
        for line in out.split("\n")[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 3:
                models.append({"name": parts[0], "size": parts[2] + " " + parts[3] if len(parts) > 3 else parts[2]})
        return models
    except Exception:
        return []


def collect_services() -> list[dict]:
    """Detect running AI services/agents via process list."""
    try:
        ps_out = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return []

    checks = [
        {"name": "OpenClaw", "pattern": r"openclaw|wecom-callback-server", "type": "agent"},
        {"name": "ZeroClaw", "pattern": r"zeroclaw", "type": "agent"},
        {"name": "Ollama", "pattern": r"ollama", "type": "local"},
        {"name": "telegram-claude-hero", "pattern": r"telegram-claude-hero", "type": "bridge"},
    ]
    services = []
    for svc in checks:
        match = re.search(svc["pattern"], ps_out, re.IGNORECASE)
        services.append({"name": svc["name"], "type": svc["type"], "running": bool(match)})

    # Static subscription entries
    services.append({"name": "Codex", "type": "subscription", "running": True})
    services.append({"name": "GLM (z.ai)", "type": "subscription", "running": True})

    return services


def collect_metrics() -> dict:
    """Collect all dashboard metrics directly (no HTTP server needed)."""
    from backend.db.engine import get_db
    from backend.services.claude_stats import read_claude_stats
    from backend.services.system_metrics import collect as collect_system

    db = get_db()

    # Cron jobs
    rows = db.execute("SELECT * FROM cron_jobs ORDER BY id").fetchall()
    cron_jobs = [
        {
            "id": r["id"],
            "name": r["name"],
            "cron_expression": r["cron_expression"],
            "job_type": r["job_type"],
            "enabled": bool(r["enabled"]),
            "last_run_at": r["last_run_at"],
            "last_result_preview": (r["last_result"] or "")[:200] or None,
            "timezone": dict(r).get("timezone"),
        }
        for r in rows
    ]

    # Memory stats
    mem_count = db.execute("SELECT COUNT(*) as c FROM memory").fetchone()["c"]
    mem_cats = db.execute(
        "SELECT category, COUNT(*) as c FROM memory GROUP BY category"
    ).fetchall()
    mem_oldest = db.execute("SELECT MIN(created_at) as t FROM memory").fetchone()["t"]
    mem_newest = db.execute("SELECT MAX(created_at) as t FROM memory").fetchone()["t"]
    mem_items = db.execute(
        "SELECT key, content, category FROM memory ORDER BY category, key"
    ).fetchall()
    mem_items_list = [{"key": r["key"], "content": r["content"], "category": r["category"]} for r in mem_items]

    # Claude stats
    claude = read_claude_stats()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cron_jobs": cron_jobs,
        "memory": {
            "count": mem_count,
            "categories": {r["category"]: r["c"] for r in mem_cats},
            "oldest": mem_oldest,
            "newest": mem_newest,
            "items": mem_items_list,
        },
        "chat": {
            "session_count": claude.get("total_sessions", 0),
            "message_count": claude.get("total_messages", 0),
            "oldest_message": claude.get("first_session_date"),
            "newest_message": claude.get("daily_activity", [{}])[-1].get("date") if claude.get("daily_activity") else None,
        },
        "claude_usage": claude,
        "system": collect_system(),
        "db_health": collect_db_health(db),
        "ollama_models": collect_ollama_models(),
        "services": collect_services(),
    }


def get_vercel_token() -> str:
    """Read Vercel CLI auth token, refreshing if expired."""
    if not VERCEL_CLI_AUTH.exists():
        raise RuntimeError(f"Vercel CLI auth not found at {VERCEL_CLI_AUTH}")

    auth = json.loads(VERCEL_CLI_AUTH.read_text())
    token = auth["token"]
    expires_at = auth.get("expiresAt", 0)

    # Refresh if token expires within 5 minutes
    if expires_at and time.time() > expires_at - 300:
        refresh_token = auth.get("refreshToken")
        if not refresh_token:
            raise RuntimeError("Token expired and no refresh token available")

        # Use Vercel CLI to refresh (it handles token refresh internally)
        result = subprocess.run(
            ["/opt/homebrew/bin/vercel", "whoami"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Vercel CLI refresh failed: {result.stderr}")

        # Re-read the token that CLI refreshed
        refreshed = json.loads(VERCEL_CLI_AUTH.read_text())
        token = refreshed["token"]

    return token


def main():
    metrics = collect_metrics()
    token = get_vercel_token()

    resp = httpx.patch(
        f"https://api.vercel.com/v1/edge-config/{EDGE_CONFIG_ID}/items",
        json={
            "items": [
                {"operation": "upsert", "key": "metrics_current", "value": json.dumps(metrics)},
                {"operation": "upsert", "key": "metrics_last_push", "value": datetime.now(timezone.utc).isoformat()},
            ]
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )

    if resp.status_code == 200:
        print(f"OK: pushed metrics at {metrics['timestamp']}")
    else:
        print(f"ERROR: {resp.status_code} {resp.text}")


if __name__ == "__main__":
    main()
