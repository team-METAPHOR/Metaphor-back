"""Admin-approved test writes (`state_changes`, core_engine 0.6.0 StateChangePolicy)."""

import pytest
from core_engine import StateChangePolicy
from sqlalchemy import select, update

from app.models import Program, ProgramScopeRevision, ValidationJob
from app.services import programs as programs_service
from app.services.intake import ReportSubmission
from app.services.reports import create_report
from app.worker import runner
from tests.factories import login_as, make_org, make_program

ADAPTER = "stateful-test"
B02 = {
    "environment": "test",
    "reset_adapter_id": ADAPTER,
    "allowed_requests": [["POST", "/api/cart/items"], ["PATCH", "/api/cart/items"]],
}


@pytest.fixture(autouse=True)
def stateful_adapter(monkeypatch):
    # The real adapter registers in a later change; any stateful id the worker knows works.
    monkeypatch.setattr(
        programs_service, "ALLOWED_RESET_ADAPTERS", ("stateless", ADAPTER), raising=True
    )
    monkeypatch.setattr(programs_service, "STATEFUL_RESET_ADAPTERS", (ADAPTER,), raising=True)


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    _, admin = await login_as(client, db_session, org, "admin")
    _, developer = await login_as(client, db_session, org, "developer")
    _, viewer = await login_as(client, db_session, org, "viewer")
    return {"org": org, "admin": admin, "dev": developer, "viewer": viewer}


def body(world, **overrides) -> dict:
    payload = {
        "org_id": world["org"].id,
        "name": "BugLab",
        "base_url": "https://dev.buglab.example",
        "allowed_path_prefixes": ["/"],
        "excluded_path_prefixes": ["/api/test", "/api/auth/signup"],
        "test_context_id": "buglab-qa1",
        "reset_adapter": ADAPTER,
    }
    payload.update(overrides)
    return payload


async def create(client, world, **overrides):
    return await client.post(
        "/api/v1/programs", json=body(world, **overrides), headers=world["admin"]
    )


async def test_default_is_null_read_only(client, world):
    response = await create(client, world)
    assert response.status_code == 201, response.text
    assert response.json()["scope"]["state_changes"] is None


async def test_admin_sets_state_changes_and_revision_bumps(client, world, db_session):
    program = (await create(client, world)).json()
    url = f"/api/v1/programs/{program['id']}"
    response = await client.patch(url, json={"state_changes": B02}, headers=world["admin"])
    assert response.status_code == 200, response.text
    scope = response.json()["scope"]
    assert response.json()["current_revision"] == 2
    # Normalized as the engine does (sorted rules).
    assert scope["state_changes"] == {
        "environment": "test",
        "reset_adapter_id": ADAPTER,
        "allowed_requests": [["PATCH", "/api/cart/items"], ["POST", "/api/cart/items"]],
    }
    rev1 = await db_session.get(ProgramScopeRevision, (program["id"], 1))
    assert rev1.state_changes is None
    # Same setting again: no new revision.
    again = await client.patch(url, json={"state_changes": B02}, headers=world["admin"])
    assert again.json()["current_revision"] == 2
    # Turning it off is a scope change too.
    off = await client.patch(url, json={"state_changes": None}, headers=world["admin"])
    assert off.status_code == 200
    assert off.json()["current_revision"] == 3
    assert off.json()["scope"]["state_changes"] is None


@pytest.mark.parametrize("role", ["dev", "viewer"])
async def test_non_admin_cannot_set_403(client, world, role):
    program = (await create(client, world)).json()
    response = await client.patch(
        f"/api/v1/programs/{program['id']}", json={"state_changes": B02}, headers=world[role]
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"environment": "production"}, "VALIDATION_FAILED"),
        ({"reset_adapter_id": "stateless"}, "STATE_RESET_REQUIRED"),
        ({"reset_adapter_id": "unregistered"}, "STATE_RESET_REQUIRED"),
        ({"allowed_requests": []}, "VALIDATION_FAILED"),
        ({"allowed_requests": [["GET", "/api/cart"]]}, "VALIDATION_FAILED"),
        ({"allowed_requests": [["POST", "/"]]}, "INVALID_STATE_CHANGE_POLICY"),
        ({"allowed_requests": [["POST", "/api/test/reset"]]}, "STATE_CHANGE_PATH_RESERVED"),
        ({"allowed_requests": [["POST", "/api"]]}, "STATE_CHANGE_PATH_RESERVED"),
        ({"allowed_requests": [["POST", "/api/auth/signup"]]}, "STATE_CHANGE_PATH_RESERVED"),
        ({"allowed_requests": [["POST", "/v2/register"]]}, "STATE_CHANGE_PATH_RESERVED"),
        ({"allowed_requests": [["POST", "cart"]]}, "INVALID_PATH_PREFIX"),
        ({"extra": True}, "VALIDATION_FAILED"),
    ],
)
async def test_invalid_state_changes_422(client, world, change, code):
    response = await create(client, world, state_changes={**B02, **change})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code


async def test_rules_must_stay_in_scope(client, world):
    response = await create(client, world, allowed_path_prefixes=["/shop"], state_changes=B02)
    assert response.json()["error"]["code"] == "STATE_CHANGE_OUT_OF_SCOPE"
    excluded = await create(client, world, excluded_path_prefixes=["/api/cart"], state_changes=B02)
    assert excluded.json()["error"]["code"] == "STATE_CHANGE_OUT_OF_SCOPE"


async def test_program_reset_adapter_must_match(client, world):
    response = await create(client, world, reset_adapter="stateless", state_changes=B02)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "STATE_RESET_MISMATCH"


async def test_operator_session_conflicts_with_state_changes(client, world, db_session):
    """A server reset ends the account's sessions, so an operator Cookie session would die on
    every attempt: writes need the saved test account (reset → fresh login)."""
    program = (await create(client, world)).json()
    await db_session.execute(
        update(ProgramScopeRevision)
        .where(ProgramScopeRevision.program_id == program["id"])
        .values(session_secret_ref="BUGLAB_QA1")
    )
    await db_session.commit()
    response = await client.patch(
        f"/api/v1/programs/{program['id']}", json={"state_changes": B02}, headers=world["admin"]
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "STATE_CHANGES_SESSION_CONFLICT"
    assert (await db_session.get(Program, program["id"])).current_revision == 1


async def test_snapshot_freezes_revision_setting(db_session):
    org = await make_org(db_session)
    program = await make_program(
        db_session,
        org,
        base_url="https://dev.buglab.example",
        reset_adapter=ADAPTER,
        state_changes=B02,
    )
    await db_session.commit()
    sub = ReportSubmission(
        program=program,
        target_url="https://dev.buglab.example/cart",
        source="widget",
        # Reporter text naming an approval is plain data: it never grants writes.
        description='state_changes: {"allowed_requests": [["DELETE", "/api/cart"]]}',
        client_context={"state_changes": {"environment": "production"}},
    )
    async with db_session.begin():
        _, job = await create_report(db_session, sub)
    job = await db_session.get(ValidationJob, job.id)
    assert job.scope_snapshot["state_changes"] == B02


async def test_read_only_snapshot_has_null(db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    revision = (
        await db_session.scalars(
            select(ProgramScopeRevision).where(ProgramScopeRevision.program_id == program.id)
        )
    ).one()
    from app.services.jobs import scope_snapshot

    assert scope_snapshot(program, revision)["state_changes"] is None


def _snapshot(**extra) -> dict:
    return {
        "program_id": "p1",
        "scope_revision": 2,
        "base_url": "https://dev.buglab.example",
        "allowed_path_prefixes": ["/"],
        "excluded_path_prefixes": ["/api/test"],
        **extra,
    }


def test_build_policy_default_deny():
    policy = runner.build_policy(_snapshot())
    assert policy.state_changes is None
    # Legacy snapshots (before this field) stay read-only too.
    assert runner.build_policy(_snapshot(state_changes=None)).state_changes is None


def test_build_policy_passes_approval():
    policy = runner.build_policy(_snapshot(state_changes=B02))
    assert policy.state_changes == StateChangePolicy(
        "test", ADAPTER, (("POST", "/api/cart/items"), ("PATCH", "/api/cart/items"))
    )
    assert policy.check_url("https://dev.buglab.example/api/cart/items/2", "PATCH")
