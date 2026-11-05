import asyncio

import pytest
from sqlalchemy import func, select

from app.models import AuditEvent, Membership
from tests.factories import TEST_PASSWORD, login_as, make_org, make_program, make_report


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    other = await make_org(db_session, name="Other")
    admin, admin_h = await login_as(client, db_session, org, "admin")
    dev, dev_h = await login_as(client, db_session, org, "developer")
    _, out_h = await login_as(client, db_session, other, "admin")
    return {
        "org": org,
        "admin": admin,
        "admin_h": admin_h,
        "dev": dev,
        "dev_h": dev_h,
        "out_h": out_h,
    }


def members_url(org_id, user_id=None):
    return f"/api/v1/orgs/{org_id}/members" + (f"/{user_id}" if user_id else "")


async def active_admins(db_session, org_id) -> int:
    query = select(func.count()).where(
        Membership.org_id == org_id, Membership.role == "admin", Membership.is_active
    )
    return await db_session.scalar(query)


async def test_members_list_visible_to_members_only(client, db_session, world):
    org = world["org"]
    gone, _ = await login_as(client, db_session, org, "reviewer")
    membership = await db_session.get(Membership, (org.id, gone.id))
    membership.is_active = False
    await db_session.commit()

    as_admin = await client.get(members_url(org.id), headers=world["admin_h"])
    assert as_admin.status_code == 200, as_admin.text
    rows = {m["user_id"]: m for m in as_admin.json()}
    assert set(rows) == {world["admin"].id, world["dev"].id, gone.id}
    assert rows[gone.id]["is_active"] is False
    assert set(rows[world["dev"].id]) == {
        "user_id",
        "email",
        "display_name",
        "role",
        "is_active",
        "joined_at",
    }

    as_dev = await client.get(members_url(org.id), headers=world["dev_h"])
    assert as_dev.status_code == 200
    assert {m["user_id"] for m in as_dev.json()} == {world["admin"].id, world["dev"].id}

    outsider = await client.get(members_url(org.id), headers=world["out_h"])
    assert outsider.status_code == 404


async def test_admin_changes_role(client, db_session, world):
    org, dev = world["org"], world["dev"]
    response = await client.patch(
        members_url(org.id, dev.id), json={"role": "reviewer"}, headers=world["admin_h"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "reviewer"
    membership = await db_session.get(Membership, (org.id, dev.id))
    await db_session.refresh(membership)
    assert membership.role == "reviewer"
    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "member.role_change")
    )
    assert audit.target == dev.id and audit.detail == {"from": "developer", "to": "reviewer"}

    bad = await client.patch(
        members_url(org.id, dev.id), json={"role": "owner"}, headers=world["admin_h"]
    )
    assert bad.status_code == 422
    missing = await client.patch(
        members_url(org.id, "0" * 32), json={"role": "admin"}, headers=world["admin_h"]
    )
    assert missing.status_code == 404


async def test_developer_cannot_change_role_403(client, world):
    org = world["org"]
    response = await client.patch(
        members_url(org.id, world["dev"].id), json={"role": "admin"}, headers=world["dev_h"]
    )
    assert response.status_code == 403
    outsider = await client.patch(
        members_url(org.id, world["dev"].id), json={"role": "admin"}, headers=world["out_h"]
    )
    assert outsider.status_code == 404


async def test_last_admin_cannot_be_demoted(client, db_session, world):
    org, admin = world["org"], world["admin"]
    response = await client.patch(
        members_url(org.id, admin.id), json={"role": "developer"}, headers=world["admin_h"]
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_ADMIN"
    assert await active_admins(db_session, org.id) == 1


async def test_last_admin_cannot_be_demoted_concurrently(client, db_session, world):
    org, admin = world["org"], world["admin"]
    second, second_h = await login_as(client, db_session, org, "admin")

    async def demote(user, headers):
        return await client.patch(
            members_url(org.id, user.id), json={"role": "developer"}, headers=headers
        )

    for _ in range(3):
        results = await asyncio.gather(demote(second, world["admin_h"]), demote(admin, second_h))
        statuses = sorted(r.status_code for r in results)
        assert statuses[0] == 200 and statuses[1] in (403, 409), statuses
        assert await active_admins(db_session, org.id) == 1
        # Restore both admins directly and race again.
        for user in (admin, second):
            membership = await db_session.get(Membership, (org.id, user.id))
            await db_session.refresh(membership)
            membership.role = "admin"
        await db_session.commit()


async def test_member_can_leave(client, db_session, world):
    org, dev = world["org"], world["dev"]
    response = await client.delete(members_url(org.id, dev.id), headers=world["dev_h"])
    assert response.status_code == 204
    membership = await db_session.get(Membership, (org.id, dev.id))
    await db_session.refresh(membership)
    assert membership.is_active is False
    audit = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "member.remove"))
    assert audit.target == dev.id and audit.detail == {"self": True}


async def test_developer_cannot_remove_others_403(client, world):
    org = world["org"]
    response = await client.delete(members_url(org.id, world["admin"].id), headers=world["dev_h"])
    assert response.status_code == 403


async def test_last_admin_cannot_leave_409(client, db_session, world):
    org, admin = world["org"], world["admin"]
    response = await client.delete(members_url(org.id, admin.id), headers=world["admin_h"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_ADMIN"
    assert await active_admins(db_session, org.id) == 1


async def test_removed_member_loses_access_immediately(client, db_session, world):
    org, dev = world["org"], world["dev"]
    program = await make_program(db_session, org)
    report = await make_report(db_session, org, program, author=dev)
    await db_session.commit()
    report_url = f"/api/v1/reports/{report.id}"
    assert (await client.get(report_url, headers=world["dev_h"])).status_code == 200

    response = await client.delete(members_url(org.id, dev.id), headers=world["admin_h"])
    assert response.status_code == 204
    audit = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "member.remove"))
    assert audit.detail == {"self": False}

    # deactivate_membership revokes every session: the old cookie is dead.
    assert (await client.get(report_url, headers=world["dev_h"])).status_code == 401
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": dev.email, "password": TEST_PASSWORD},
        headers={"Origin": "http://localhost:3000"},
    )
    headers = {"Cookie": f"sid={login.cookies['sid']}"}
    assert (await client.get(report_url, headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/orgs/{org.id}", headers=headers)).status_code == 404
