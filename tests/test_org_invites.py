import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update

from app.core.ids import new_id
from app.models import AuditEvent, Membership, OrgInvite
from tests.factories import login_as, make_org

ACCEPT = "/api/v1/invites/accept"


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    admin, admin_h = await login_as(client, db_session, org, "admin")
    dev, dev_h = await login_as(client, db_session, org, "developer")
    _, out_h = await login_as(client, db_session, await make_org(db_session, name="Other"), "admin")
    return {
        "org": org,
        "admin": admin,
        "admin_h": admin_h,
        "dev": dev,
        "dev_h": dev_h,
        "out_h": out_h,
    }


def invites_url(org_id, invite_id=None):
    return f"/api/v1/orgs/{org_id}/invites" + (f"/{invite_id}" if invite_id else "")


async def invite(client, world, **body) -> dict:
    payload = {"role": "developer", **body}
    response = await client.post(
        invites_url(world["org"].id), json=payload, headers=world["admin_h"]
    )
    assert response.status_code == 201, response.text
    return response.json()


async def accept(client, headers, token):
    return await client.post(ACCEPT, json={"token": token}, headers=headers)


def without_request_id(response) -> dict:
    body = response.json()
    body["error"].pop("request_id")
    return body


async def test_admin_creates_invite_token_shown_once(client, db_session, world):
    created = await invite(client, world, role="reviewer", expires_in_days=3)
    assert set(created) == {"id", "role", "expires_at", "token"}
    assert created["role"] == "reviewer" and len(created["token"]) >= 43
    expires_at = datetime.fromisoformat(created["expires_at"])
    assert timedelta(days=2, hours=23) < expires_at - datetime.now(UTC) <= timedelta(days=3)

    listed = await client.get(invites_url(world["org"].id), headers=world["admin_h"])
    assert listed.status_code == 200
    assert listed.json() == [
        {
            "id": created["id"],
            "role": "reviewer",
            "created_by": world["admin"].id,
            "expires_at": created["expires_at"],
        }
    ]
    assert created["token"] not in listed.text

    row = await db_session.get(OrgInvite, created["id"])
    assert row.token_hash == hashlib.sha256(created["token"].encode()).hexdigest()
    dump = await db_session.execute(text("SELECT row_to_json(t)::text FROM org_invites t"))
    audits = await db_session.execute(text("SELECT row_to_json(a)::text FROM audit_events a"))
    for stored in (*dump.scalars(), *audits.scalars()):
        assert created["token"] not in stored

    for days in (0, 31):
        bad = await client.post(
            invites_url(world["org"].id),
            json={"role": "developer", "expires_in_days": days},
            headers=world["admin_h"],
        )
        assert bad.status_code == 422


async def test_accept_creates_membership_with_role(client, db_session, world):
    created = await invite(client, world, role="reviewer")
    joiner, joiner_h = await login_as(client, db_session)
    response = await accept(client, joiner_h, created["token"])
    assert response.status_code == 200, response.text
    assert response.json() == {
        "org_id": world["org"].id,
        "org_name": world["org"].name,
        "role": "reviewer",
    }
    membership = await db_session.get(Membership, (world["org"].id, joiner.id))
    assert membership.role == "reviewer" and membership.is_active
    row = await db_session.get(OrgInvite, created["id"])
    await db_session.refresh(row)
    assert row.used_by == joiner.id and row.used_at is not None
    me = (await client.get("/api/v1/auth/me", headers=joiner_h)).json()
    assert [m["org_id"] for m in me["memberships"]] == [world["org"].id]
    listed = await client.get(invites_url(world["org"].id), headers=world["admin_h"])
    assert listed.json() == []
    actions = (await db_session.scalars(select(AuditEvent.action))).all()
    assert "invite.create" in actions and "invite.accept" in actions


async def test_invite_accept_is_single_use_under_race(client, db_session, world):
    for _ in range(5):
        created = await invite(client, world)
        users = [await login_as(client, db_session) for _ in range(4)]
        results = await asyncio.gather(*(accept(client, h, created["token"]) for _, h in users))
        assert sorted(r.status_code for r in results) == [200, 404, 404, 404]
        joined = select(func.count()).where(
            Membership.org_id == world["org"].id,
            Membership.user_id.in_([u.id for u, _ in users]),
        )
        assert await db_session.scalar(joined) == 1


async def test_expired_revoked_used_unknown_all_404_same_body(client, db_session, world):
    _, joiner_h = await login_as(client, db_session)
    _, other_h = await login_as(client, db_session)

    expired = await invite(client, world)
    await db_session.execute(
        update(OrgInvite)
        .where(OrgInvite.id == expired["id"])
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()

    revoked = await invite(client, world)
    response = await client.delete(
        invites_url(world["org"].id, revoked["id"]), headers=world["admin_h"]
    )
    assert response.status_code == 204

    used = await invite(client, world)
    assert (await accept(client, other_h, used["token"])).status_code == 200

    bodies = []
    for token in (expired["token"], revoked["token"], used["token"], "not-a-real-token"):
        response = await accept(client, joiner_h, token)
        assert response.status_code == 404
        bodies.append(without_request_id(response))
    assert bodies[0]["error"]["code"] == "INVITE_INVALID"
    assert all(body == bodies[0] for body in bodies)

    failures = (
        await db_session.scalars(
            select(AuditEvent.target).where(AuditEvent.action == "invite.accept_failed")
        )
    ).all()
    assert len(failures) == 4 and all(len(t) == 12 for t in failures)


async def test_reactivated_member_gets_invite_role(client, db_session, world):
    org, dev = world["org"], world["dev"]
    membership = await db_session.get(Membership, (org.id, dev.id))
    membership.role, membership.is_active = "admin", False
    await db_session.commit()

    created = await invite(client, world, role="developer")
    _, dev_h = await login_as_existing(client, dev)
    response = await accept(client, dev_h, created["token"])
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "developer"
    await db_session.refresh(membership)
    assert membership.is_active is True and membership.role == "developer"


async def login_as_existing(client, user):
    from tests.factories import PLATFORM_ORIGIN, TEST_PASSWORD

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": TEST_PASSWORD},
        headers={"Origin": PLATFORM_ORIGIN},
    )
    assert response.status_code == 200, response.text
    return user, {"Cookie": f"sid={response.cookies['sid']}", "Origin": PLATFORM_ORIGIN}


async def test_already_member_409_keeps_invite(client, db_session, world):
    created = await invite(client, world, role="admin")
    response = await accept(client, world["dev_h"], created["token"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ALREADY_MEMBER"
    row = await db_session.get(OrgInvite, created["id"])
    assert row.used_at is None
    membership = await db_session.get(Membership, (world["org"].id, world["dev"].id))
    assert membership.role == "developer"
    _, joiner_h = await login_as(client, db_session)
    assert (await accept(client, joiner_h, created["token"])).status_code == 200


async def test_non_admin_cannot_invite_403(client, world):
    org = world["org"]
    response = await client.post(
        invites_url(org.id), json={"role": "admin"}, headers=world["dev_h"]
    )
    assert response.status_code == 403
    assert (await client.get(invites_url(org.id), headers=world["dev_h"])).status_code == 403
    created = await invite(client, world)
    revoke = await client.delete(invites_url(org.id, created["id"]), headers=world["dev_h"])
    assert revoke.status_code == 403
    outsider = await client.post(
        invites_url(org.id), json={"role": "developer"}, headers=world["out_h"]
    )
    assert outsider.status_code == 404
    bad_role = await client.post(
        invites_url(org.id), json={"role": "owner"}, headers=world["admin_h"]
    )
    assert bad_role.status_code == 422


async def test_pending_invite_limit_409(client, db_session, world):
    org = world["org"]
    now = datetime.now(UTC)
    db_session.add_all(
        OrgInvite(
            id=new_id(),
            org_id=org.id,
            token_hash=f"{i:064x}",
            role="developer",
            created_by=world["admin"].id,
            expires_at=now + timedelta(days=1),
        )
        for i in range(50)
    )
    await db_session.commit()
    response = await client.post(
        invites_url(org.id), json={"role": "developer"}, headers=world["admin_h"]
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVITE_LIMIT"


async def test_accept_attempt_limit_429(client, db_session, world):
    created = await invite(client, world)
    _, joiner_h = await login_as(client, db_session)
    for i in range(20):
        assert (await accept(client, joiner_h, f"wrong-{i}")).status_code == 404
    response = await accept(client, joiner_h, created["token"])
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "INVITE_ACCEPT_LIMITED"
    row = await db_session.get(OrgInvite, created["id"])
    assert row.used_at is None


@pytest.mark.parametrize("change", ["demote", "remove"])
async def test_invite_dies_when_creator_loses_admin(client, db_session, world, change):
    """An invite carries its creator's authority: once they are no longer an admin, it is void."""
    org = world["org"]
    second, second_h = await login_as(client, db_session, org, "admin")
    created = await client.post(invites_url(org.id), json={"role": "admin"}, headers=second_h)
    assert created.status_code == 201
    await db_session.execute(
        update(Membership)
        .where(Membership.org_id == org.id, Membership.user_id == second.id)
        .values(**({"role": "developer"} if change == "demote" else {"is_active": False}))
    )
    await db_session.commit()

    _, joiner_h = await login_as(client, db_session)
    response = await accept(client, joiner_h, created.json()["token"])
    assert (response.status_code, response.json()["error"]["code"]) == (404, "INVITE_INVALID")
    joined = await db_session.scalar(
        select(func.count()).select_from(Membership).where(Membership.org_id == org.id)
    )
    assert joined == 3  # admin, dev, second — nobody new


async def test_concurrent_accept_and_revoke_never_deadlock(client, db_session, world):
    """Accept and revoke lock org → invite in the same order: one wins, neither errors."""
    for _ in range(5):
        created = await invite(client, world)
        _, joiner_h = await login_as(client, db_session)
        accepted, revoked = await asyncio.gather(
            accept(client, joiner_h, created["token"]),
            client.delete(invites_url(world["org"].id, created["id"]), headers=world["admin_h"]),
        )
        assert {accepted.status_code, revoked.status_code} in ({200, 404}, {404, 204})
