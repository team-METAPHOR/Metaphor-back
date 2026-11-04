import asyncio

import pytest
from sqlalchemy import select

from app.models import ProgramScopeRevision, QaResult, Report, ValidationJob
from app.worker import queue, runner
from tests.factories import login_as, make_org, make_program
from tests.fakes import ScriptedModel
from tests.test_reports_api import submit


@pytest.fixture(autouse=True)
def _worker(worker_env):
    return worker_env


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org, approved_app_versions=["build-2"])
    program.submission_mode = "open_authenticated"
    await db_session.commit()
    _, admin_h = await login_as(client, db_session, org, "admin")
    _, dev_h = await login_as(client, db_session, org, "developer")
    _, reporter_h = await login_as(client, db_session)
    body = (await submit(client, reporter_h, program.id, description="버그")).json()
    return {
        "program": program,
        "report_id": body["id"],
        "job_id": body["validation_job_id"],
        "admin_h": admin_h,
        "dev_h": dev_h,
        "reporter_h": reporter_h,
    }


async def finish(db_session, job_id, verdict="reproduced", engine_id="e" * 32):
    job = await db_session.get(ValidationJob, job_id, populate_existing=True)
    job.status, job.verdict, job.engine_job_id = "completed", verdict, engine_id
    db_session.add(QaResult(job_id=job_id, raw={"status": "completed"}))
    await db_session.commit()


async def request(client, world, body, headers=None):
    return await client.post(
        f"/api/v1/reports/{world['report_id']}/validation-jobs",
        json=body,
        headers=headers or world["admin_h"],
    )


async def test_fresh_creates_job(client, db_session, world):
    await finish(db_session, world["job_id"])
    response = await request(client, world, {"mode": "fresh"})
    assert response.status_code == 202, response.text
    job = await db_session.get(ValidationJob, response.json()["validation_job_id"])
    assert (job.kind, job.baseline_job_id) == ("fresh", None)


async def test_fresh_while_active_409(client, world):
    response = await request(client, world, {"mode": "fresh"})
    assert (response.status_code, response.json()["error"]["code"]) == (409, "ACTIVE_JOB_EXISTS")


async def test_retest_copies_baseline_snapshot_with_new_version(client, db_session, world):
    await finish(db_session, world["job_id"])
    baseline = await db_session.get(ValidationJob, world["job_id"])
    body = {"mode": "retest", "baseline_job_id": world["job_id"], "app_version": "build-2"}
    response = await request(client, world, body)
    assert response.status_code == 202, response.text
    job = await db_session.get(ValidationJob, response.json()["validation_job_id"])
    assert (job.kind, job.baseline_job_id, job.app_version) == ("retest", baseline.id, "build-2")
    expected = {**baseline.scope_snapshot, "app_version": "build-2"}
    assert job.scope_snapshot == expected


@pytest.mark.parametrize("verdict", ["inconclusive", None])
async def test_retest_requires_comparable_baseline(client, db_session, world, verdict):
    await finish(db_session, world["job_id"], verdict=verdict)
    body = {"mode": "retest", "baseline_job_id": world["job_id"], "app_version": "build-2"}
    response = await request(client, world, body)
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "BASELINE_NOT_COMPARABLE",
    )


async def test_retest_unapproved_version_422(client, db_session, world):
    await finish(db_session, world["job_id"])
    body = {"mode": "retest", "baseline_job_id": world["job_id"], "app_version": "build-9"}
    response = await request(client, world, body)
    assert (response.status_code, response.json()["error"]["code"]) == (
        422,
        "APP_VERSION_NOT_APPROVED",
    )


async def test_retest_after_scope_change_409(client, db_session, world):
    await finish(db_session, world["job_id"])
    program = world["program"]
    await db_session.refresh(program)
    old = await db_session.get(ProgramScopeRevision, (program.id, 1))
    db_session.add(
        ProgramScopeRevision(
            program_id=program.id,
            revision=2,
            base_url=old.base_url,
            allowed_path_prefixes=["/search"],
            approved_app_versions=["build-2"],
            test_context_id=old.test_context_id,
            reset_adapter=old.reset_adapter,
        )
    )
    program.current_revision = 2
    await db_session.commit()
    body = {"mode": "retest", "baseline_job_id": world["job_id"], "app_version": "build-2"}
    response = await request(client, world, body)
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "SCOPE_CHANGED_USE_FRESH",
    )


async def test_baseline_from_other_report_404(client, db_session, world):
    other = (await submit(client, world["reporter_h"], world["program"].id, description="x")).json()
    await finish(db_session, other["validation_job_id"])
    body = {
        "mode": "retest",
        "baseline_job_id": other["validation_job_id"],
        "app_version": "build-2",
    }
    response = await request(client, world, body)
    assert response.status_code == 404


@pytest.mark.parametrize(("who", "status"), [("reporter_h", 403), ("dev_h", 403)])
async def test_only_reviewer_or_admin(client, db_session, world, who, status):
    await finish(db_session, world["job_id"])
    response = await request(client, world, {"mode": "fresh"}, world[who])
    assert response.status_code == status


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "retest"},
        {"mode": "retest", "baseline_job_id": "x"},
        {"mode": "fresh", "steps": []},
        {"mode": "replan"},
    ],
)
async def test_request_body_validation_422(client, db_session, world, body):
    await finish(db_session, world["job_id"])
    response = await request(client, world, body)
    assert response.status_code == 422


async def test_retest_does_not_touch_workflow_status(client, db_session, world):
    await finish(db_session, world["job_id"])
    body = {"mode": "retest", "baseline_job_id": world["job_id"], "app_version": "build-2"}
    await request(client, world, body)
    report = await db_session.get(Report, world["report_id"], populate_existing=True)
    assert report.workflow_status == "received"


# --- worker side -----------------------------------------------------------


async def test_worker_retest_missing_baseline_files_fails(client, db_session, world, conn):
    await finish(db_session, world["job_id"])
    body = {"mode": "retest", "baseline_job_id": world["job_id"], "app_version": "build-2"}
    job_id = (await request(client, world, body)).json()["validation_job_id"]
    assert queue.claim_next(conn, "w1", lease_seconds=30) == job_id
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    job = await db_session.get(ValidationJob, job_id, populate_existing=True)
    assert (job.status, job.backend_error_code) == ("failed", "BASELINE_ARTIFACTS_MISSING")


async def test_worker_retest_calls_engine_with_baseline(
    client, db_session, world, conn, monkeypatch
):
    from app.integrations import storage
    from tests.fakes import fake_run

    await finish(db_session, world["job_id"])
    report = await db_session.get(Report, world["report_id"])
    baseline_dir = storage.safe_path(report.org_id, report.id, "e" * 32)
    baseline_dir.mkdir(parents=True)
    for name in runner.BASELINE_FILES:
        (baseline_dir / name).write_text("{}")
    body = {"mode": "retest", "baseline_job_id": world["job_id"], "app_version": "build-2"}
    job_id = (await request(client, world, body)).json()["validation_job_id"]
    queue.claim_next(conn, "w1", lease_seconds=30)
    seen = {}
    run = fake_run()

    async def retest(self, baseline_engine_id, **kwargs):
        seen["baseline"] = baseline_engine_id
        seen["app_version"] = self.options.app_version
        return await run(self)

    monkeypatch.setattr(runner.QAService, "retest", retest)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    assert seen == {"baseline": "e" * 32, "app_version": "build-2"}
    job = await db_session.get(ValidationJob, job_id, populate_existing=True)
    assert job.status == "completed"
    assert (await db_session.scalars(select(ValidationJob))).all()
