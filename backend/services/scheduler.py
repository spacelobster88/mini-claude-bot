import asyncio
import subprocess
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from backend.db.engine import get_db

scheduler = BackgroundScheduler()


def start_scheduler() -> None:
    """Load all enabled jobs from DB and start the scheduler."""
    db = get_db()
    rows = db.execute("SELECT * FROM cron_jobs WHERE enabled = 1").fetchall()
    for row in rows:
        _add_job_to_scheduler(dict(row))
    scheduler.start()


def shutdown_scheduler() -> None:
    scheduler.shutdown(wait=False)


def _add_job_to_scheduler(job: dict) -> None:
    tz = job.get("timezone") or None
    trigger = CronTrigger.from_crontab(job["cron_expression"], timezone=tz)
    scheduler.add_job(
        _execute_job,
        trigger=trigger,
        id=str(job["id"]),
        args=[job["id"]],
        replace_existing=True,
    )


def _execute_job(job_id: int) -> None:
    db = get_db()
    row = db.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return

    job = dict(row)
    now = datetime.now(timezone.utc).isoformat()

    try:
        if job["job_type"] == "shell":
            result = subprocess.run(
                job["command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=900,
            )
            output = result.stdout or result.stderr
        elif job["job_type"] == "claude":
            result = subprocess.run(
                ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "text", job["command"]],
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = result.stdout or result.stderr
        else:
            output = f"Unknown job type: {job['job_type']}"
    except subprocess.TimeoutExpired:
        output = "ERROR: Job timed out"
    except Exception as e:
        output = f"ERROR: {e}"

    db.execute(
        "UPDATE cron_jobs SET last_run_at = ?, last_result = ?, updated_at = ? WHERE id = ?",
        (now, output[:10000], now, job_id),
    )
    db.commit()


def sync_job(job_id: int) -> None:
    """Re-sync a single job from DB to the scheduler."""
    db = get_db()
    row = db.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        _remove_job(job_id)
        return

    job = dict(row)
    if job["enabled"]:
        _add_job_to_scheduler(job)
    else:
        _remove_job(job_id)


def _remove_job(job_id: int) -> None:
    try:
        scheduler.remove_job(str(job_id))
    except Exception:
        pass


def run_job_now(job_id: int) -> str:
    """Execute a job immediately and return the result."""
    _execute_job(job_id)
    db = get_db()
    row = db.execute("SELECT last_result FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()
    return row["last_result"] if row else "Job not found"
