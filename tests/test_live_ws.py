"""`WS /api/v1/validation-jobs/{id}/live` against the real relay table (no engine run)."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.v1.endpoints import live_preview as endpoint
from app.db.session import get_db_session
from app.main import app
from app.services import live_preview as service
from app.worker import live, queue
from tests.conftest import TEST_DATABASE_URL
from tests.factories import PLATFORM_ORIGIN, login_as, make_org, make_program
from tests.test_live_relay import engine_stream
from tests.test_reports_api import submit


@pytest.fixture
async def world(client, db_session, worker_env):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    await db_session.commit()
    _, dev_h = await login_as(client, db_session, org, "developer")
    viewer, viewer_h = await login_as(client, db_session, org, "viewer")
    other = await make_org(db_session, name="Other")
    await db_session.commit()
    _, stranger_h = await login_as(client, db_session, other, "admin")
    body = (await submit(client, dev_h, program.id, description="버그")).json()
    return SimpleNamespace(
        job_id=body["validation_job_id"],
        org_id=org.id,
        viewer=viewer,
        viewer_h=viewer_h,
        dev_h=dev_h,
        stranger_h=stranger_h,
    )


@pytest.fixture
def ws(monkeypatch):
    """TestClient runs the app on its own loop: give it sessions from a loop-agnostic engine."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    monkeypatch.setattr(endpoint, "POLL_SECONDS", 0.05)
    app.dependency_overrides[get_db_session] = override
    with TestClient(app) as test_client:
        yield test_client


def connect(test_client, job_id, headers, origin=PLATFORM_ORIGIN):
    headers = {"cookie": headers["Cookie"]} | ({"origin": origin} if origin else {})
    return test_client.websocket_connect(f"/api/v1/validation-jobs/{job_id}/live", headers=headers)


def receive(socket):
    message = socket.receive()
    if message.get("bytes") is not None:
        return message["bytes"]
    if message["type"] == "websocket.close":
        raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
    return json.loads(message["text"])


def close_code(test_client, job_id, headers, **kwargs):
    with pytest.raises(WebSocketDisconnect) as closed:
        with connect(test_client, job_id, headers, **kwargs) as socket:
            while True:
                receive(socket)
    return closed.value.code, closed.value.reason


async def in_thread(fn, *args):
    return await asyncio.to_thread(fn, *args)


async def test_origin_session_and_org_are_checked(world, ws):
    def run():
        bad_origin = close_code(ws, world.job_id, world.viewer_h, origin="https://evil.example")
        no_origin = close_code(ws, world.job_id, world.viewer_h, origin=None)
        anonymous = close_code(ws, world.job_id, {"Cookie": "sid=nope"})
        stranger = close_code(ws, world.job_id, world.stranger_h)
        missing = close_code(ws, "0" * 32, world.viewer_h)
        return bad_origin, no_origin, anonymous, stranger, missing

    bad_origin, no_origin, anonymous, stranger, missing = await in_thread(run)
    assert bad_origin[0] == no_origin[0] == 1008
    assert anonymous == (4401, "UNAUTHENTICATED")
    # Another org's job and a job that does not exist look the same.
    assert stranger == missing == (4404, "NOT_FOUND")


async def test_queued_job_reports_state_to_a_viewer(world, ws):
    def run():
        with connect(ws, world.job_id, world.viewer_h) as socket:
            return receive(socket)

    state = await in_thread(run)
    assert state == {
        "schema_version": "live-relay-1",
        "kind": "relay_state",
        "job_id": world.job_id,
        "report_id": state["report_id"],
        "job_status": "queued",
        "preview": "queued",
        "stream": 0,
        "condition_id": None,
        "updated_at": None,
    }


async def test_running_job_streams_messages_frame_then_end(world, ws, conn):
    assert queue.claim_next(conn, "w1", lease_seconds=30) == world.job_id
    stream = engine_stream()
    live.publish(conn, world.job_id, enabled=True, stream=1, messages=stream.read(0))

    def run():
        received = []
        with connect(ws, world.job_id, world.viewer_h) as socket:
            for _ in range(4):  # state, status, progress, frame
                received.append(receive(socket))
            stream._end("completed")
            live.publish(conn, world.job_id, enabled=True, stream=1, messages=stream.read(0))
            received.append(receive(socket))  # woken by NOTIFY (or the poll)
            queue.finish_if_active(conn, world.job_id, "completed", None)
            live.delete(conn, world.job_id)
            received.append(receive(socket))
            with pytest.raises(WebSocketDisconnect) as closed:
                receive(socket)
        return received, closed.value.code

    received, code = await in_thread(run)
    state, status, progress, frame, end, ended = received
    assert (state["preview"], state["stream"]) == ("live", 1)
    assert status["kind"] == "status" and progress["kind"] == "progress"
    size = int.from_bytes(frame[:4], "big")
    header = json.loads(frame[4 : 4 + size])
    assert header["kind"] == "frame" and header["job_id"] == world.job_id
    assert frame[4 + size : 4 + size + 2] == b"\xff\xd8"
    assert end["kind"] == "end" and end["job_id"] == world.job_id
    assert status["sequence"] < progress["sequence"] < header["sequence"] < end["sequence"]
    assert (ended["preview"], ended["job_status"], code) == ("ended", "completed", 1000)
    assert "e" * 32 not in json.dumps([status, progress, end, header])


async def test_reconnect_restores_latest_state(world, ws, conn):
    assert queue.claim_next(conn, "w1", lease_seconds=30) == world.job_id
    live.publish(conn, world.job_id, enabled=True, stream=1, messages=engine_stream().read(0))

    def run():
        kinds = []
        for _ in range(2):
            with connect(ws, world.job_id, world.viewer_h) as socket:
                got = [receive(socket) for _ in range(4)]
                kinds.append([g["kind"] if isinstance(g, dict) else "frame" for g in got])
        return kinds

    first, second = await in_thread(run)
    assert first == second == ["relay_state", "status", "progress", "frame"]


async def test_revoked_membership_closes_the_connection(world, ws, conn, monkeypatch):
    monkeypatch.setattr(endpoint, "RECHECK_SECONDS", 0)

    def run():
        with connect(ws, world.job_id, world.viewer_h) as socket:
            assert receive(socket)["preview"] == "queued"
            conn.execute(
                "UPDATE memberships SET is_active = false WHERE user_id = %s", (world.viewer.id,)
            )
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    receive(socket)
        return closed.value.code

    assert await in_thread(run) == 4404


async def test_connection_limit_per_user(world, ws, monkeypatch):
    monkeypatch.setattr(service, "connections", service.ConnectionLimit(limit=1))

    def run():
        with connect(ws, world.job_id, world.viewer_h) as socket:
            receive(socket)
            return close_code(ws, world.job_id, world.viewer_h)

    assert await in_thread(run) == (4429, "TOO_MANY_CONNECTIONS")


async def test_cancel_drops_the_preview(client, world, conn):
    assert queue.claim_next(conn, "w1", lease_seconds=30) == world.job_id
    live.publish(conn, world.job_id, enabled=True, stream=1, messages=engine_stream().read(0))
    response = await client.post(
        f"/api/v1/validation-jobs/{world.job_id}/cancel", headers=world.dev_h
    )
    assert response.json()["status"] == "cancelling"
    row = conn.execute("SELECT 1 FROM live_previews WHERE job_id = %s", (world.job_id,))
    assert row.fetchone() is None


NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


def snap(**fields):
    values = {
        "status": "running",
        "started_at": NOW,
        "lease_expired": False,
        "now": NOW + timedelta(seconds=1),
        "enabled": True,
        "stream": 1,
        "version": 3,
        "updated_at": NOW,
    }
    return SimpleNamespace(**(values | fields))


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"status": "queued"}, "queued"),
        ({"status": "cancelling"}, "cancelling"),
        ({"status": "needs_info"}, "ended"),
        ({"lease_expired": True}, "worker_lost"),
        ({"version": None}, "starting"),
        ({"version": None, "now": NOW + timedelta(seconds=30)}, "relay_unavailable"),
        ({"enabled": False}, "disabled"),
        ({"now": NOW + timedelta(seconds=30)}, "relay_unavailable"),
        ({"stream": 0}, "waiting"),
        ({}, "live"),
    ],
)
def test_preview_states(fields, expected):
    assert service.preview_state(snap(**fields)) == expected


async def test_notifier_wakes_only_that_jobs_subscribers(world, conn):
    from app.integrations.live_notify import LiveNotifier

    notifier = LiveNotifier(TEST_DATABASE_URL.replace("+psycopg", ""))
    mine, other = asyncio.Event(), asyncio.Event()
    notifier.subscribe(world.job_id, mine)
    notifier.subscribe("0" * 32, other)
    try:
        for _ in range(50):  # until LISTEN is in place
            conn.execute("SELECT pg_notify('live_preview', %s)", (world.job_id,))
            try:
                await asyncio.wait_for(mine.wait(), 0.1)
                break
            except TimeoutError:
                continue
        assert mine.is_set() and not other.is_set()
    finally:
        await notifier.close()
