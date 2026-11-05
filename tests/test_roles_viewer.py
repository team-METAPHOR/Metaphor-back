"""Role matrix: admin / developer / viewer.

viewer reads everything in its org but every write is 403; developer handles reports and jobs
but not org/program/widget administration.
"""

import pytest
from sqlalchemy import select

from app.models import Membership, QaResult, ValidationJob
from app.services.access import ADMIN_ROLES, WRITE_ROLES
from tests.factories import login_as, make_org, make_program
from tests.test_core_intake import png
from tests.test_reports_api import shot, submit


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    await db_session.commit()
    _, admin_h = await login_as(client, db_session, org, "admin")
    dev, dev_h = await login_as(client, db_session, org, "developer")
    viewer, viewer_h = await login_as(client, db_session, org, "viewer")
    body = (await submit(client, dev_h, program.id, description="버그", files=shot())).json()
    job = await db_session.get(ValidationJob, body["validation_job_id"])
    job.status, job.verdict, job.engine_job_id = "completed", "reproduced", "e" * 32
    db_session.add(QaResult(job_id=job.id, raw={"status": "completed"}))
    await db_session.commit()
    detail = (await client.get(f"/api/v1/reports/{body['id']}", headers=dev_h)).json()
    return {
        "org": org,
        "program": program,
        "dev": dev,
        "viewer": viewer,
        "report_id": body["id"],
        "number": detail["number"],
        "artifact_id": detail["input_artifact_id"],
        "job_id": job.id,
        "admin_h": admin_h,
        "dev_h": dev_h,
        "viewer_h": viewer_h,
    }


def test_role_tuples_are_centralized():
    assert WRITE_ROLES == ("admin", "developer")
    assert ADMIN_ROLES == ("admin",)


def read_urls(w) -> list[str]:
    org, program, report, job = w["org"].id, w["program"].id, w["report_id"], w["job_id"]
    return [
        "/api/v1/reports",
        f"/api/v1/reports/{report}",
        f"/api/v1/orgs/{org}/reports/{w['number']}",
        f"/api/v1/reports/{report}/developer-handoff",
        f"/api/v1/validation-jobs/{job}",
        f"/api/v1/validation-jobs/{job}/result",
        f"/api/v1/validation-jobs/{job}/events",
        f"/api/v1/validation-jobs/{job}/artifacts",
        f"/api/v1/evidence/{w['artifact_id']}/download",
        f"/api/v1/orgs/{org}",
        f"/api/v1/orgs/{org}/members",
        "/api/v1/programs",
        f"/api/v1/programs/{program}",
        f"/api/v1/programs/{program}/widget-settings",
    ]


async def test_viewer_reads_everything_in_org(client, world):
    for url in read_urls(world):
        response = await client.get(url, headers=world["viewer_h"])
        assert response.status_code == 200, (url, response.text)


def write_requests(w) -> list[tuple[str, str, dict]]:
    org, program, report, job = w["org"].id, w["program"].id, w["report_id"], w["job_id"]
    return [
        ("PATCH", f"/api/v1/reports/{report}/workflow", {"json": {"status": "fixing"}}),
        ("PATCH", f"/api/v1/reports/{report}/assignee", {"json": {"user_id": w["dev"].id}}),
        ("POST", f"/api/v1/reports/{report}/validation-jobs", {"json": {"mode": "fresh"}}),
        (
            "POST",
            f"/api/v1/reports/{report}/clarifications",
            {"json": {"source_job_id": job, "answers": "답변"}},
        ),
        ("POST", f"/api/v1/validation-jobs/{job}/cancel", {}),
        (
            "POST",
            "/api/v1/programs",
            {
                "json": {
                    "org_id": org,
                    "name": "New",
                    "base_url": "https://staging.example.com",
                    "allowed_path_prefixes": ["/"],
                    "test_context_id": "ctx",
                }
            },
        ),
        ("PATCH", f"/api/v1/programs/{program}", {"json": {"name": "Renamed"}}),
        ("GET", f"/api/v1/programs/{program}/origin-verification", {}),
        ("POST", f"/api/v1/programs/{program}/origin-verification/check", {}),
        (
            "PATCH",
            f"/api/v1/programs/{program}/widget-settings",
            {"json": {"widget_enabled": True}},
        ),
        ("POST", f"/api/v1/programs/{program}/widget-settings/rotate-key", {}),
        ("PATCH", f"/api/v1/orgs/{org}", {"json": {"name": "Renamed"}}),
        ("PATCH", f"/api/v1/orgs/{org}/members/{w['dev'].id}", {"json": {"role": "viewer"}}),
        ("DELETE", f"/api/v1/orgs/{org}/members/{w['dev'].id}", {}),
        ("POST", f"/api/v1/orgs/{org}/invites", {"json": {"role": "viewer"}}),
        ("GET", f"/api/v1/orgs/{org}/invites", {}),
        ("DELETE", f"/api/v1/orgs/{org}/invites/{'0' * 32}", {}),
    ]


async def test_viewer_every_write_403(client, world):
    for method, url, kwargs in write_requests(world):
        response = await client.request(method, url, headers=world["viewer_h"], **kwargs)
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_viewer_cannot_file_report(client, db_session, world):
    response = await submit(client, world["viewer_h"], world["program"].id, files=shot(png()))
    assert (response.status_code, response.json()["error"]["code"]) == (403, "FORBIDDEN")


async def test_viewer_may_leave_org(client, db_session, world):
    url = f"/api/v1/orgs/{world['org'].id}/members/{world['viewer'].id}"
    assert (await client.delete(url, headers=world["viewer_h"])).status_code == 204


async def test_viewer_may_be_assignee(client, world):
    response = await client.patch(
        f"/api/v1/reports/{world['report_id']}/assignee",
        json={"user_id": world["viewer"].id},
        headers=world["dev_h"],
    )
    assert response.status_code == 200, response.text


async def test_developer_writes_reports_and_jobs(client, db_session, world):
    h = world["dev_h"]
    report = world["report_id"]
    workflow = await client.patch(
        f"/api/v1/reports/{report}/workflow", json={"status": "fixing"}, headers=h
    )
    assert workflow.status_code == 200, workflow.text
    assignee = await client.patch(
        f"/api/v1/reports/{report}/assignee", json={"user_id": world["dev"].id}, headers=h
    )
    assert assignee.status_code == 200, assignee.text
    rerun = await client.post(
        f"/api/v1/reports/{report}/validation-jobs", json={"mode": "fresh"}, headers=h
    )
    assert rerun.status_code == 202, rerun.text
    new_job = rerun.json()["validation_job_id"]
    cancel = await client.post(f"/api/v1/validation-jobs/{new_job}/cancel", headers=h)
    assert cancel.status_code == 202, cancel.text


async def test_developer_answers_clarification(client, db_session, world):
    job = await db_session.get(ValidationJob, world["job_id"], populate_existing=True)
    job.status, job.verdict = "needs_info", None
    await db_session.commit()
    response = await client.post(
        f"/api/v1/reports/{world['report_id']}/clarifications",
        json={"source_job_id": world["job_id"], "answers": "답변"},
        headers=world["dev_h"],
    )
    assert response.status_code == 202, response.text


async def test_developer_admin_actions_403(client, world):
    org, program = world["org"].id, world["program"].id
    admin_only = [
        ("PATCH", f"/api/v1/programs/{program}", {"json": {"name": "Renamed"}}),
        (
            "PATCH",
            f"/api/v1/programs/{program}/widget-settings",
            {"json": {"widget_enabled": True}},
        ),
        ("POST", f"/api/v1/programs/{program}/widget-settings/rotate-key", {}),
        ("GET", f"/api/v1/programs/{program}/origin-verification", {}),
        ("PATCH", f"/api/v1/orgs/{org}", {"json": {"name": "Renamed"}}),
        ("POST", f"/api/v1/orgs/{org}/invites", {"json": {"role": "viewer"}}),
    ]
    for method, url, kwargs in admin_only:
        response = await client.request(method, url, headers=world["dev_h"], **kwargs)
        assert response.status_code == 403, (method, url, response.text)


async def test_admin_invites_and_promotes_viewer(client, db_session, world):
    org = world["org"].id
    invite = await client.post(
        f"/api/v1/orgs/{org}/invites", json={"role": "viewer"}, headers=world["admin_h"]
    )
    assert (invite.status_code, invite.json()["role"]) == (201, "viewer")
    promote = await client.patch(
        f"/api/v1/orgs/{org}/members/{world['viewer'].id}",
        json={"role": "developer"},
        headers=world["admin_h"],
    )
    assert promote.status_code == 200, promote.text
    membership = await db_session.scalar(
        select(Membership).where(Membership.user_id == world["viewer"].id)
    )
    await db_session.refresh(membership)
    assert membership.role == "developer"


async def test_reviewer_role_rejected(client, world):
    response = await client.post(
        f"/api/v1/orgs/{world['org'].id}/invites",
        json={"role": "reviewer"},
        headers=world["admin_h"],
    )
    assert response.status_code == 422
