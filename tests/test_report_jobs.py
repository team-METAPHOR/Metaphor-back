"""GET /reports/{report_id}/validation-jobs: every run of a report, newest first."""

from datetime import timedelta

import pytest

from tests.factories import login_as, make_org, make_program
from tests.test_report_list import NOW, add_job, seed


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    await db_session.commit()
    admin, admin_h = await login_as(client, db_session, org, "admin")
    _, viewer_h = await login_as(client, db_session, org, "viewer")
    return {
        "org": org,
        "program": program,
        "admin": admin,
        "admin_h": admin_h,
        "viewer_h": viewer_h,
    }


async def test_lists_all_runs_newest_first_with_cursor(client, db_session, world):
    report = await seed(db_session, world)
    kinds = ["initial", "fresh", "resume", "retest"]
    for i, kind in enumerate(kinds):
        await add_job(
            db_session,
            report,
            kind=kind,
            verdict="reproduced",
            created_at=NOW + timedelta(seconds=i),
            started_at=NOW + timedelta(seconds=i),
            finished_at=NOW + timedelta(seconds=i + 5),
        )
    other = await seed(db_session, world)
    await add_job(db_session, other)
    await db_session.commit()

    url = f"/api/v1/reports/{report.id}/validation-jobs"
    first = await client.get(f"{url}?limit=3", headers=world["viewer_h"])
    assert first.status_code == 200, first.text
    body = first.json()
    assert [j["kind"] for j in body["items"]] == ["retest", "resume", "fresh"]
    assert {j["report_id"] for j in body["items"]} == {report.id}
    assert "engine_job_id" not in body["items"][0] and "scope_snapshot" not in body["items"][0]
    rest = await client.get(
        f"{url}?limit=3&cursor={body['next_cursor']}", headers=world["viewer_h"]
    )
    assert [j["kind"] for j in rest.json()["items"]] == ["initial"]
    assert rest.json()["next_cursor"] is None


async def test_report_without_runs_is_empty(client, db_session, world):
    report = await seed(db_session, world)
    response = await client.get(
        f"/api/v1/reports/{report.id}/validation-jobs", headers=world["admin_h"]
    )
    assert response.json() == {"items": [], "next_cursor": None}


async def test_other_org_gets_404(client, db_session, world):
    report = await seed(db_session, world)
    await add_job(db_session, report)
    await db_session.commit()
    _, stranger_h = await login_as(client, db_session)
    response = await client.get(f"/api/v1/reports/{report.id}/validation-jobs", headers=stranger_h)
    assert response.status_code == 404


async def test_bad_cursor_is_400(client, db_session, world):
    report = await seed(db_session, world)
    response = await client.get(
        f"/api/v1/reports/{report.id}/validation-jobs?cursor=!!", headers=world["admin_h"]
    )
    assert (response.status_code, response.json()["error"]["code"]) == (400, "INVALID_CURSOR")
