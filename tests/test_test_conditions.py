"""Per-program test condition registry (#54): admin CRUD, engine-rule validation, secrets."""

import pytest
from sqlalchemy import select, update

from app.models import AuditEvent, ProgramTestCondition
from tests.factories import login_as, make_org, make_program

SECRET_REF = "STAFF_SESSION"


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    other_org = await make_org(db_session, name="Other")
    program = await make_program(db_session, org)
    other_program = await make_program(db_session, other_org)
    await db_session.commit()
    _, admin = await login_as(client, db_session, org, "admin")
    _, developer = await login_as(client, db_session, org, "developer")
    _, viewer = await login_as(client, db_session, org, "viewer")
    _, outsider = await login_as(client, db_session, other_org, "admin")
    return {
        "program": program,
        "other_program": other_program,
        "admin": admin,
        "dev": developer,
        "viewer": viewer,
        "out": outsider,
    }


def condition(**overrides) -> dict:
    body = {
        "condition_id": "staff-chromium",
        "label": "일반 직원 · Chromium",
        "browser_name": "chromium",
        "width": 1280,
        "height": 800,
        "response_delay_ms": 0,
        "test_context_id": "staff-1",
        "record_video": False,
        "reset_adapter": "stateless",
    }
    body.update(overrides)
    return body


def url(world, suffix="", program="program"):
    return f"/api/v1/programs/{world[program].id}/test-conditions{suffix}"


async def add(client, world, headers=None, **overrides):
    return await client.post(
        url(world), json=condition(**overrides), headers=headers or world["admin"]
    )


async def test_admin_creates_lists_updates_deletes(client, world, db_session):
    response = await add(client, world)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["condition_id"] == "staff-chromium"
    assert body["has_session"] is False
    await add(
        client, world, condition_id="anon-webkit", browser_name="webkit", response_delay_ms=800
    )

    listed = await client.get(url(world), headers=world["viewer"])
    assert listed.status_code == 200
    assert [c["condition_id"] for c in listed.json()["items"]] == ["staff-chromium", "anon-webkit"]

    patched = await client.patch(
        url(world, "/anon-webkit"), json={"width": 390, "height": 844}, headers=world["admin"]
    )
    assert patched.status_code == 200, patched.text
    assert (patched.json()["width"], patched.json()["browser_name"]) == (390, "webkit")

    deleted = await client.delete(url(world, "/anon-webkit"), headers=world["admin"])
    assert deleted.status_code == 204
    listed = await client.get(url(world), headers=world["admin"])
    assert [c["condition_id"] for c in listed.json()["items"]] == ["staff-chromium"]
    actions = (await db_session.scalars(select(AuditEvent.action))).all()
    assert actions.count("program.condition.create") == 2
    assert "program.condition.update" in actions and "program.condition.delete" in actions


async def test_non_admin_cannot_write(client, world):
    assert (await add(client, world, headers=world["dev"])).status_code == 403
    assert (await add(client, world, headers=world["viewer"])).status_code == 403
    await add(client, world)
    response = await client.patch(
        url(world, "/staff-chromium"), json={"width": 400}, headers=world["dev"]
    )
    assert response.status_code == 403
    response = await client.delete(url(world, "/staff-chromium"), headers=world["viewer"])
    assert response.status_code == 403


async def test_other_org_gets_404(client, world):
    await add(client, world)
    assert (await client.get(url(world), headers=world["out"])).status_code == 404
    assert (await add(client, world, headers=world["out"])).status_code == 404
    response = await client.patch(
        url(world, "/staff-chromium"), json={"width": 400}, headers=world["out"]
    )
    assert response.status_code == 404
    response = await client.delete(url(world, "/staff-chromium"), headers=world["out"])
    assert response.status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"condition_id": "bad id"},
        {"condition_id": "x" * 65},
        {"label": ""},
        {"label": "x" * 201},
        {"label": "연락처 user@example.com"},  # labels are public: no PII
        {"browser_name": "edge"},
        {"response_delay_ms": 2001},
        {"response_delay_ms": -1},
        {"width": 319},
        {"height": 1081},
        {"test_context_id": "has space"},
        {"reset_adapter": "rm -rf /"},
        {"session_secret_ref": "OTHER_ORG_SECRET"},  # operator-only field
        {"env": {"PATH": "/tmp"}},
    ],
)
async def test_invalid_condition_rejected(client, world, overrides):
    response = await add(client, world, **overrides)
    assert response.status_code == 422, response.text


async def test_duplicate_id_409_and_limit_16(client, world):
    await add(client, world)
    duplicate = await add(client, world)
    assert (duplicate.status_code, duplicate.json()["error"]["code"]) == (409, "CONDITION_EXISTS")
    for i in range(15):
        assert (await add(client, world, condition_id=f"c{i}")).status_code == 201
    response = await add(client, world, condition_id="c16")
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "CONDITION_LIMIT_EXCEEDED",
    )


async def test_session_ref_operator_only_and_never_returned(client, world, db_session):
    await add(client, world)
    # Operator binds a worker-side session out of band.
    await db_session.execute(
        update(ProgramTestCondition)
        .where(ProgramTestCondition.condition_id == "staff-chromium")
        .values(session_secret_ref=SECRET_REF)
    )
    await db_session.commit()
    listed = await client.get(url(world), headers=world["admin"])
    assert listed.json()["items"][0]["has_session"] is True
    assert SECRET_REF not in listed.text and "session_secret_ref" not in listed.text

    # Label edit keeps the binding; a test context change drops it (different account).
    kept = await client.patch(
        url(world, "/staff-chromium"), json={"label": "직원"}, headers=world["admin"]
    )
    assert kept.json()["has_session"] is True
    dropped = await client.patch(
        url(world, "/staff-chromium"), json={"test_context_id": "staff-2"}, headers=world["admin"]
    )
    assert dropped.json()["has_session"] is False


async def test_session_requires_synthetic_context(client, world, db_session):
    await add(client, world)
    await db_session.execute(update(ProgramTestCondition).values(session_secret_ref=SECRET_REF))
    await db_session.commit()
    response = await client.patch(
        url(world, "/staff-chromium"), json={"test_context_id": "anonymous"}, headers=world["admin"]
    )
    # The context change drops the session, so anonymous is allowed again.
    assert response.status_code == 200
    assert response.json()["has_session"] is False


async def test_program_base_url_change_unbinds_condition_sessions(client, world, db_session):
    await add(client, world)
    await db_session.execute(update(ProgramTestCondition).values(session_secret_ref=SECRET_REF))
    await db_session.commit()
    response = await client.patch(
        f"/api/v1/programs/{world['program'].id}",
        json={"base_url": "https://other.example.com"},
        headers=world["admin"],
    )
    assert response.status_code == 200, response.text
    listed = await client.get(url(world), headers=world["admin"])
    assert listed.json()["items"][0]["has_session"] is False


async def test_missing_condition_404(client, world):
    response = await client.patch(url(world, "/nope"), json={"width": 400}, headers=world["admin"])
    assert response.status_code == 404
    assert (await client.delete(url(world, "/nope"), headers=world["admin"])).status_code == 404


async def test_condition_session_on_http_origin_rejected(client, world, db_session):
    from app.models import ProgramScopeRevision

    await add(client, world)
    await db_session.execute(update(ProgramTestCondition).values(session_secret_ref=SECRET_REF))
    await db_session.commit()
    program_url = f"/api/v1/programs/{world['program'].id}"
    moved = await client.patch(
        program_url, json={"base_url": "http://staging.example.com"}, headers=world["admin"]
    )
    assert moved.json()["error"]["code"] == "CREDENTIALS_REQUIRE_HTTPS"
    # Legacy data: an HTTP origin with a bound condition session can't be kept on edit.
    await db_session.execute(
        update(ProgramScopeRevision)
        .where(ProgramScopeRevision.program_id == world["program"].id)
        .values(base_url="http://staging.example.com")
    )
    await db_session.commit()
    response = await client.patch(
        url(world, "/staff-chromium"), json={"width": 1024}, headers=world["admin"]
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CREDENTIALS_REQUIRE_HTTPS"
