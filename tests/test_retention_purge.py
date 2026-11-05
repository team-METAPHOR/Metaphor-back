import hashlib
import os
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.core.ids import new_id
from app.integrations import storage
from app.models import Artifact, Clarification, IdempotencyRecord, QaResult, Report, ValidationJob
from app.models.validation_job import JobEvent
from app.worker import purge
from tests.conftest import TEST_DATABASE_URL
from tests.factories import make_org, make_program, make_report, make_user

OLD = time.time() - 2 * 24 * 3600


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(s, "database_url", TEST_DATABASE_URL)
    return s


@pytest.fixture
async def report(db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    report = await make_report(db_session, org, program)
    await db_session.commit()
    return report


def run_purge():
    with psycopg.connect(TEST_DATABASE_URL.replace("+psycopg", ""), autocommit=True) as conn:
        return purge.purge(conn)


def write(report, *parts: str, data: bytes = b"x", age: float | None = OLD):
    path = storage.safe_path(report.org_id, report.id, *parts)
    storage.write_private(path, data)
    if age is not None:
        os.utime(path, (age, age))
    return path


async def add_artifact(db_session, report, path, *, expires_in: timedelta, key=None) -> str:
    artifact_id = new_id()
    db_session.add(
        Artifact(
            id=artifact_id,
            org_id=report.org_id,
            report_id=report.id,
            kind="json",
            storage_key=key or storage.relative_key(path),
            sha256=hashlib.sha256(b"x").hexdigest(),
            size=1,
            expires_at=datetime.now(UTC) + expires_in,
        )
    )
    await db_session.commit()
    return artifact_id


async def artifact_ids(db_session) -> set[str]:
    db_session.expire_all()
    return set((await db_session.scalars(select(Artifact.id))).all())


async def test_purge_removes_expired_files_and_rows(db_session, report):
    engine_dir = "e" * 32
    expired = write(report, engine_dir, "result.json")
    expired_id = await add_artifact(db_session, report, expired, expires_in=timedelta(seconds=-1))
    stats = run_purge()
    assert not expired.exists()
    assert expired_id not in await artifact_ids(db_session)
    assert stats["artifacts"] == 1
    # Unlinking just touched the engine dir, so it is kept today (it may be a running engine's)
    # and removed by a later run once it is a day old.
    engine = expired.parent
    assert engine.is_dir()
    os.utime(engine, (OLD, OLD))
    assert run_purge()["dirs"] == 1
    assert not engine.exists()


async def test_purge_keeps_unexpired(db_session, report):
    kept = write(report, "e" * 32, "result.json")
    kept_id = await add_artifact(db_session, report, kept, expires_in=timedelta(days=1))
    run_purge()
    assert kept.exists()
    assert kept_id in await artifact_ids(db_session)


async def test_orphan_older_than_one_day_removed_fresh_kept(db_session, report):
    old_orphan = write(report, "input", "a" * 64 + ".png")
    fresh_orphan = write(report, "input", "b" * 64 + ".png", age=None)
    stats = run_purge()
    assert not old_orphan.exists()
    assert fresh_orphan.exists()
    assert stats["orphans"] == 1


async def test_fresh_empty_engine_dir_kept(report):
    # A running engine creates its directory first and writes into it afterwards.
    fresh = storage.safe_path(report.org_id, report.id, "f" * 32)
    fresh.mkdir(parents=True)
    old = storage.safe_path(report.org_id, report.id, "d" * 32)
    old.mkdir(parents=True)
    os.utime(old, (OLD, OLD))
    run_purge()
    assert fresh.is_dir()
    assert not old.exists()


@pytest.mark.parametrize("key", ["../outside.json", "/tmp/outside.json", "a/../../outside.json"])
async def test_path_traversal_in_storage_key_ignored(db_session, report, tmp_path, key):
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    if key.startswith("/"):
        key = str(outside)
    artifact_id = await add_artifact(
        db_session, report, None, expires_in=timedelta(seconds=-1), key=key
    )
    run_purge()
    assert outside.exists()  # never touched
    assert artifact_id not in await artifact_ids(db_session)  # expired row still removed


async def test_symlinked_file_is_unlinked_not_followed(db_session, report, tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = storage.safe_path(report.org_id, report.id, "e" * 32, "link.json")
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    os.utime(link, (OLD, OLD), follow_symlinks=False)
    run_purge()
    assert not link.is_symlink()
    assert target.exists()


async def test_expired_idempotency_records_removed(db_session):
    now = datetime.now(UTC)
    for key, expires in (("old", now - timedelta(seconds=1)), ("new", now + timedelta(hours=1))):
        db_session.add(
            IdempotencyRecord(
                user_id=None,
                scope="s",
                key=key,
                request_hash="h",
                status_code=201,
                response={},
                expires_at=expires,
            )
        )
    await db_session.commit()
    stats = run_purge()
    assert stats["idempotency_records"] == 1
    keys = (await db_session.scalars(select(IdempotencyRecord.key))).all()
    assert keys == ["new"]


async def test_purge_is_idempotent(db_session, report):
    path = write(report, "e" * 32, "result.json")
    await add_artifact(db_session, report, path, expires_in=timedelta(seconds=-1))
    run_purge()
    stats = run_purge()
    assert stats == {
        "artifacts": 0,
        "orphans": 0,
        "dirs": 0,
        "idempotency_records": 0,
        "reports_scrubbed": 0,
        "ip_hashes_cleared": 0,
    }
    assert await db_session.scalar(select(func.count()).select_from(Artifact)) == 0


async def test_missing_storage_root_is_fine():
    assert run_purge()["orphans"] == 0


# --- personal data in rows -------------------------------------------------


def days_ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


async def seed_history(db_session, *, age_days: float, retention_days=30, status="completed"):
    """A report with a finished job, its raw result, events, questions and an answer. Returns
    (report_id, job_id): reload() expires the ORM objects."""
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    program.retention_days = retention_days
    author = await make_user(db_session, org, "admin")
    report = await make_report(
        db_session,
        org,
        program,
        title="결제 실패 hong@example.com",
        description="010-1234-5678 로 로그인",
        expected_result="결제 완료",
        test_data="card 4111",
        client_context={"browser": "chrome"},
        client_ip_hash="a" * 64,
        fingerprint="f" * 64,
        created_at=days_ago(age_days),
    )
    job = ValidationJob(
        id=new_id(),
        report_id=report.id,
        org_id=org.id,
        kind="initial",
        status=status,
        verdict="reproduced",
        classification="functional",
        scope_revision=1,
        scope_snapshot={},
        questions=[{"question": "어떤 계정?"}],
        answers="계정은 hong",
    )
    db_session.add(job)
    await db_session.flush()
    report.latest_job_id = job.id
    db_session.add(QaResult(job_id=job.id, raw={"report": {"summary": "hong"}}))
    db_session.add(JobEvent(job_id=job.id, kind="stage", detail={"note": "hong"}))
    db_session.add(
        Clarification(
            id=new_id(),
            report_id=report.id,
            job_id=job.id,
            author_id=author.id,
            answers="hong 계정",
        )
    )
    await db_session.commit()
    return report.id, job.id


async def reload(db_session, model, key):
    db_session.expire_all()
    return await db_session.get(model, key)


async def test_expired_report_text_scrubbed_stats_kept(db_session):
    report_id, job_id = await seed_history(db_session, age_days=31)
    stats = run_purge()
    assert stats["reports_scrubbed"] == 1
    r = await reload(db_session, Report, report_id)
    assert (r.title, r.description, r.expected_result, r.test_data) == ("", "", "", "")
    assert r.client_context is None and r.client_ip_hash is None
    assert r.scrubbed_at is not None
    assert (r.fingerprint, r.workflow_status, r.latest_job_id) == ("f" * 64, "received", job_id)
    j = await reload(db_session, ValidationJob, job_id)
    assert (j.status, j.verdict, j.classification) == ("completed", "reproduced", "functional")
    assert j.questions == [] and j.answers is None
    assert await reload(db_session, QaResult, job_id) is None
    events = (await db_session.scalars(select(JobEvent).where(JobEvent.job_id == job_id))).all()
    assert [(e.kind, e.detail) for e in events] == [("stage", {})]
    answers = await db_session.scalar(
        select(Clarification.answers).where(Clarification.report_id == report_id)
    )
    assert answers == ""
    assert run_purge()["reports_scrubbed"] == 0  # once only


async def test_report_within_retention_untouched(db_session):
    report_id, job_id = await seed_history(db_session, age_days=29)
    assert run_purge()["reports_scrubbed"] == 0
    r = await reload(db_session, Report, report_id)
    assert r.description == "010-1234-5678 로 로그인" and r.scrubbed_at is None
    assert await reload(db_session, QaResult, job_id) is not None


async def test_retention_days_is_per_program(db_session):
    report_id, _ = await seed_history(db_session, age_days=8, retention_days=7)
    assert run_purge()["reports_scrubbed"] == 1
    assert (await reload(db_session, Report, report_id)).description == ""


async def test_report_with_active_job_waits(db_session):
    report_id, _ = await seed_history(db_session, age_days=40, status="running")
    assert run_purge()["reports_scrubbed"] == 0
    assert (await reload(db_session, Report, report_id)).scrubbed_at is None


async def test_scrub_is_batched(db_session, monkeypatch):
    for _ in range(3):
        await seed_history(db_session, age_days=40)
    monkeypatch.setattr(purge, "SCRUB_BATCH", 2)
    assert run_purge()["reports_scrubbed"] == 2
    assert run_purge()["reports_scrubbed"] == 1


async def test_ip_hash_cleared_after_a_day_regardless_of_retention(db_session):
    old_id, _ = await seed_history(db_session, age_days=1.5)
    fresh_id, _ = await seed_history(db_session, age_days=0.5)
    stats = run_purge()
    assert (stats["ip_hashes_cleared"], stats["reports_scrubbed"]) == (1, 0)
    o = await reload(db_session, Report, old_id)
    assert o.client_ip_hash is None and o.description != ""
    assert (await reload(db_session, Report, fresh_id)).client_ip_hash == "a" * 64


async def test_create_job_rereads_scrub_under_row_lock(db_session):
    """A caller holding a stale Report (loaded before the purge committed) is still refused."""
    from app.core.errors import ApiError
    from app.services.jobs import create_job

    report_id, _ = await seed_history(db_session, age_days=40)
    stale = await db_session.get(Report, report_id)
    assert stale.scrubbed_at is None
    await db_session.commit()
    assert run_purge()["reports_scrubbed"] == 1
    with pytest.raises(ApiError) as exc:
        await create_job(db_session, stale, "fresh")
    assert (exc.value.status, exc.value.code) == (410, "REPORT_EXPIRED")
    await db_session.rollback()


async def test_purge_skips_report_locked_by_job_creation(db_session):
    report_id, _ = await seed_history(db_session, age_days=40)
    # Without an IP hash the purge's IP UPDATE does not wait on the locked row.
    await db_session.execute(
        update(Report).where(Report.id == report_id).values(client_ip_hash=None)
    )
    await db_session.commit()
    url = TEST_DATABASE_URL.replace("+psycopg", "")
    with psycopg.connect(url) as other:  # an in-flight create_job: row locked, new job queued
        other.execute("SELECT 1 FROM reports WHERE id = %s FOR UPDATE", (report_id,))
        assert run_purge()["reports_scrubbed"] == 0
        other.execute(
            "INSERT INTO validation_jobs (id, report_id, org_id, kind, status, scope_revision,"
            " scope_snapshot) SELECT %s, id, org_id, 'fresh', 'queued', 1, '{}' FROM reports"
            " WHERE id = %s",
            (new_id(), report_id),
        )
        other.commit()
    assert run_purge()["reports_scrubbed"] == 0
    assert (await reload(db_session, Report, report_id)).scrubbed_at is None
