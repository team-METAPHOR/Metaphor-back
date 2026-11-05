import pytest
from sqlalchemy import select, update

from app.models import AuditEvent, Membership, Report
from tests.factories import login_as, make_org, make_program, make_user
from tests.test_reports_api import submit


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    await db_session.commit()
    admin, admin_h = await login_as(client, db_session, org, "admin")
    reviewer, reviewer_h = await login_as(client, db_session, org, "reviewer")
    dev, dev_h = await login_as(client, db_session, org, "developer")
    dev.display_name = "개발자"
    await db_session.commit()
    body = (await submit(client, dev_h, program.id, description="버그")).json()
    return {
        "org": org,
        "program": program,
        "report_id": body["id"],
        "admin": admin,
        "reviewer": reviewer,
        "dev": dev,
        "admin_h": admin_h,
        "reviewer_h": reviewer_h,
        "dev_h": dev_h,
    }


async def assign(client, world, user_id, headers=None, report_id=None):
    return await client.patch(
        f"/api/v1/reports/{report_id or world['report_id']}/assignee",
        json={"user_id": user_id},
        headers=headers or world["reviewer_h"],
    )


async def test_assign_and_unassign(client, db_session, world):
    dev = world["dev"]
    response = await assign(client, world, dev.id)
    assert response.status_code == 200, response.text
    assert response.json()["assignee"] == {
        "id": dev.id,
        "display_name": "개발자",
        "email": dev.email,
        "active": True,
    }
    detail = await client.get(f"/api/v1/reports/{world['report_id']}", headers=world["dev_h"])
    assert detail.json()["assignee"]["id"] == dev.id
    (item,) = (await client.get("/api/v1/reports", headers=world["dev_h"])).json()["items"]
    assert item["assignee"]["id"] == dev.id

    cleared = await assign(client, world, None, world["admin_h"])
    assert (cleared.status_code, cleared.json()["assignee"]) == (200, None)
    audits = (
        await db_session.scalars(
            select(AuditEvent).where(AuditEvent.action == "report.assign").order_by(AuditEvent.id)
        )
    ).all()
    assert [a.detail for a in audits] == [
        {"from": None, "to": dev.id},
        {"from": dev.id, "to": None},
    ]
    assert all(a.target == world["report_id"] for a in audits)


async def test_unassigned_report_shows_null(client, world):
    detail = await client.get(f"/api/v1/reports/{world['report_id']}", headers=world["dev_h"])
    assert detail.json()["assignee"] is None


async def test_non_member_assignee_422_same_message(client, db_session, world):
    other = await make_org(db_session, "Other")
    outsider = await make_user(db_session, other, "admin")
    inactive = await make_user(db_session, world["org"], "developer")
    await db_session.execute(
        update(Membership).where(Membership.user_id == inactive.id).values(is_active=False)
    )
    await db_session.commit()
    errors = []
    for user_id in (outsider.id, inactive.id, "f" * 32):
        response = await assign(client, world, user_id)
        assert response.status_code == 422
        errors.append((response.json()["error"]["code"], response.json()["error"]["message"]))
    assert len(set(errors)) == 1 and errors[0][0] == "ASSIGNEE_NOT_MEMBER"


async def test_roles_and_cross_org(client, db_session, world):
    assert (await assign(client, world, world["dev"].id, world["dev_h"])).status_code == 403
    other = await make_org(db_session, "Other")
    await db_session.commit()
    _, other_admin_h = await login_as(client, db_session, other, "admin")
    response = await assign(client, world, world["dev"].id, other_admin_h)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "NOT_FOUND")


async def test_deactivated_assignee_kept_but_redacted(client, db_session, world):
    """The assignment survives deactivation, but a former member's email/name is not shown."""
    dev = world["dev"]
    await assign(client, world, dev.id)
    await db_session.execute(
        update(Membership).where(Membership.user_id == dev.id).values(is_active=False)
    )
    await db_session.commit()
    gone = {"id": dev.id, "display_name": None, "email": None, "active": False}
    detail = await client.get(f"/api/v1/reports/{world['report_id']}", headers=world["admin_h"])
    assert detail.json()["assignee"] == gone
    (item,) = (await client.get("/api/v1/reports", headers=world["admin_h"])).json()["items"]
    assert item["assignee"] == gone
    filtered = await client.get(f"/api/v1/reports?assignee={dev.id}", headers=world["admin_h"])
    assert [i["assignee"] for i in filtered.json()["items"]] == [gone]


async def test_assignee_filter_me_and_user_id_with_cursor(client, db_session, world):
    ids = [world["report_id"]]
    for _ in range(3):
        ids.append(
            (await submit(client, world["dev_h"], world["program"].id, description="b")).json()[
                "id"
            ]
        )
    for report_id in ids[:3]:
        await assign(client, world, world["dev"].id, report_id=report_id)
    await assign(client, world, world["reviewer"].id, report_id=ids[3])

    h = world["dev_h"]
    page = (await client.get("/api/v1/reports?assignee=me&limit=2", headers=h)).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = (
        await client.get(
            f"/api/v1/reports?assignee=me&limit=2&cursor={page['next_cursor']}", headers=h
        )
    ).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    mine = {i["id"] for i in page["items"] + rest["items"]}
    assert mine == set(ids[:3])

    by_id = (await client.get(f"/api/v1/reports?assignee={world['reviewer'].id}", headers=h)).json()
    assert [i["id"] for i in by_id["items"]] == [ids[3]]
    reviewer_me = (
        await client.get("/api/v1/reports?assignee=me", headers=world["reviewer_h"])
    ).json()
    assert [i["id"] for i in reviewer_me["items"]] == [ids[3]]
    admin_me = (await client.get("/api/v1/reports?assignee=me", headers=world["admin_h"])).json()
    assert admin_me["items"] == []
    report = await db_session.get(Report, ids[3], populate_existing=True)
    assert report.assignee_id == world["reviewer"].id
