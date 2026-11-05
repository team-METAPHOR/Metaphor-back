"""Retention purge. Run once a day (cron / k8s CronJob): `uv run python -m app.worker.purge`.

1. Expired artifacts: unlink the file, then delete the row.
2. Orphan files under STORAGE_ROOT with no artifact row, older than ORPHAN_AGE (failed uploads,
   engine dirs of jobs that died before their result was persisted).
3. Empty directories older than ORPHAN_AGE (a running engine creates its dir before writing).
4. Expired idempotency records.
5. Personal data in rows: `client_ip_hash` after a day (it only feeds the 1-hour widget IP
   limit), and, past the program's `retention_days`, the report's free text and client metadata
   plus its jobs' raw results, event details, questions and answers. Report and job rows stay
   for statistics; `reports.scrubbed_at` marks them. Stored duplicates and their screenshots
   are deleted. Reports with an active job wait.

Safe to run repeatedly and concurrently with the API/worker: nothing younger than ORPHAN_AGE
is touched unless its artifact row says it expired.
"""

import logging
import os
import time
from pathlib import Path, PurePosixPath

import psycopg

from app.integrations import storage
from app.models.validation_job import ACTIVE_JOB_STATUSES
from app.worker.main import sync_database_url

logger = logging.getLogger(__name__)
ORPHAN_AGE_SECONDS = 24 * 3600
SCRUB_BATCH = 500


def _inside_root(root: Path, storage_key: str) -> Path | None:
    """The path a key names, only if it is relative, has no '..' and its directory resolves
    under the root. Symlinks are not followed for the final component (we unlink the link)."""
    key = PurePosixPath(storage_key)
    if key.is_absolute() or ".." in key.parts or "\\" in storage_key or not key.parts:
        return None
    path = root.joinpath(*key.parts)
    try:
        parent = path.parent.resolve()
    except (OSError, RuntimeError):
        return None
    if parent != root and root not in parent.parents:
        return None
    return path


def _unlink(root: Path, artifact_id: str, storage_key: str) -> None:
    path = _inside_root(root, storage_key)
    if path is None:
        logger.warning("artifact %s has an unsafe storage key; row removed only", artifact_id)
    else:
        path.unlink(missing_ok=True)


def _purge_expired(conn: psycopg.Connection, root: Path) -> int:
    rows = conn.execute("SELECT id, storage_key FROM artifacts WHERE expires_at < now()").fetchall()
    for artifact_id, storage_key in rows:
        _unlink(root, artifact_id, storage_key)
        conn.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
    return len(rows)


def _old(path: Path, cutoff: float) -> bool:
    try:
        return path.lstat().st_mtime < cutoff
    except FileNotFoundError:
        return False


def _purge_orphans(conn: psycopg.Connection, root: Path, cutoff: float) -> tuple[int, int]:
    if not root.is_dir():
        return 0, 0
    # ponytail: loads every key into memory; page by org directory if this reaches millions.
    known = {key for (key,) in conn.execute("SELECT storage_key FROM artifacts")}
    files = dirs = 0
    for current, subdirs, names in os.walk(root, topdown=False, followlinks=False):
        directory = Path(current)
        for name in names:
            path = directory / name
            if path.relative_to(root).as_posix() not in known and _old(path, cutoff):
                path.unlink(missing_ok=True)
                files += 1
        # Symlinked directories show up in subdirs but are never walked: unlink old orphans.
        for name in subdirs:
            path = directory / name
            if path.is_symlink() and _old(path, cutoff):
                path.unlink(missing_ok=True)
                files += 1
        if directory != root and _old(directory, cutoff):
            try:
                directory.rmdir()  # only succeeds when empty
                dirs += 1
            except OSError:
                pass
    return files, dirs


def _clear_ip_hashes(conn: psycopg.Connection) -> int:
    return conn.execute(
        "UPDATE reports SET client_ip_hash = NULL"
        " WHERE client_ip_hash IS NOT NULL AND created_at < now() - interval '24 hours'"
    ).rowcount


def _scrub_reports(conn: psycopg.Connection, root: Path) -> int:
    """One batch per run; the cron catches up on a backlog over the following runs. Stored
    duplicates (text, context, masked screenshot) are deleted outright; `duplicate_count`
    stays on the report."""
    with conn.transaction():
        ids = [
            report_id
            for (report_id,) in conn.execute(
                """
                SELECT r.id FROM reports r JOIN programs p ON p.id = r.program_id
                WHERE r.scrubbed_at IS NULL
                  AND r.created_at < now() - make_interval(days => p.retention_days)
                  AND NOT EXISTS (
                    SELECT 1 FROM validation_jobs j
                    WHERE j.report_id = r.id AND j.status = ANY(%(active)s)
                  )
                ORDER BY r.created_at
                LIMIT %(limit)s
                FOR UPDATE OF r SKIP LOCKED
                """,
                {"active": list(ACTIVE_JOB_STATUSES), "limit": SCRUB_BATCH},
            )
        ]
        if ids:
            # Re-check under the row lock: a create_job that committed while we waited for the
            # snapshot is visible to this new statement (READ COMMITTED).
            busy = {
                report_id
                for (report_id,) in conn.execute(
                    "SELECT DISTINCT report_id FROM validation_jobs"
                    " WHERE report_id = ANY(%(ids)s) AND status = ANY(%(active)s)",
                    {"ids": ids, "active": list(ACTIVE_JOB_STATUSES)},
                )
            }
            ids = [report_id for report_id in ids if report_id not in busy]
        if not ids:
            return 0
        params = {"ids": ids}
        jobs = "SELECT id FROM validation_jobs WHERE report_id = ANY(%(ids)s)"
        conn.execute(f"DELETE FROM qa_results WHERE job_id IN ({jobs})", params)
        conn.execute(
            f"UPDATE job_events SET detail = '{{}}'::jsonb WHERE job_id IN ({jobs})", params
        )
        conn.execute(
            "UPDATE validation_jobs SET questions = '[]'::jsonb, answers = NULL"
            " WHERE report_id = ANY(%(ids)s)",
            params,
        )
        conn.execute(
            "UPDATE clarifications SET answers = '' WHERE report_id = ANY(%(ids)s)", params
        )
        duplicate_files = conn.execute(
            """
            DELETE FROM artifacts WHERE id IN (
                SELECT input_artifact_id FROM report_duplicates WHERE report_id = ANY(%(ids)s)
            ) RETURNING id, storage_key
            """,
            params,
        ).fetchall()
        conn.execute("DELETE FROM report_duplicates WHERE report_id = ANY(%(ids)s)", params)
        conn.execute(
            """
            UPDATE reports SET title = '', description = '', expected_result = '',
                test_data = '', client_context = NULL, client_ip_hash = NULL,
                scrubbed_at = now(), updated_at = now()
            WHERE id = ANY(%(ids)s)
            """,
            params,
        )
    # After commit: a crash here only leaves files without a row, which the orphan pass removes.
    for artifact_id, storage_key in duplicate_files:
        _unlink(root, artifact_id, storage_key)
    return len(ids)


def purge(conn: psycopg.Connection) -> dict[str, int]:
    root = storage.storage_root()
    cutoff = time.time() - ORPHAN_AGE_SECONDS
    artifacts = _purge_expired(conn, root)
    orphans, dirs = _purge_orphans(conn, root, cutoff)
    records = conn.execute("DELETE FROM idempotency_records WHERE expires_at < now()").rowcount
    ip_hashes = _clear_ip_hashes(conn)
    scrubbed = _scrub_reports(conn, root)
    return {
        "artifacts": artifacts,
        "orphans": orphans,
        "dirs": dirs,
        "idempotency_records": records,
        "reports_scrubbed": scrubbed,
        "ip_hashes_cleared": ip_hashes,
    }


def main() -> None:
    from app.core.logging import configure_logging

    configure_logging()
    logging.getLogger().setLevel(logging.INFO)
    with psycopg.connect(sync_database_url(), autocommit=True) as conn:
        logger.info("purge done: %s", purge(conn))


if __name__ == "__main__":
    main()
