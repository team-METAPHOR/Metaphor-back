import json

import pytest
from sqlalchemy import select

from app.models import AuditEvent, ReportEvalTag, ValidationJob
from tests.factories import login_as, make_org, make_program
from tests.test_reports_api import submit

TAG = {"eval_run_id": "buglab-qa-v1.run-1", "case_id": "B02", "repeat_id": "r1"}


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    await db_session.commit()
    _, dev_h = await login_as(client, db_session, org, "developer")
    _, admin_h = await login_as(client, db_session, org, "admin")
    _, viewer_h = await login_as(client, db_session, org, "viewer")
    body = (await submit(client, dev_h, program.id, description="장바구니 수량이 안 바뀜")).json()
    return {
        "org": org,
        "program": program,
        "report_id": body["id"],
        "job_id": body["validation_job_id"],
        "dev_h": dev_h,
        "admin_h": admin_h,
        "viewer_h": viewer_h,
    }


def tag_url(report_id):
    return f"/api/v1/reports/{report_id}/eval-tag"


async def test_developer_tags_report_and_reads_it_back(client, db_session, world):
    response = await client.put(tag_url(world["report_id"]), json=TAG, headers=world["dev_h"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert {k: body[k] for k in TAG} == TAG
    assert body["report_id"] == world["report_id"]

    read = await client.get(tag_url(world["report_id"]), headers=world["viewer_h"])
    assert (read.status_code, read.json()["case_id"]) == (200, "B02")

    (audit,) = (
        await db_session.scalars(select(AuditEvent).where(AuditEvent.action == "report.eval_tag"))
    ).all()
    assert audit.target == world["report_id"]
    assert audit.detail == TAG


async def test_retag_replaces_and_untagged_is_404(client, world):
    missing = await client.get(tag_url(world["report_id"]), headers=world["dev_h"])
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "NOT_FOUND")
    await client.put(tag_url(world["report_id"]), json=TAG, headers=world["dev_h"])
    again = await client.put(
        tag_url(world["report_id"]), json={**TAG, "repeat_id": "r2"}, headers=world["admin_h"]
    )
    assert (again.status_code, again.json()["repeat_id"]) == (200, "r2")


async def test_viewer_cannot_tag(client, world):
    response = await client.put(tag_url(world["report_id"]), json=TAG, headers=world["viewer_h"])
    assert response.status_code == 403


async def test_other_org_gets_404(client, db_session, world):
    other = await make_org(db_session, "Other")
    await db_session.commit()
    _, outsider_h = await login_as(client, db_session, other, "admin")
    put = await client.put(tag_url(world["report_id"]), json=TAG, headers=outsider_h)
    get = await client.get(tag_url(world["report_id"]), headers=outsider_h)
    assert (put.status_code, get.status_code) == (404, 404)
    unknown = await client.put(tag_url("0" * 32), json=TAG, headers=world["dev_h"])
    assert unknown.status_code == 404


async def test_same_slot_on_two_reports_conflicts(client, world):
    second = (await submit(client, world["dev_h"], world["program"].id, description="다른")).json()
    await client.put(tag_url(world["report_id"]), json=TAG, headers=world["dev_h"])
    clash = await client.put(tag_url(second["id"]), json=TAG, headers=world["dev_h"])
    assert (clash.status_code, clash.json()["error"]["code"]) == (409, "EVAL_SLOT_TAKEN")
    other_repeat = await client.put(
        tag_url(second["id"]), json={**TAG, "repeat_id": "r2"}, headers=world["dev_h"]
    )
    assert other_repeat.status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        {**TAG, "case_id": "정답: 수량 2"},  # free text could carry an answer key
        {**TAG, "case_id": ""},
        {**TAG, "repeat_id": "x" * 65},
        {**TAG, "extra": "nope"},
        {"eval_run_id": "run", "case_id": "B02"},
    ],
)
async def test_invalid_ids_rejected_without_echo(client, world, bad):
    response = await client.put(tag_url(world["report_id"]), json=bad, headers=world["dev_h"])
    assert response.status_code == 422
    assert "정답" not in response.text


async def test_tag_never_reaches_engine_input_or_report_views(client, conn, world):
    """The worker loads its engine input without the tag, and report views don't show it."""
    from app.worker import runner

    await client.put(tag_url(world["report_id"]), json=TAG, headers=world["dev_h"])
    conn.execute("UPDATE validation_jobs SET status = 'running' WHERE id = %s", (world["job_id"],))
    data = runner._load(conn, world["job_id"])
    dumped = json.dumps(data, default=str)
    for value in TAG.values():
        assert value not in dumped
    for path in (
        f"/api/v1/reports/{world['report_id']}",
        f"/api/v1/reports/{world['report_id']}/developer-handoff",
        f"/api/v1/validation-jobs/{world['job_id']}",
        "/api/v1/reports",
    ):
        response = await client.get(path, headers=world["dev_h"])
        assert response.status_code == 200, (path, response.text)
        assert "buglab-qa-v1" not in response.text, path


async def test_tag_removed_with_report_cascade(db_session, world):
    from app.models import Report

    report = await db_session.get(Report, world["report_id"])
    db_session.add(ReportEvalTag(report_id=report.id, org_id=report.org_id, **TAG))
    await db_session.commit()
    await db_session.execute(
        ValidationJob.__table__.delete().where(ValidationJob.report_id == report.id)
    )
    await db_session.delete(report)
    await db_session.commit()
    assert (await db_session.scalars(select(ReportEvalTag))).all() == []
