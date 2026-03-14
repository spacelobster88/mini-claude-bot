#!/usr/bin/env python3
"""Push metrics to Vercel Edge Config using curl (no external dependencies)."""
import json
import subprocess
import sys
import time
import os
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.engine import get_db
from backend.services.claude_stats import read_claude_stats
from backend.services.system_metrics import collect as collect_system

def collect_metrics() -> dict:
    """Collect all dashboard metrics."""
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

    # Collect system metrics (without Ollama - avoid timeout)
    # system = collect_system()

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
        },
        "claude_usage": claude,
        # "system": system,  # Removed to avoid timeout
        # "ollama_models": [],  # Removed to avoid timeout
    }


def get_vercel_token() -> str:
    """Read Vercel CLI auth token."""
    VERCEL_CLI_AUTH = Path.home() / "Library" / "Application Support" / "com.vercel.cli" / "auth.json"
    if not VERCEL_CLI_AUTH.exists():
        raise RuntimeError(f"Vercel CLI auth not found at {VERCEL_CLI_AUTH}")

    auth = json.loads(VERCEL_CLI_AUTH.read_text())
    token = auth.get("token")
    return token


def push_to_vercel(metrics: dict) -> bool:
    """Push metrics to Vercel Edge Config using curl."""
    EDGE_CONFIG_ID = "ecfg_f46v94r95ijeifeq3n0xv0yai1ia"
    VERCEL_CLI_AUTH = Path.home() / "Library" / "Application Support" / "com.vercel.cli" / "auth.json"

    if not VERCEL_CLI_AUTH.exists():
        raise RuntimeError(f"Vercel CLI auth not found at {VERCEL_CLI_AUTH}")

    auth = json.loads(VERCEL_CLI_AUTH.read_text())
    token = auth.get("token")

    timestamp = metrics.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    # Prepare metrics data
    metrics_data = {
        "items": [
            {"operation": "upsert", "key": "metrics_current", "value": json.dumps(metrics)},
            {"operation": "upsert", "key": "metrics_last_push", "value": timestamp},
        ]
    }

    # Use curl to push
    curl_cmd = [
        "curl", "-X", "PATCH",
        f"https://api.vercel.com/v1/edge-config/{EDGE_CONFIG_ID}/items",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(metrics_data),
        "--max-time", "30",  # 30 second timeout
    ]

    try:
        result = subprocess.run(
            curl_cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0:
            print(f"✅ OK: Pushed metrics at {timestamp}")
            return True
        else:
            print(f"❌ ERROR: {result.returncode} {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print(f"❌ ERROR: Request timed out after 30 seconds")
        return False
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return False


if __name__ == "__main__":
    metrics = collect_metrics()
    push_to_vercel(metrics)
