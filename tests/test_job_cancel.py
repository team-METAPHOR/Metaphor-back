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
    program.submission_mode = "open_authenticated"
    await db_session.commit()
    _, admin_h = await login_as(client, db_session, org, "admin")
    _, dev_h = await login_as(client, db_session, org, "developer")
    _, reporter_h = await login_as(client, db_session)
    body = (await submit(client, reporter_h, program.id, description="버그")).json()
    return {
        "job_id": body["validation_job_id"],
        "admin_h": admin_h,
        "dev_h": dev_h,
        "reporter_h": reporter_h,
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


async def test_reporter_can_cancel_own_initial_job(client, db_session, world):
    response = await cancel(client, world, world["reporter_h"])
    assert response.status_code == 202


async def test_reporter_cannot_cancel_org_requested_job(client, db_session, world):
    row = await job(db_session, world)
    row.kind = "retest"
    await db_session.commit()
    response = await cancel(client, world, world["reporter_h"])
    assert response.status_code == 403


async def test_developer_403_stranger_404(client, db_session, world):
    assert (await cancel(client, world, world["dev_h"])).status_code == 403
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


async def test_reporter_cannot_cancel_resume_started_by_org(client, db_session, world):
    row = await job(db_session, world)
    row.status = "needs_info"
    await db_session.commit()
    answered = await client.post(
        f"/api/v1/reports/{row.report_id}/clarifications",
        json={"source_job_id": row.id, "answers": "관리자 답변"},
        headers=world["admin_h"],
    )
    resume_id = answered.json()["validation_job_id"]
    response = await client.post(
        f"/api/v1/validation-jobs/{resume_id}/cancel", headers=world["reporter_h"]
    )
    assert response.status_code == 403


async def test_reporter_can_cancel_own_resume(client, db_session, world):
    row = await job(db_session, world)
    row.status = "needs_info"
    await db_session.commit()
    answered = await client.post(
        f"/api/v1/reports/{row.report_id}/clarifications",
        json={"source_job_id": row.id, "answers": "제보자 답변"},
        headers=world["reporter_h"],
    )
    resume_id = answered.json()["validation_job_id"]
    response = await client.post(
        f"/api/v1/validation-jobs/{resume_id}/cancel", headers=world["reporter_h"]
    )
    assert response.status_code == 202
