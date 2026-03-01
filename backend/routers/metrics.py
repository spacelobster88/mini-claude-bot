"""Aggregated metrics endpoint for the public dashboard."""
from datetime import datetime, timezone

from fastapi import APIRouter

from backend.db.engine import get_db
from backend.services.claude_stats import read_claude_stats
from backend.services.system_metrics import collect as collect_system

router = APIRouter(prefix="/api")


@router.get("/metrics")
def get_metrics():
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

    # Claude stats (includes session/message counts and daily activity)
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
