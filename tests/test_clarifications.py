import pytest
from sqlalchemy import select

from app.models import Clarification, ValidationJob
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
    body = (await submit(client, reporter_h, program.id, description="버그 설명")).json()
    return {
        "report_id": body["id"],
        "job_id": body["validation_job_id"],
        "admin_h": admin_h,
        "dev_h": dev_h,
        "reporter_h": reporter_h,
    }


async def set_status(db_session, job_id, status):
    job = await db_session.get(ValidationJob, job_id, populate_existing=True)
    job.status = status
    await db_session.commit()


async def answer(client, world, headers=None, source=None, text="cat은 결과 있음", key=None):
    headers = dict(headers or world["reporter_h"])
    if key:
        headers["Idempotency-Key"] = key
    return await client.post(
        f"/api/v1/reports/{world['report_id']}/clarifications",
        json={"source_job_id": source or world["job_id"], "answers": text},
        headers=headers,
    )


async def test_answer_creates_resume_job_linked(client, db_session, world):
    await set_status(db_session, world["job_id"], "needs_info")
    response = await answer(client, world)
    assert response.status_code == 202, response.text
    new_id = response.json()["validation_job_id"]
    assert response.json()["status"] == "queued"
    new = await db_session.get(ValidationJob, new_id)
    assert (new.kind, new.parent_job_id, new.answers) == (
        "resume",
        world["job_id"],
        "cat은 결과 있음",
    )
    old = await db_session.get(ValidationJob, world["job_id"], populate_existing=True)
    assert old.status == "needs_info"
    row = await db_session.scalar(select(Clarification))
    assert (row.job_id, row.created_job_id) == (world["job_id"], new_id)


async def test_org_reviewer_can_answer_developer_cannot(client, db_session, world):
    await set_status(db_session, world["job_id"], "needs_info")
    denied = await answer(client, world, world["dev_h"])
    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "FORBIDDEN")
    ok = await answer(client, world, world["admin_h"])
    assert ok.status_code == 202


async def test_answer_on_non_needs_info_409(client, db_session, world):
    await set_status(db_session, world["job_id"], "completed")
    response = await answer(client, world)
    assert (response.status_code, response.json()["error"]["code"]) == (409, "NOT_AWAITING_ANSWER")


async def test_second_answer_same_source_409(client, db_session, world):
    await set_status(db_session, world["job_id"], "needs_info")
    assert (await answer(client, world)).status_code == 202
    again = await answer(client, world, text="다른 답")
    assert again.status_code == 409
    assert again.json()["error"]["code"] in ("ALREADY_ANSWERED", "NOT_AWAITING_ANSWER")


async def test_idempotent_answer_replays(client, db_session, world):
    await set_status(db_session, world["job_id"], "needs_info")
    first = await answer(client, world, key="a1")
    second = await answer(client, world, key="a1")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()


async def test_blank_answer_422(client, db_session, world):
    await set_status(db_session, world["job_id"], "needs_info")
    response = await answer(client, world, text="   ")
    assert (response.status_code, response.json()["error"]["code"]) == (422, "ANSWER_REQUIRED")


async def test_cumulative_length_over_8000_422(client, db_session, world):
    await set_status(db_session, world["job_id"], "needs_info")
    first = await answer(client, world, text="a" * 4000)
    assert first.status_code == 202
    second_source = first.json()["validation_job_id"]
    await set_status(db_session, second_source, "needs_info")
    too_long = await answer(client, world, source=second_source, text="b" * 3990)
    assert (too_long.status_code, too_long.json()["error"]["code"]) == (422, "ANSWER_TOO_LONG")


async def test_answer_redacted(client, db_session, world):
    await set_status(db_session, world["job_id"], "needs_info")
    response = await answer(client, world, text="password=hunter2 로그인")
    new = await db_session.get(ValidationJob, response.json()["validation_job_id"])
    assert "hunter2" not in new.answers


async def test_stranger_404(client, db_session, world):
    await set_status(db_session, world["job_id"], "needs_info")
    _, stranger_h = await login_as(client, db_session)
    response = await answer(client, world, stranger_h)
    assert response.status_code == 404
