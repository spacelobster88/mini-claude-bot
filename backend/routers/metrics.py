"""Aggregated metrics endpoint for the public dashboard.

Metrics are cached to a local JSON file by the /refresh endpoint (called by a
5-minute CRON job). The /metrics endpoint serves from this cache for fast reads.
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from backend.db.engine import get_db
from backend.services.claude_stats import read_claude_stats
from backend.services.system_metrics import collect as collect_system

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

METRICS_CACHE_PATH = Path(os.getenv(
    "METRICS_CACHE_PATH",
    os.path.expanduser("~/.mini-claude-bot/metrics_cache.json"),
))
_refresh_lock = threading.Lock()


def _collect_metrics() -> dict:
    """Collect all dashboard metrics from DB and system."""
    db = get_db()

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
    }


@router.post("/metrics/refresh")
def refresh_metrics():
    """Collect fresh metrics and cache to local JSON file.

    Called by a 5-minute CRON job. The cached file is served by GET /metrics.
    """
    with _refresh_lock:
        try:
            metrics = _collect_metrics()
            METRICS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(METRICS_CACHE_PATH, "w") as f:
                json.dump(metrics, f)
            logger.info("Metrics refreshed and cached to %s", METRICS_CACHE_PATH)
            return {"status": "ok", "timestamp": metrics["timestamp"]}
        except Exception as e:
            logger.error("Failed to refresh metrics: %s", e)
            return {"status": "error", "message": str(e)}


@router.get("/metrics")
def get_metrics():
    """Serve metrics from cache. Falls back to live collection if no cache."""
    if METRICS_CACHE_PATH.exists():
        try:
            with open(METRICS_CACHE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    # Fallback: collect live (slower)
    return _collect_metrics()
