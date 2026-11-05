"""Reports over the daily quota are stored but parked (no job) until someone reruns them."""

import pytest
from sqlalchemy import func, select, update

from app.models import AuditEvent, Program, Report, ValidationJob
from app.schemas.validation_job import JobBrief
from app.services.reports import report_progress
from tests.factories import login_as
from tests.test_reports_api import submit
from tests.test_widget_public import STAGING, ip_secret, payload, post  # noqa: F401
from tests.test_widget_public import world as widget_world  # noqa: F401


@pytest.fixture
async def world(widget_world, client, db_session):  # noqa: F811
    _, dev_h = await login_as(client, db_session, widget_world["org"], "developer")
    return {**widget_world, "dev_h": dev_h}


async def set_limit(db_session, world, limit):
    await db_session.execute(
        update(Program).where(Program.id == world["program"].id).values(daily_job_limit=limit)
    )
    await db_session.commit()


async def parked_audits(db_session):
    return (
        await db_session.scalars(select(AuditEvent).where(AuditEvent.action == "report.parked"))
    ).all()


async def job_count(db_session, report_id):
    return await db_session.scalar(
        select(func.count()).select_from(ValidationJob).where(ValidationJob.report_id == report_id)
    )


async def test_platform_report_over_daily_limit_is_parked(client, db_session, world):
    await set_limit(db_session, world, 1)
    first = await submit(client, world["dev_h"], world["program"].id, description="a")
    assert first.status_code == 201 and first.json()["validation_job_id"]
    response = await submit(client, world["dev_h"], world["program"].id, description="b")
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["validation_job_id"], body["status"]) == (None, "waiting_quota")
    report = await db_session.get(Report, body["id"])
    assert (report.run_blocked, report.latest_job_id) == ("quota", None)
    assert await job_count(db_session, report.id) == 0
    (audit,) = await parked_audits(db_session)
    assert (audit.target, audit.actor_id) == (report.id, report.author_id)
    assert audit.detail == {"reason": "quota", "code": "DAILY_LIMIT_EXCEEDED"}
    detail = await client.get(f"/api/v1/reports/{report.id}", headers=world["dev_h"])
    assert (detail.json()["progress"], detail.json()["latest_job"]) == ("waiting_quota", None)
    listed = await client.get("/api/v1/reports", headers=world["dev_h"])
    progress = {item["id"]: item["progress"] for item in listed.json()["items"]}
    assert progress[report.id] == "waiting_quota"


async def test_widget_over_daily_cap_is_parked_and_does_not_leak(client, db_session, world):
    await set_limit(db_session, world, 5)  # widget cap max(1, 5 // 2) == 2
    for i in range(2):
        assert (await post(client, world["key"], payload(title=f"t{i}"))).status_code == 201
    response = await post(client, world["key"], payload(title="third"))
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"receipt_id", "status", "created_at"}
    assert body["status"] == "received" and body["receipt_id"]
    assert "quota" not in response.text
    report = await db_session.scalar(
        select(Report).where(Report.public_receipt_id == body["receipt_id"])
    )
    assert (report.run_blocked, report.latest_job_id) == ("quota", None)
    (audit,) = await parked_audits(db_session)
    assert audit.actor_id is None
    assert audit.detail == {"reason": "quota", "code": "WIDGET_DAILY_LIMITED"}


async def test_widget_over_program_job_limit_is_parked(client, db_session, world):
    await set_limit(db_session, world, 4)  # widget cap 2; three platform jobs leave one slot
    for i in range(3):
        assert (
            await submit(client, world["dev_h"], world["program"].id, description=f"{i}")
        ).json()["validation_job_id"]
    assert (await post(client, world["key"], payload())).status_code == 201
    response = await post(client, world["key"], payload(title="second"))
    assert (response.status_code, response.json()["status"]) == (201, "received")
    (audit,) = await parked_audits(db_session)
    assert audit.detail == {"reason": "quota", "code": "DAILY_LIMIT_EXCEEDED"}


async def test_hourly_abuse_limit_still_rejects(client, db_session, world):
    await set_limit(db_session, world, 1000)
    for i in range(10):
        assert (await post(client, world["key"], payload(title=f"t{i}"))).status_code == 201
    response = await post(client, world["key"], payload())
    assert (response.status_code, response.json()["error"]["code"]) == (
        429,
        "WIDGET_IP_RATE_LIMITED",
    )
    assert await parked_audits(db_session) == []


async def test_rerun_starts_parked_report_and_clears_block(client, db_session, world):
    await set_limit(db_session, world, 1)
    await submit(client, world["dev_h"], world["program"].id, description="a")
    report_id = (await submit(client, world["dev_h"], world["program"].id, description="b")).json()[
        "id"
    ]
    url = f"/api/v1/reports/{report_id}/validation-jobs"
    still = await client.post(url, json={"mode": "fresh"}, headers=world["admin_h"])
    assert (still.status_code, still.json()["error"]["code"]) == (429, "DAILY_LIMIT_EXCEEDED")
    report = await db_session.get(Report, report_id, populate_existing=True)
    assert report.run_blocked == "quota"

    await set_limit(db_session, world, 10)
    response = await client.post(url, json={"mode": "fresh"}, headers=world["admin_h"])
    assert response.status_code == 202, response.text
    report = await db_session.get(Report, report_id, populate_existing=True)
    assert (report.run_blocked, report.latest_job_id) == (
        None,
        response.json()["validation_job_id"],
    )
    detail = await client.get(f"/api/v1/reports/{report_id}", headers=world["dev_h"])
    assert detail.json()["progress"] == "reproducing"


@pytest.mark.parametrize(
    ("workflow", "progress"),
    [("received", "waiting_quota"), ("fixing", "fixing"), ("dismissed", "dismissed")],
)
def test_progress_waiting_quota(workflow, progress):
    assert report_progress(workflow, None, run_blocked="quota") == progress


def test_progress_waiting_quota_ignored_once_a_job_exists():
    job = JobBrief(id="j", kind="fresh", status="queued", verdict=None)
    assert report_progress("received", job, run_blocked=None) == "reproducing"
