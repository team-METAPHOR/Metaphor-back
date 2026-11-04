from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.errors import ApiError
from app.core.pagination import decode_cursor, encode_cursor
from app.models import AuditEvent, IdempotencyRecord, Report, ValidationJob
from app.services.audit import record_audit
from app.services.idempotency import run_once
from app.services.jobs import create_job
from app.services.limits import enforce_rate_limit
from tests.factories import make_org, make_program, make_report, make_user


@pytest.fixture
async def world(db_session):
    org = await make_org(db_session)
    user = await make_user(db_session, org, "admin")
    program = await make_program(db_session, org, daily_job_limit=3)
    report = await make_report(db_session, org, program, user)
    await db_session.commit()
    return org, user, program, report


# --- idempotency -----------------------------------------------------------


async def test_run_once_replays_same_request(db_session):
    calls = []

    async def fn():
        calls.append(1)
        return 201, {"id": f"r{len(calls)}"}

    first = await run_once(db_session, user_id=None, scope="s", key="k", request_hash="h", fn=fn)
    second = await run_once(db_session, user_id=None, scope="s", key="k", request_hash="h", fn=fn)
    assert first == second == (201, {"id": "r1"})
    assert len(calls) == 1


async def test_run_once_same_key_different_body_409(db_session):
    async def fn():
        return 201, {}

    await run_once(db_session, user_id=None, scope="s", key="k", request_hash="h1", fn=fn)
    with pytest.raises(ApiError) as error:
        await run_once(db_session, user_id=None, scope="s", key="k", request_hash="h2", fn=fn)
    assert (error.value.status, error.value.code) == (409, "IDEMPOTENCY_KEY_REUSED")


async def test_run_once_without_key_always_runs(db_session):
    calls = []

    async def fn():
        calls.append(1)
        return 201, {}

    for _ in range(2):
        await run_once(db_session, user_id=None, scope="s", key=None, request_hash="h", fn=fn)
    assert len(calls) == 2
    assert (await db_session.scalar(select(func.count()).select_from(IdempotencyRecord))) == 0


async def test_run_once_rejects_bad_key(db_session):
    async def fn():
        return 201, {}

    with pytest.raises(ApiError) as error:
        await run_once(db_session, user_id=None, scope="s", key="x" * 129, request_hash="h", fn=fn)
    assert error.value.code == "IDEMPOTENCY_KEY_INVALID"


async def test_run_once_expired_record_is_replaced(db_session):
    db_session.add(
        IdempotencyRecord(
            user_id=None,
            scope="s",
            key="k",
            request_hash="old",
            status_code=201,
            response={"id": "old"},
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    await db_session.flush()

    async def fn():
        return 201, {"id": "new"}

    result = await run_once(db_session, user_id=None, scope="s", key="k", request_hash="h", fn=fn)
    assert result == (201, {"id": "new"})


async def test_run_once_scoped_per_user(db_session, world):
    _, user, _, _ = world

    async def fn():
        return 201, {"who": "x"}

    await run_once(db_session, user_id=user.id, scope="s", key="k", request_hash="h1", fn=fn)
    # Same scope/key for anonymous is a different record, not a conflict.
    await run_once(db_session, user_id=None, scope="s", key="k", request_hash="h2", fn=fn)


# --- rate limit / audit / cursor -------------------------------------------


async def test_rate_limit_429_at_threshold(db_session, world):
    org, user, _, _ = world
    stmt = select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "t")
    for _ in range(2):
        await enforce_rate_limit(db_session, stmt, limit=2, code="T_LIMIT")
        await record_audit(db_session, actor_id=user.id, org_id=org.id, action="t", target="x")
    with pytest.raises(ApiError) as error:
        await enforce_rate_limit(db_session, stmt, limit=2, code="T_LIMIT")
    assert (error.value.status, error.value.code) == (429, "T_LIMIT")


async def test_record_audit_persists(db_session, world):
    org, user, _, _ = world
    await record_audit(
        db_session, actor_id=user.id, org_id=org.id, action="a", target="t", detail={"k": 1}
    )
    row = await db_session.scalar(select(AuditEvent))
    assert (row.action, row.target, row.detail) == ("a", "t", {"k": 1})


def test_cursor_roundtrip_and_invalid():
    moment = datetime(2026, 11, 5, 1, 2, 3, 456789, tzinfo=UTC)
    assert decode_cursor(encode_cursor(moment, "abc")) == (moment, "abc")
    with pytest.raises(ApiError) as error:
        decode_cursor("not-a-cursor")
    assert (error.value.status, error.value.code) == (400, "INVALID_CURSOR")


# --- create_job ------------------------------------------------------------


async def test_create_job_snapshots_scope_and_links_report(db_session, world):
    _, _, program, report = world
    async with db_session.begin():
        job = await create_job(db_session, report, "initial")
    assert job.status == "queued"
    assert job.scope_revision == 1
    snapshot = job.scope_snapshot
    assert snapshot["program_id"] == program.id
    assert snapshot["base_url"] == "https://staging.example.com"
    assert snapshot["test_context_id"] == "ctx-1"
    assert "session_headers" not in snapshot
    refreshed = await db_session.get(Report, report.id, populate_existing=True)
    assert refreshed.latest_job_id == job.id


async def test_create_job_second_active_job_409(db_session, world):
    *_, report = world
    async with db_session.begin():
        await create_job(db_session, report, "initial")
    with pytest.raises(ApiError) as error:
        async with db_session.begin():
            await create_job(db_session, report, "fresh")
    assert (error.value.status, error.value.code) == (409, "ACTIVE_JOB_EXISTS")


async def test_create_job_daily_limit_429(db_session, world):
    org, user, program, _ = world  # daily_job_limit=3
    for _ in range(3):
        report = await make_report(db_session, org, program, user)
        await db_session.commit()
        async with db_session.begin():
            await create_job(db_session, report, "initial")
    report = await make_report(db_session, org, program, user)
    await db_session.commit()
    with pytest.raises(ApiError) as error:
        async with db_session.begin():
            await create_job(db_session, report, "initial")
    assert (error.value.status, error.value.code) == (429, "DAILY_LIMIT_EXCEEDED")
    count = await db_session.scalar(select(func.count()).select_from(ValidationJob))
    assert count == 3


async def test_create_job_uses_given_snapshot_for_retest(db_session, world):
    *_, report = world
    snapshot = {"program_id": report.program_id, "scope_revision": 1, "app_version": "b2"}
    async with db_session.begin():
        job = await create_job(db_session, report, "retest", snapshot=snapshot, app_version="b2")
    assert job.scope_snapshot == snapshot
    assert job.app_version == "b2"


async def test_create_job_rejects_unknown_kind(db_session, world):
    *_, report = world
    with pytest.raises(ValueError):
        async with db_session.begin():
            await create_job(db_session, report, "rerun")
