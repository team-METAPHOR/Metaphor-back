import pytest
from sqlalchemy import select

from app.models import AuditEvent, ValidationJob
from app.worker import queue
from tests.factories import login_as, make_org, make_program
from tests.test_reports_api import submit


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    await db_session.commit()
    _, admin_h = await login_as(client, db_session, org, "admin")
    _, dev_h = await login_as(client, db_session, org, "developer")
    _, viewer_h = await login_as(client, db_session, org, "viewer")
    body = (await submit(client, dev_h, program.id, description="버그")).json()
    return {
        "job_id": body["validation_job_id"],
        "admin_h": admin_h,
        "dev_h": dev_h,
        "viewer_h": viewer_h,
    }


async def cancel(client, world, headers=None):
    return await client.post(
        f"/api/v1/validation-jobs/{world['job_id']}/cancel", headers=headers or world["admin_h"]
    )


async def job(db_session, world):
    return await db_session.get(ValidationJob, world["job_id"], populate_existing=True)


async def test_cancel_queued_is_final_and_never_claimed(client, db_session, world, conn):
    response = await cancel(client, world)
    assert response.status_code == 202
    assert response.json() == {"id": world["job_id"], "status": "cancelled"}
    row = await job(db_session, world)
    assert row.finished_at is not None and row.cancel_requested_at is not None
    assert queue.claim_next(conn, "w1", lease_seconds=30) is None
    audit = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "job.cancel"))
    assert audit.target == world["job_id"]


async def test_cancel_running_marks_cancelling_and_is_idempotent(client, db_session, world, conn):
    assert queue.claim_next(conn, "w1", lease_seconds=30) == world["job_id"]
    first = await cancel(client, world)
    second = await cancel(client, world)
    assert first.json()["status"] == second.json()["status"] == "cancelling"
    row = await job(db_session, world)
    assert row.status == "cancelling" and row.cancel_requested_at is not None


async def test_cancel_finished_409(client, db_session, world):
    row = await job(db_session, world)
    row.status = "completed"
    await db_session.commit()
    response = await cancel(client, world)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "JOB_ALREADY_FINISHED"
    assert response.json()["error"]["detail"] == {"status": "completed"}


async def test_viewer_403_stranger_404(client, db_session, world):
    assert (await cancel(client, world, world["viewer_h"])).status_code == 403
    _, stranger_h = await login_as(client, db_session)
    assert (await cancel(client, world, stranger_h)).status_code == 404


async def test_cancel_vs_worker_result_keeps_result(client, db_session, world, conn, tmp_path):
    """Cancel arrives while running; the engine finishes first → result wins."""
    from app.worker.persist import persist_result

    queue.claim_next(conn, "w1", lease_seconds=30)
    await cancel(client, world)
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    row = await job(db_session, world)
    raw = {
        "status": "completed",
        "error_code": None,
        "questions": [],
        "report": {"run": {"verdict": {"value": "reproduced", "reason_code": "X"}}},
    }
    assert persist_result(
        conn,
        row.id,
        raw,
        org_id=row.org_id,
        report_id=row.report_id,
        engine_dir=engine_dir,
        retention_days=30,
    )
    row = await job(db_session, world)
    assert (row.status, row.verdict) == ("completed", "reproduced")
