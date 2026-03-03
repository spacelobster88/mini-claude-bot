"""Lightweight schema migration framework for SQLite.

Tracks applied versions in a _schema_version table.
Each migration is an (version, description, sql) tuple.
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

# Version 1 = baseline (existing schema as of initial commit).
# Only add NEW migrations here. Baseline tables are created in engine._init_tables().
MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "baseline", ""),
    (2, "add timezone to cron_jobs", ""),  # column now in CREATE TABLE; old DBs already migrated
    (3, "add gateway_sessions table", """
        CREATE TABLE IF NOT EXISTS gateway_sessions (
            chat_id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            first_done INTEGER DEFAULT 0,
            busy INTEGER DEFAULT 0,
            last_active REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """),
    (4, "add cron_job_runs table", """
        CREATE TABLE IF NOT EXISTS cron_job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            started_at TIMESTAMP NOT NULL,
            finished_at TIMESTAMP,
            result TEXT,
            success INTEGER DEFAULT 1,
            FOREIGN KEY (job_id) REFERENCES cron_jobs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cron_runs_job ON cron_job_runs(job_id);
        CREATE INDEX IF NOT EXISTS idx_cron_runs_started ON cron_job_runs(started_at);
    """),
]


def run_migrations(db: sqlite3.Connection) -> None:
    """Apply any pending migrations."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version INTEGER PRIMARY KEY
        )
    """)

    applied = {r[0] for r in db.execute("SELECT version FROM _schema_version").fetchall()}

    for version, description, sql in MIGRATIONS:
        if version in applied:
            continue
        if sql.strip():
            db.executescript(sql)
        db.execute("INSERT INTO _schema_version (version) VALUES (?)", (version,))
        db.commit()
        logger.info("Applied migration v%d: %s", version, description)
