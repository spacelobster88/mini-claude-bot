from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db.engine import get_db
from backend.services.scheduler import sync_job, run_job_now

router = APIRouter(prefix="/api/cron", tags=["cron"])


class CronJobCreate(BaseModel):
    name: str
    cron_expression: str
    command: str
    job_type: str = "shell"
    enabled: bool = True
    timezone: str | None = None


class CronJobUpdate(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    command: str | None = None
    job_type: str | None = None
    enabled: bool | None = None
    timezone: str | None = None


@router.get("")
def list_jobs():
    db = get_db()
    rows = db.execute("SELECT * FROM cron_jobs ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
def create_job(job: CronJobCreate):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """INSERT INTO cron_jobs (name, cron_expression, command, job_type, enabled, timezone, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (job.name, job.cron_expression, job.command, job.job_type, int(job.enabled), job.timezone, now, now),
    )
    db.commit()
    job_id = cursor.lastrowid
    sync_job(job_id)
    return {"id": job_id}


@router.put("/{job_id}")
def update_job(job_id: int, update: CronJobUpdate):
    db = get_db()
    row = db.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Job not found")

    current = dict(row)
    fields = update.model_dump(exclude_none=True)
    if not fields:
        return current

    if "enabled" in fields:
        fields["enabled"] = int(fields["enabled"])

    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    db.execute(f"UPDATE cron_jobs SET {set_clause} WHERE id = ?", values)
    db.commit()

    sync_job(job_id)
    updated = db.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(updated)


@router.delete("/{job_id}")
def delete_job(job_id: int):
    db = get_db()
    db.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
    db.commit()
    sync_job(job_id)  # removes from scheduler
    return {"deleted": True}


@router.post("/{job_id}/run")
def trigger_job(job_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Job not found")
    result = run_job_now(job_id)
    return {"result": result}
