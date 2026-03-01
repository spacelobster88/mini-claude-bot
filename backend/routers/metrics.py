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

    # Chat stats
    chat_sessions = db.execute(
        "SELECT COUNT(DISTINCT session_id) as c FROM chat_messages"
    ).fetchone()["c"]
    chat_messages = db.execute("SELECT COUNT(*) as c FROM chat_messages").fetchone()["c"]
    chat_oldest = db.execute("SELECT MIN(created_at) as t FROM chat_messages").fetchone()["t"]
    chat_newest = db.execute("SELECT MAX(created_at) as t FROM chat_messages").fetchone()["t"]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cron_jobs": cron_jobs,
        "memory": {
            "count": mem_count,
            "categories": {r["category"]: r["c"] for r in mem_cats},
            "oldest": mem_oldest,
            "newest": mem_newest,
        },
        "chat": {
            "session_count": chat_sessions,
            "message_count": chat_messages,
            "oldest_message": chat_oldest,
            "newest_message": chat_newest,
        },
        "claude_usage": read_claude_stats(),
        "system": collect_system(),
    }
