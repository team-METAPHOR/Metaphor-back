import multiprocessing
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app.worker import queue
from app.worker.main import supervise
from tests.conftest import TEST_DATABASE_URL
from tests.factories import make_org, make_program, make_report, make_user

SYNC_URL = TEST_DATABASE_URL.replace("+psycopg", "")


@pytest.fixture
def conn():
    with psycopg.connect(SYNC_URL, autocommit=True) as connection:
        yield connection


async def seed_jobs(session, count, status="queued"):
    from app.services.jobs import create_job

    org = await make_org(session)
    user = await make_user(session, org, "admin")
    program = await make_program(session, org)
    await session.commit()
    ids = []
    for _ in range(count):
        report = await make_report(session, org, program, user)
        await session.commit()
        async with session.begin():
            job = await create_job(session, report, "initial")
            job.status = status
        ids.append(job.id)
    return ids


def status_of(conn, job_id):
    return conn.execute(
        "SELECT status, backend_error_code, finished_at FROM validation_jobs WHERE id = %s",
        (job_id,),
    ).fetchone()


async def test_two_claimers_get_distinct_jobs(db_session, conn):
    ids = await seed_jobs(db_session, 2)
    with psycopg.connect(SYNC_URL) as other:
        first = queue.claim_next(conn, "w1", lease_seconds=30)
        second = queue.claim_next(other, "w2", lease_seconds=30)
        other.commit()
    assert {first, second} == set(ids)
    assert queue.claim_next(conn, "w3", lease_seconds=30) is None
    row = conn.execute(
        "SELECT status, stage, lease_owner, attempts, started_at FROM validation_jobs "
        "WHERE id = %s",
        (first,),
    ).fetchone()
    assert row[:4] == ("running", "received", "w1", 1)
    assert row[4] is not None


async def test_claim_finalizes_queued_cancel_without_running(db_session, conn):
    (job_id,) = await seed_jobs(db_session, 1)
    conn.execute("UPDATE validation_jobs SET cancel_requested_at = now() WHERE id = %s", (job_id,))
    assert queue.claim_next(conn, "w1", lease_seconds=30) is None
    status, code, finished = status_of(conn, job_id)
    assert (status, code) == ("cancelled", None)
    assert finished is not None


async def test_renew_lease_only_for_owner_and_active(db_session, conn):
    await seed_jobs(db_session, 1)
    job_id = queue.claim_next(conn, "w1", lease_seconds=30)
    assert queue.renew_lease(conn, job_id, "w1", lease_seconds=30) is True
    assert queue.renew_lease(conn, job_id, "intruder", lease_seconds=30) is False
    conn.execute("UPDATE validation_jobs SET status = 'completed' WHERE id = %s", (job_id,))
    assert queue.renew_lease(conn, job_id, "w1", lease_seconds=30) is False


async def test_reap_marks_worker_lost_and_cancelling_cancelled(db_session, conn):
    lost, cancelling, finished, fresh = await seed_jobs(db_session, 4)
    past = datetime.now(UTC) - timedelta(seconds=5)
    future = datetime.now(UTC) + timedelta(seconds=60)
    for job_id, status, lease in (
        (lost, "running", past),
        (cancelling, "cancelling", past),
        (finished, "completed", past),
        (fresh, "running", future),
    ):
        conn.execute(
            "UPDATE validation_jobs SET status = %s, lease_expires_at = %s WHERE id = %s",
            (status, lease, job_id),
        )
    assert queue.reap_expired(conn) == 2
    assert status_of(conn, lost)[:2] == ("failed", "WORKER_LOST")
    assert status_of(conn, cancelling)[:2] == ("cancelled", "WORKER_LOST")
    assert status_of(conn, finished)[:2] == ("completed", None)
    assert status_of(conn, fresh)[:2] == ("running", None)


async def test_finish_if_active_does_not_overwrite_terminal(db_session, conn):
    (job_id,) = await seed_jobs(db_session, 1)
    conn.execute("UPDATE validation_jobs SET status = 'completed' WHERE id = %s", (job_id,))
    assert queue.finish_if_active(conn, job_id, "failed", "WORKER_TIMEOUT") is False
    assert status_of(conn, job_id)[0] == "completed"


def _hang(job_id: str) -> None:
    time.sleep(999)


def _crash(job_id: str) -> None:
    raise SystemExit(3)


@pytest.mark.parametrize(
    ("target", "expected"), [(_hang, "WORKER_TIMEOUT"), (_crash, "WORKER_CRASHED")]
)
async def test_supervise_finalizes_hung_or_crashed_child(db_session, conn, target, expected):
    await seed_jobs(db_session, 1)
    job_id = queue.claim_next(conn, "w1", lease_seconds=30)
    started = time.monotonic()
    process = supervise(
        conn,
        job_id,
        "w1",
        target=target,
        deadline_seconds=2,
        lease_seconds=30,
        heartbeat_seconds=0.2,
        context=multiprocessing.get_context("spawn"),
    )
    assert time.monotonic() - started < 8
    assert not process.is_alive()
    assert status_of(conn, job_id)[:2] == ("failed", expected)


async def test_supervise_cancelling_job_becomes_cancelled(db_session, conn):
    await seed_jobs(db_session, 1)
    job_id = queue.claim_next(conn, "w1", lease_seconds=30)
    conn.execute("UPDATE validation_jobs SET status = 'cancelling' WHERE id = %s", (job_id,))
    supervise(
        conn,
        job_id,
        "w1",
        target=_crash,
        deadline_seconds=5,
        lease_seconds=30,
        heartbeat_seconds=0.2,
        context=multiprocessing.get_context("spawn"),
    )
    assert status_of(conn, job_id)[:2] == ("cancelled", "CANCEL_FORCED")


def _sleep_short(job_id: str) -> None:
    time.sleep(30)


async def test_supervise_kills_child_when_lease_lost(db_session, conn):
    await seed_jobs(db_session, 1)
    job_id = queue.claim_next(conn, "w1", lease_seconds=30)
    # Another actor takes ownership (e.g. reaper + new claim): renewals start failing.
    conn.execute("UPDATE validation_jobs SET lease_owner = 'other' WHERE id = %s", (job_id,))
    started = time.monotonic()
    process = supervise(
        conn,
        job_id,
        "w1",
        target=_sleep_short,
        deadline_seconds=60,
        lease_seconds=30,
        heartbeat_seconds=0.2,
        context=multiprocessing.get_context("spawn"),
    )
    assert time.monotonic() - started < 10
    assert not process.is_alive()


async def test_supervise_kills_child_when_renewal_raises(db_session, conn, monkeypatch):
    await seed_jobs(db_session, 1)
    job_id = queue.claim_next(conn, "w1", lease_seconds=30)

    def broken(*args, **kwargs):
        raise psycopg.OperationalError("db down")

    monkeypatch.setattr(queue, "renew_lease", broken)
    holder = {}

    def capture(*args, **kwargs):
        holder["process"] = original(*args, **kwargs)
        return holder["process"]

    original = multiprocessing.get_context("spawn").Process
    context = multiprocessing.get_context("spawn")
    monkeypatch.setattr(context, "Process", capture)
    with pytest.raises(psycopg.OperationalError):
        supervise(
            conn,
            job_id,
            "w1",
            target=_sleep_short,
            deadline_seconds=60,
            lease_seconds=30,
            heartbeat_seconds=0.2,
            context=context,
        )
    assert not holder["process"].is_alive()
