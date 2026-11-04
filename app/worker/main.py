"""Worker loop: reap → claim → supervise one engine child process per job.

Run: `uv run python -m app.worker.main`
"""

import logging
import multiprocessing
import os
import signal
import socket
import time
from collections.abc import Callable
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess

import psycopg

from app.core.config import get_settings
from app.worker import queue

logger = logging.getLogger(__name__)
KILL_GRACE_SECONDS = 3


def sync_database_url() -> str:
    return get_settings().database_url.replace("+psycopg", "")


def _child_entry(target: Callable[[str], None], job_id: str) -> None:
    # Own process group, so Chromium and other grandchildren die with the child.
    os.setsid()
    target(job_id)


def _kill_group(process: BaseProcess) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        process.join(KILL_GRACE_SECONDS)
        if not process.is_alive():
            return


def supervise(
    conn: psycopg.Connection,
    job_id: str,
    worker_id: str,
    *,
    target: Callable[[str], None],
    deadline_seconds: float,
    lease_seconds: int,
    heartbeat_seconds: float = 5,
    context: BaseContext | None = None,
) -> BaseProcess:
    """Run `target(job_id)` in a child, renew the lease, enforce the hard deadline, and make sure
    the job never stays active after the child is gone."""
    context = context or multiprocessing.get_context("spawn")
    process = context.Process(target=_child_entry, args=(target, job_id), daemon=False)
    process.start()
    started = time.monotonic()
    timed_out = False
    while process.is_alive():
        process.join(heartbeat_seconds)
        queue.renew_lease(conn, job_id, worker_id, lease_seconds=lease_seconds)
        if process.is_alive() and time.monotonic() - started > deadline_seconds:
            timed_out = True
            _kill_group(process)
    code = "WORKER_TIMEOUT" if timed_out else "WORKER_CRASHED"
    if queue.abandon(conn, job_id, code, cancel_code="CANCEL_FORCED"):
        logger.warning("job %s closed by supervisor: %s (exit %s)", job_id, code, process.exitcode)
    return process


def run_forever(target: Callable[[str], None], *, idle_seconds: float = 1.0) -> None:
    """ponytail: one job at a time per worker process; scale by running more workers."""
    settings = get_settings()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    with psycopg.connect(sync_database_url(), autocommit=True) as conn:
        while True:
            queue.reap_expired(conn)
            job_id = queue.claim_next(conn, worker_id, lease_seconds=settings.lease_seconds)
            if job_id is None:
                time.sleep(idle_seconds)
                continue
            supervise(
                conn,
                job_id,
                worker_id,
                target=target,
                deadline_seconds=settings.worker_hard_deadline_seconds,
                lease_seconds=settings.lease_seconds,
            )


if __name__ == "__main__":
    from app.core.logging import configure_logging
    from app.worker.runner import run_job  # added by the engine-runner task

    configure_logging()
    run_forever(run_job)
