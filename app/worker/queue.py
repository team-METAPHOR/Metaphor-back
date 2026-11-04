"""PostgreSQL-backed job queue operations (sync psycopg; the worker owns its own connection).

All functions expect an autocommit connection: each statement is its own transaction.
"""

import psycopg

ACTIVE = ("queued", "running", "cancelling")
_ABANDON = """
UPDATE validation_jobs
SET status = CASE WHEN status = 'cancelling' THEN 'cancelled' ELSE 'failed' END,
    backend_error_code = CASE WHEN status = 'cancelling' THEN %(cancel_code)s ELSE %(code)s END,
    lease_owner = NULL, lease_expires_at = NULL, finished_at = now()
"""


def claim_next(conn: psycopg.Connection, worker_id: str, *, lease_seconds: int) -> str | None:
    """Finalize cancelled-while-queued jobs, then atomically claim the oldest queued job."""
    conn.execute(
        "UPDATE validation_jobs SET status = 'cancelled', finished_at = now() "
        "WHERE status = 'queued' AND cancel_requested_at IS NOT NULL"
    )
    row = conn.execute(
        """
        UPDATE validation_jobs
        SET status = 'running', stage = 'received', lease_owner = %(w)s,
            lease_expires_at = now() + make_interval(secs => %(lease)s),
            started_at = now(), attempts = attempts + 1
        WHERE id = (
            SELECT id FROM validation_jobs
            WHERE status = 'queued' AND cancel_requested_at IS NULL
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id
        """,
        {"w": worker_id, "lease": lease_seconds},
    ).fetchone()
    return row[0] if row else None


def renew_lease(
    conn: psycopg.Connection, job_id: str, worker_id: str, *, lease_seconds: int
) -> bool:
    cursor = conn.execute(
        "UPDATE validation_jobs "
        "SET lease_expires_at = now() + make_interval(secs => %s) "
        "WHERE id = %s AND lease_owner = %s AND status IN ('running', 'cancelling')",
        (lease_seconds, job_id, worker_id),
    )
    return cursor.rowcount == 1


def finish_if_active(
    conn: psycopg.Connection, job_id: str, status: str, backend_error_code: str | None
) -> bool:
    """Set a terminal status only if the job is still active (never overwrite a result)."""
    cursor = conn.execute(
        "UPDATE validation_jobs SET status = %s, backend_error_code = %s, lease_owner = NULL, "
        "lease_expires_at = NULL, finished_at = now() "
        "WHERE id = %s AND status IN ('queued', 'running', 'cancelling')",
        (status, backend_error_code, job_id),
    )
    return cursor.rowcount == 1


def abandon(conn: psycopg.Connection, job_id: str, code: str, *, cancel_code: str) -> bool:
    """Close a job whose child exited without a result: cancelling → cancelled, else failed."""
    cursor = conn.execute(
        _ABANDON + "WHERE id = %(id)s AND status IN ('running', 'cancelling')",
        {"id": job_id, "code": code, "cancel_code": cancel_code},
    )
    return cursor.rowcount == 1


def reap_expired(conn: psycopg.Connection) -> int:
    """Jobs whose worker stopped renewing the lease. Never re-run: whether the paid engine call
    already happened is unknown, so a retry must be an explicit new job."""
    cursor = conn.execute(
        _ABANDON + "WHERE status IN ('running', 'cancelling') AND lease_expires_at < now()",
        {"code": "WORKER_LOST", "cancel_code": "WORKER_LOST"},
    )
    return cursor.rowcount
