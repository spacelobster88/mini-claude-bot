"""Unit tests for the scheduler service."""
from backend.services.scheduler import _execute_job


def test_execute_shell_job(fresh_db):
    db = fresh_db
    db.execute(
        "INSERT INTO cron_jobs (id, name, cron_expression, command, job_type) VALUES (?, ?, ?, ?, ?)",
        (1, "echo-test", "* * * * *", "echo hello_from_test", "shell"),
    )
    db.commit()

    _execute_job(1)

    row = db.execute("SELECT last_result, last_run_at FROM cron_jobs WHERE id = 1").fetchone()
    assert row["last_run_at"] is not None
    assert "hello_from_test" in row["last_result"]


def test_execute_nonexistent_job(fresh_db):
    # Should not raise
    _execute_job(9999)


def test_execute_failing_command(fresh_db):
    db = fresh_db
    db.execute(
        "INSERT INTO cron_jobs (id, name, cron_expression, command, job_type) VALUES (?, ?, ?, ?, ?)",
        (2, "fail-test", "* * * * *", "false", "shell"),
    )
    db.commit()

    _execute_job(2)

    row = db.execute("SELECT last_result FROM cron_jobs WHERE id = 2").fetchone()
    assert row["last_result"] is not None  # should have stored something (empty stderr)
