"""Aggregated metrics endpoint for the public dashboard.

Metrics are cached to a local JSON file by the /refresh endpoint (called by a
5-minute CRON job). The /metrics endpoint serves from this cache for fast reads.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from backend.db.engine import get_db
from backend.services.claude_stats import read_claude_stats
from backend.services.system_metrics import collect as collect_system

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

HARNESS_ARCHIVE_INDEX = Path(os.path.expanduser("~/.claude-gateway-archives/index.json"))


def _collect_harness_summary() -> dict:
    """Collect running harness jobs and archived count."""
    from backend.services.session_manager import get_session_manager

    running_jobs = []
    seen_cwds = set()
    try:
        manager = get_session_manager()
        # 1. Scan bg tasks for actively running harness loops
        for key, task in manager._bg_tasks.items():
            cwd = task.get("cwd")
            if not cwd:
                continue
            harness = manager._read_harness_progress(cwd)
            if harness is None:
                continue
            seen_cwds.add(cwd)
            running_jobs.append({
                "bg_status": task.get("status", "unknown"),
                "elapsed_seconds": int(time.time() - task.get("started_at", time.time())),
                "chain_depth": task.get("chain_depth", 0),
                "project_id": task.get("project_id", "unknown"),
                "project_name": harness.get("project_name", "unknown"),
                "current_phase": harness.get("current_phase", "unknown"),
                "done": harness.get("done", 0),
                "total": harness.get("total", 0),
                "in_progress": harness.get("in_progress", 0),
                "blocked": harness.get("blocked", 0),
            })

        # 2. Scan filesystem for .harness/ directories in active sessions
        from backend.services.session_manager import SESSION_BASE_DIR
        base = Path(SESSION_BASE_DIR)
        if base.exists():
            for harness_dir in base.glob("*/*/.harness"):
                cwd = str(harness_dir.parent)
                if cwd in seen_cwds:
                    continue
                harness = manager._read_harness_progress(cwd)
                if harness is None:
                    continue
                chat_id = harness_dir.parent.name
                running_jobs.append({
                    "bg_status": "idle",
                    "elapsed_seconds": 0,
                    "chain_depth": 0,
                    "project_id": chat_id[:8],
                    "project_name": harness.get("project_name", "unknown"),
                    "current_phase": harness.get("current_phase", "unknown"),
                    "done": harness.get("done", 0),
                    "total": harness.get("total", 0),
                    "in_progress": harness.get("in_progress", 0),
                    "blocked": harness.get("blocked", 0),
                })
    except Exception as e:
        logger.warning("Failed to collect harness jobs: %s", e)

    # Load archived harness loops with details
    archived_projects = []
    try:
        if HARNESS_ARCHIVE_INDEX.exists():
            with open(HARNESS_ARCHIVE_INDEX) as f:
                index = json.load(f)
                for entry in index:
                    archived_projects.append({
                        "project_name": entry.get("project_name", "unknown"),
                        "archived_at": entry.get("archived_at"),
                        "tasks_done": entry.get("tasks_done", 0),
                        "tasks_total": entry.get("tasks_total", 0),
                        "status": entry.get("status", "unknown"),
                    })
    except Exception:
        pass

    # Separate active vs completed
    active_jobs = [j for j in running_jobs if j["done"] < j["total"]]
    completed_jobs = [j for j in running_jobs if j["done"] >= j["total"] and j["total"] > 0]

    return {
        "running_jobs": active_jobs,
        "completed_jobs": completed_jobs,
        "archived_count": len(archived_projects),
        "archived_projects": archived_projects,
    }


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

    # Harness loop status
    harness = _collect_harness_summary()

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
        "harness": harness,
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
