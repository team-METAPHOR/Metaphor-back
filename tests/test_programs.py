import pytest
from sqlalchemy import select, update

from app.models import AuditEvent, Program, ProgramScopeRevision
from tests.factories import login_as, make_org

SECRET_REF = "SHOP_STAGING_ADMIN"


def scope(**overrides) -> dict:
    body = {
        "base_url": "https://staging.example.com",
        "allowed_path_prefixes": ["/shop"],
        "test_context_id": "synthetic-1",
        "mask_selectors": ["#card-number"],
    }
    body.update(overrides)
    return body


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    other_org = await make_org(db_session, name="Other")
    _, admin = await login_as(client, db_session, org, "admin")
    _, developer = await login_as(client, db_session, org, "developer")
    _, outsider = await login_as(client, db_session, other_org, "admin")
    return {"org": org, "other_org": other_org, "admin": admin, "dev": developer, "out": outsider}


async def create(client, world, **body):
    payload = {"org_id": world["org"].id, "name": "Shop", **scope(), **body}
    return await client.post("/api/v1/programs", json=payload, headers=world["admin"])


async def created(client, world, **body) -> dict:
    response = await create(client, world, **body)
    assert response.status_code == 201, response.text
    return response.json()


async def bind_session(db_session, program_id: str) -> None:
    """Operator-side: session refs are provisioned out of band, never through the API."""
    await db_session.execute(
        update(ProgramScopeRevision)
        .where(ProgramScopeRevision.program_id == program_id)
        .values(session_secret_ref=SECRET_REF)
    )
    await db_session.commit()


async def test_admin_creates_program_rev1(client, world, db_session):
    response = await create(client, world)
    assert response.status_code == 201
    body = response.json()
    assert body["current_revision"] == 1
    assert body["status"] == "draft"
    assert body["scope"]["has_session"] is False
    assert body["scope"]["allowed_path_prefixes"] == ["/shop"]
    assert "session_secret_ref" not in response.text
    assert "allow_private_targets" not in response.text
    row = await db_session.get(ProgramScopeRevision, (body["id"], 1))
    assert row.base_url == "https://staging.example.com/"
    actions = (await db_session.scalars(select(AuditEvent.action))).all()
    assert actions == ["program.create"]


async def test_invalid_scope_rejected_422(client, world):
    response = await create(client, world, base_url="https://a.com/path")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BASE_URL_MUST_BE_ORIGIN"


@pytest.mark.parametrize(
    "overrides",
    [
        {"width": 100},
        {"mask_selectors": ["#x"] * 31},
        {"mask_selectors": ["x" * 257]},
        {"mask_selectors": [""]},
    ],
)
async def test_invalid_run_options_422(client, world, overrides):
    response = await create(client, world, **overrides)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RUN_OPTIONS"


@pytest.mark.parametrize(
    "field",
    [
        {"allow_private_targets": True},
        {"private_cidrs": ["10.0.0.0/8"]},
        # Another org's worker secret must not be nameable by a customer admin.
        {"session_secret_ref": "OTHER_ORG_ADMIN"},
    ],
)
async def test_operator_only_fields_rejected(client, world, field):
    assert (await create(client, world, **field)).status_code == 422


async def test_patch_scope_creates_new_revision_keeps_old(client, world, db_session):
    program = await created(client, world)
    # Operator-only settings on rev1 must carry over to rev2.
    await db_session.execute(
        update(ProgramScopeRevision)
        .where(ProgramScopeRevision.program_id == program["id"])
        .values(
            allow_private_targets=True, private_cidrs=["10.1.0.0/16"], session_secret_ref=SECRET_REF
        )
    )
    await db_session.commit()
    before = await db_session.get(ProgramScopeRevision, (program["id"], 1))
    before_values = {c.key: getattr(before, c.key) for c in ProgramScopeRevision.__table__.c}

    response = await client.patch(
        f"/api/v1/programs/{program['id']}",
        json={"allowed_path_prefixes": ["/shop", "/cart"]},
        headers=world["admin"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["current_revision"] == 2
    assert response.json()["scope"]["allowed_path_prefixes"] == ["/cart", "/shop"]

    db_session.expire_all()
    old = await db_session.get(ProgramScopeRevision, (program["id"], 1))
    assert {c.key: getattr(old, c.key) for c in ProgramScopeRevision.__table__.c} == before_values
    new = await db_session.get(ProgramScopeRevision, (program["id"], 2))
    assert new.session_secret_ref == SECRET_REF
    assert new.mask_selectors == ["#card-number"]
    assert (new.allow_private_targets, new.private_cidrs) == (True, ["10.1.0.0/16"])


async def test_patch_status_or_same_scope_keeps_revision(client, world, db_session):
    program = await created(client, world)
    url = f"/api/v1/programs/{program['id']}"
    for body in (
        {"status": "active", "name": "Shop 2"},
        {"base_url": "https://staging.example.com/"},
    ):
        response = await client.patch(url, json=body, headers=world["admin"])
        assert response.status_code == 200, response.text
        assert response.json()["current_revision"] == 1
    assert response.json()["status"] == "active"
    assert response.json()["name"] == "Shop 2"
    revisions = (await db_session.scalars(select(ProgramScopeRevision.revision))).all()
    assert revisions == [1]
    actions = (await db_session.scalars(select(AuditEvent.action).order_by(AuditEvent.id))).all()
    assert actions == ["program.create", "program.update"]


async def test_session_ref_not_patchable_and_dropped_on_origin_change(client, world, db_session):
    program = await created(client, world)
    await bind_session(db_session, program["id"])
    url = f"/api/v1/programs/{program['id']}"
    for value in ("OTHER_ORG_ADMIN", None):
        patch = await client.patch(url, json={"session_secret_ref": value}, headers=world["admin"])
        assert patch.status_code == 422
    # A bound session needs a synthetic test context.
    anonymous = await client.patch(
        url, json={"test_context_id": "anonymous"}, headers=world["admin"]
    )
    assert anonymous.json()["error"]["code"] == "INVALID_RUN_OPTIONS"
    # Provisioned credentials are bound to their origin: moving base_url drops them.
    moved = await client.patch(
        url, json={"base_url": "https://attacker.example"}, headers=world["admin"]
    )
    assert moved.status_code == 200
    assert moved.json()["scope"]["has_session"] is False
    rev2 = await db_session.get(ProgramScopeRevision, (program["id"], 2))
    assert rev2.session_secret_ref is None


async def test_invalid_patch_does_not_create_revision(client, world, db_session):
    program = await created(client, world)
    response = await client.patch(
        f"/api/v1/programs/{program['id']}",
        json={"base_url": "ftp://staging.example.com"},
        headers=world["admin"],
    )
    assert response.status_code == 422
    assert (await db_session.get(Program, program["id"])).current_revision == 1


async def test_developer_cannot_create_or_patch_403(client, world):
    response = await client.post(
        "/api/v1/programs",
        json={"org_id": world["org"].id, "name": "Shop", **scope()},
        headers=world["dev"],
    )
    assert response.status_code == 403
    program = await created(client, world)
    patch = await client.patch(
        f"/api/v1/programs/{program['id']}", json={"name": "x"}, headers=world["dev"]
    )
    assert patch.status_code == 403
    # Developers can still read the full program.
    get = await client.get(f"/api/v1/programs/{program['id']}", headers=world["dev"])
    assert get.status_code == 200
    assert get.json()["scope"]["has_session"] is False


async def test_other_org_program_404(client, world):
    program = await created(client, world)
    url = f"/api/v1/programs/{program['id']}"
    assert (await client.get(url, headers=world["out"])).status_code == 404
    assert (await client.patch(url, json={"name": "x"}, headers=world["out"])).status_code == 404
    create_elsewhere = await client.post(
        "/api/v1/programs",
        json={"org_id": world["other_org"].id, "name": "x", **scope()},
        headers=world["admin"],
    )
    assert create_elsewhere.status_code == 404


async def test_invite_only_hidden_from_uninvited_user(client, world, db_session):
    program = await created(client, world, status="active")
    reporter, reporter_headers = await login_as(client, db_session)

    listed = await client.get("/api/v1/programs", headers=reporter_headers)
    assert listed.json()["items"] == []
    get = await client.get(f"/api/v1/programs/{program['id']}", headers=reporter_headers)
    assert get.status_code == 404

    invite = await client.post(
        f"/api/v1/programs/{program['id']}/invites",
        json={"email": reporter.email},
        headers=world["admin"],
    )
    assert invite.status_code == 201, invite.text

    listed = await client.get("/api/v1/programs", headers=reporter_headers)
    assert [p["id"] for p in listed.json()["items"]] == [program["id"]]
    get = await client.get(f"/api/v1/programs/{program['id']}", headers=reporter_headers)
    assert get.status_code == 200
    assert set(get.json()) == {"id", "name", "status", "submission_mode", "base_url"}

    removed = await client.delete(
        f"/api/v1/programs/{program['id']}/invites/{reporter.id}", headers=world["admin"]
    )
    assert removed.status_code == 204
    listed = await client.get("/api/v1/programs", headers=reporter_headers)
    assert listed.json()["items"] == []


async def test_open_program_visible_to_any_user_as_public_subset(client, world, db_session):
    open_program = await created(
        client, world, status="active", submission_mode="open_authenticated"
    )
    await created(client, world, name="Draft", submission_mode="open_authenticated")  # not active
    _, reporter_headers = await login_as(client, db_session)
    listed = await client.get("/api/v1/programs", headers=reporter_headers)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [p["id"] for p in items] == [open_program["id"]]
    assert items[0]["role"] is None
    assert "has_session" not in listed.text and SECRET_REF not in listed.text

    member_list = await client.get("/api/v1/programs", headers=world["dev"])
    assert {p["role"] for p in member_list.json()["items"]} == {"developer"}
    assert len(member_list.json()["items"]) == 2


async def test_invite_requires_admin_and_known_user(client, world):
    program = await created(client, world)
    url = f"/api/v1/programs/{program['id']}/invites"
    assert (
        await client.post(url, json={"email": "x@example.com"}, headers=world["dev"])
    ).status_code == 403
    missing = await client.post(url, json={"email": "nobody@example.com"}, headers=world["admin"])
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "USER_NOT_FOUND"


async def test_secret_ref_never_in_any_response(client, world, db_session):
    program = await created(client, world, status="active", submission_mode="open_authenticated")
    await bind_session(db_session, program["id"])
    url = f"/api/v1/programs/{program['id']}"
    _, reporter_headers = await login_as(client, db_session)
    responses = [
        await client.get(url, headers=world["admin"]),
        await client.get("/api/v1/programs", headers=world["admin"]),
        await client.get(url, headers=reporter_headers),
        await client.get("/api/v1/programs", headers=reporter_headers),
        await client.patch(url, json={"width": 1024}, headers=world["admin"]),
    ]
    for response in responses:
        assert response.status_code == 200
        assert SECRET_REF not in response.text
    assert responses[-1].json()["scope"]["has_session"] is True  # carried into rev2


async def test_unknown_reset_adapter_422(client, world):
    response = await create(client, world, reset_adapter="truncate_all")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "RESET_ADAPTER_UNKNOWN"
    program = await created(client, world)
    patch = await client.patch(
        f"/api/v1/programs/{program['id']}", json={"reset_adapter": "nope"}, headers=world["admin"]
    )
    assert patch.json()["error"]["code"] == "RESET_ADAPTER_UNKNOWN"
