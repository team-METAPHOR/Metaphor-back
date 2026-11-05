import os
import subprocess
import sys

import psycopg
import pytest
from sqlalchemy import select

from app.models import AuditEvent, Report, ValidationJob
from app.schemas.validation_job import JobBrief
from app.services.reports import report_progress
from app.worker import queue
from app.worker.persist import persist_result
from tests.conftest import TEST_DATABASE_URL
from tests.factories import login_as, make_org, make_program
from tests.test_reports_api import submit


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    await db_session.commit()
    _, admin_h = await login_as(client, db_session, org, "admin")
    _, triager_h = await login_as(client, db_session, org, "developer")
    _, dev_h = await login_as(client, db_session, org, "developer")
    _, viewer_h = await login_as(client, db_session, org, "viewer")
    body = (await submit(client, dev_h, program.id, description="버그")).json()
    return {
        "org": org,
        "program": program,
        "report_id": body["id"],
        "job_id": body["validation_job_id"],
        "admin_h": admin_h,
        "triager_h": triager_h,
        "dev_h": dev_h,
        "viewer_h": viewer_h,
    }


async def patch(client, world, status, headers=None):
    return await client.patch(
        f"/api/v1/reports/{world['report_id']}/workflow",
        json={"status": status},
        headers=headers or world["triager_h"],
    )


async def report_row(db_session, report_id):
    return await db_session.get(Report, report_id, populate_existing=True)


# --- PATCH /reports/{id}/workflow ---------------------------------------------


async def test_developer_moves_to_fixing_and_audits(client, db_session, world):
    response = await patch(client, world, "fixing")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["workflow_status"], body["progress"]) == ("fixing", "fixing")
    assert body["id"] == world["report_id"]
    audit = (
        await db_session.scalars(select(AuditEvent).where(AuditEvent.action == "report.workflow"))
    ).one()
    assert (audit.target, audit.detail) == (
        world["report_id"],
        {"from": "received", "to": "fixing"},
    )
    assert audit.actor_id is not None


async def test_admin_dismisses_then_reopens(client, world):
    assert (await patch(client, world, "dismissed", world["admin_h"])).json()[
        "workflow_status"
    ] == "dismissed"
    reopened = await patch(client, world, "received", world["admin_h"])
    assert (reopened.status_code, reopened.json()["workflow_status"]) == (200, "received")


async def test_same_status_is_noop_200(client, world):
    response = await patch(client, world, "received")
    assert (response.status_code, response.json()["workflow_status"]) == (200, "received")


async def test_viewer_403_stranger_404(client, db_session, world):
    assert (await patch(client, world, "fixing", world["viewer_h"])).status_code == 403
    _, stranger_h = await login_as(client, db_session)
    assert (await patch(client, world, "fixing", stranger_h)).status_code == 404
    other = await make_org(db_session, "Other")
    await db_session.commit()
    _, other_admin_h = await login_as(client, db_session, other, "admin")
    response = await patch(client, world, "fixing", other_admin_h)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "NOT_FOUND")


@pytest.mark.parametrize("status", ["in_review", "accepted", ""])
async def test_unknown_status_422(client, world, status):
    response = await patch(client, world, status)
    assert (response.status_code, response.json()["error"]["code"]) == (422, "VALIDATION_FAILED")


async def test_closed_report_cannot_change_409(client, db_session, world):
    row = await report_row(db_session, world["report_id"])
    row.workflow_status = "closed"
    await db_session.commit()
    for status in ("received", "fixing", "dismissed", "closed"):
        response = await patch(client, world, status)
        assert (response.status_code, response.json()["error"]["code"]) == (409, "REPORT_CLOSED")


async def test_close_without_verified_fix_409(client, world):
    response = await patch(client, world, "closed")
    assert (response.status_code, response.json()["error"]["code"]) == (409, "FIX_NOT_VERIFIED")


async def test_requires_csrf_origin(client, world):
    headers = {"Cookie": world["triager_h"]["Cookie"]}
    assert (await patch(client, world, "fixing", headers)).status_code == 403


# --- fix verified by a passing retest, then manual close --------------------------


def engine_raw(status="completed", verdict="not_reproduced"):
    return {
        "status": status,
        "error_code": None,
        "questions": [],
        "report": {"run": {"verdict": {"value": verdict, "reason_code": "X"}}},
    }


async def running_job(db_session, conn, world, kind):
    """A running job of `kind` (non-initial kinds follow a finished initial job)."""
    initial = await db_session.get(ValidationJob, world["job_id"], populate_existing=True)
    if kind == "initial":
        assert queue.claim_next(conn, "w1", lease_seconds=30) == initial.id
        return initial
    initial.status, initial.verdict = "completed", "reproduced"
    await db_session.flush()
    job = ValidationJob(
        id="a" * 32,
        report_id=initial.report_id,
        org_id=initial.org_id,
        kind=kind,
        status="running",
        scope_revision=1,
        scope_snapshot={},
        parent_job_id=initial.id,
        baseline_job_id=initial.id if kind == "retest" else None,
    )
    db_session.add(job)
    await db_session.flush()
    report = await report_row(db_session, world["report_id"])
    report.latest_job_id = job.id
    report.workflow_status = "fixing"
    await db_session.commit()
    return job


def persist(conn, job, tmp_path, raw):
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir(exist_ok=True)
    return persist_result(
        conn,
        job.id,
        raw,
        org_id=job.org_id,
        report_id=job.report_id,
        engine_dir=engine_dir,
        retention_days=30,
    )


async def test_passing_retest_verifies_fix_but_does_not_close(
    client, db_session, world, conn, tmp_path
):
    job = await running_job(db_session, conn, world, "retest")
    assert persist(conn, job, tmp_path, engine_raw())
    report = await report_row(db_session, world["report_id"])
    assert (report.workflow_status, report.closed_at) == ("fixing", None)
    audits = await db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "report.workflow")
    )
    assert audits.all() == []
    detail = (await client.get(f"/api/v1/reports/{report.id}", headers=world["dev_h"])).json()
    assert (detail["workflow_status"], detail["progress"]) == ("fixing", "verified")
    assert detail["fix_verified"] is True and detail["closed_at"] is None
    (item,) = (await client.get("/api/v1/reports", headers=world["dev_h"])).json()["items"]
    assert (item["fix_verified"], item["progress"]) == (True, "verified")


async def test_close_after_verified_fix(client, db_session, world, conn, tmp_path):
    job = await running_job(db_session, conn, world, "retest")
    assert persist(conn, job, tmp_path, engine_raw())
    response = await patch(client, world, "closed")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["workflow_status"], body["progress"]) == ("closed", "closed")
    assert body["closed_at"] is not None and body["fix_verified"] is True
    audit = (
        await db_session.scalars(select(AuditEvent).where(AuditEvent.action == "report.workflow"))
    ).one()
    assert audit.actor_id is not None
    assert audit.detail == {"from": "fixing", "to": "closed"}
    again = await patch(client, world, "received")
    assert (again.status_code, again.json()["error"]["code"]) == (409, "REPORT_CLOSED")


async def test_fix_verified_follows_latest_finished_job(client, db_session, world, conn, tmp_path):
    """A later finished job that is not a passing retest clears fix_verified; an active job
    after the passing retest does not (it has not finished yet)."""
    job = await running_job(db_session, conn, world, "retest")
    assert persist(conn, job, tmp_path, engine_raw())
    fresh = ValidationJob(
        id="b" * 32,
        report_id=job.report_id,
        org_id=job.org_id,
        kind="fresh",
        status="running",
        scope_revision=1,
        scope_snapshot={},
    )
    db_session.add(fresh)
    report = await report_row(db_session, world["report_id"])
    report.latest_job_id = fresh.id
    await db_session.commit()
    url = f"/api/v1/reports/{report.id}"
    assert (await client.get(url, headers=world["dev_h"])).json()["fix_verified"] is True
    assert persist(conn, fresh, tmp_path, engine_raw("completed", "reproduced"))
    detail = (await client.get(url, headers=world["dev_h"])).json()
    assert (detail["fix_verified"], detail["progress"]) == (False, "fixing")
    response = await patch(client, world, "closed")
    assert (response.status_code, response.json()["error"]["code"]) == (409, "FIX_NOT_VERIFIED")


@pytest.mark.parametrize(
    ("kind", "status", "verdict"),
    [
        ("retest", "completed", "reproduced"),
        ("retest", "inconclusive", None),
        ("retest", "failed", None),
        ("fresh", "completed", "not_reproduced"),
        ("initial", "completed", "not_reproduced"),
    ],
)
async def test_not_verified(client, db_session, world, conn, tmp_path, kind, status, verdict):
    job = await running_job(db_session, conn, world, kind)
    before = (await report_row(db_session, world["report_id"])).workflow_status
    assert persist(conn, job, tmp_path, engine_raw(status, verdict))
    report = await report_row(db_session, world["report_id"])
    assert (report.workflow_status, report.closed_at) == (before, None)
    audits = await db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "report.workflow")
    )
    assert audits.all() == []
    detail = (await client.get(f"/api/v1/reports/{report.id}", headers=world["dev_h"])).json()
    assert detail["fix_verified"] is False


async def test_lost_result_does_not_verify(client, db_session, world, conn, tmp_path):
    """A result that loses the race to a terminal state writes nothing, so nothing closes."""
    job = await running_job(db_session, conn, world, "retest")
    conn.execute("UPDATE validation_jobs SET status = 'cancelled' WHERE id = %s", (job.id,))
    assert not persist(conn, job, tmp_path, engine_raw())
    assert (await report_row(db_session, world["report_id"])).workflow_status == "fixing"
    assert (await patch(client, world, "closed")).status_code == 409


# --- progress ------------------------------------------------------------------


def brief(status, verdict=None):
    return JobBrief(id="j", kind="initial", status=status, verdict=verdict)


@pytest.mark.parametrize(
    ("workflow", "job", "progress"),
    [
        ("closed", brief("completed", "not_reproduced"), "closed"),
        ("closed", None, "closed"),
        ("dismissed", brief("running"), "dismissed"),
        ("fixing", brief("queued"), "fixing"),
        ("fixing", None, "fixing"),
        ("received", None, "new"),
        ("received", brief("queued"), "reproducing"),
        ("received", brief("running"), "reproducing"),
        ("received", brief("cancelling"), "reproducing"),
        ("received", brief("completed", "reproduced"), "reproduced"),
        ("received", brief("completed", "not_reproduced"), "triaged"),
        ("received", brief("inconclusive", "inconclusive"), "triaged"),
        ("received", brief("needs_info"), "triaged"),
        ("received", brief("failed"), "triaged"),
        ("received", brief("cancelled"), "triaged"),
    ],
)
def test_progress_mapping(workflow, job, progress):
    assert report_progress(workflow, job) == progress


@pytest.mark.parametrize(
    ("workflow", "progress"),
    [
        ("closed", "closed"),
        ("dismissed", "dismissed"),
        ("fixing", "verified"),
        ("received", "verified"),
    ],
)
def test_progress_verified(workflow, progress):
    job = JobBrief(id="j", kind="retest", status="completed", verdict="not_reproduced")
    assert report_progress(workflow, job, fix_verified=True) == progress


async def test_list_items_carry_progress(client, world):
    listed = await client.get("/api/v1/reports", headers=world["dev_h"])
    (item,) = listed.json()["items"]
    assert (item["progress"], item["workflow_status"]) == ("reproducing", "received")
    assert item["closed_at"] is None and item["fix_verified"] is False


# --- migration 0007 --------------------------------------------------------------


def test_migration_maps_old_workflow_statuses():
    """0006 rows (in_review / accepted) → upgrade head → received / fixing; closed stays."""
    name = TEST_DATABASE_URL.rsplit("/", 1)[1] + "_workflow"
    admin_url = TEST_DATABASE_URL.replace("+psycopg", "")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')
    url_ = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/" + name
    env = {**os.environ, "DATABASE_URL": url_}

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr

    try:
        alembic("upgrade", "0006")
        with psycopg.connect(url_.replace("+psycopg", ""), autocommit=True) as conn:
            conn.execute("INSERT INTO organizations (id, name) VALUES ('o1', 'Acme')")
            conn.execute("INSERT INTO programs (id, owner_org_id, name) VALUES ('p1', 'o1', 'S')")
            for rid, status in (("r1", "in_review"), ("r2", "accepted"), ("r3", "closed")):
                conn.execute(
                    "INSERT INTO reports (id, program_id, org_id, source, target_url,"
                    " workflow_status) VALUES (%s, 'p1', 'o1', 'widget', 'https://x/', %s)",
                    (rid, status),
                )
        alembic("upgrade", "head")
        with psycopg.connect(url_.replace("+psycopg", ""), autocommit=True) as conn:
            rows = conn.execute(
                "SELECT id, workflow_status, closed_at FROM reports ORDER BY id"
            ).fetchall()
            assert rows == [
                ("r1", "received", None),
                ("r2", "fixing", None),
                ("r3", "closed", None),
            ]
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("UPDATE reports SET workflow_status = 'in_review' WHERE id = 'r1'")
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
