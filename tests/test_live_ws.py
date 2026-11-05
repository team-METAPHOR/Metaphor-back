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
        program_id=program.id,
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


# --- one-time connection tickets (issue #107) -------------------------------

PROTOCOL = "metaphor-live.v1"


async def issue(client, job_id, headers):
    return await client.post(f"/api/v1/validation-jobs/{job_id}/live-ticket", headers=headers)


def connect_ticket(test_client, job_id, ticket, protocols=None, origin=PLATFORM_ORIGIN):
    """Direct API connection: no cookie, the ticket rides in Sec-WebSocket-Protocol."""
    headers = {"origin": origin} if origin else {}
    protocols = protocols if protocols is not None else [PROTOCOL, f"ticket.{ticket}"]
    return test_client.websocket_connect(
        f"/api/v1/validation-jobs/{job_id}/live", headers=headers, subprotocols=protocols
    )


def ticket_close(test_client, job_id, ticket, **kwargs):
    with pytest.raises(WebSocketDisconnect) as closed:
        with connect_ticket(test_client, job_id, ticket, **kwargs) as socket:
            while True:
                receive(socket)
    return closed.value.code, closed.value.reason


async def test_ticket_is_issued_without_cache_and_connects(client, world, ws, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "public_api_base", "https://api.example.com")
    response = await issue(client, world.job_id, world.viewer_h)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"websocket_url", "ticket", "expires_at", "subprotocol"}
    assert body["websocket_url"] == (
        f"wss://api.example.com/api/v1/validation-jobs/{world.job_id}/live"
    )
    assert body["subprotocol"] == PROTOCOL
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(0) < expires - datetime.now(UTC) <= timedelta(seconds=30)
    ticket = body["ticket"]

    def run():
        with connect_ticket(ws, world.job_id, ticket) as socket:
            return receive(socket), socket.accepted_subprotocol, socket.extra_headers

    state, selected, extra = await in_thread(run)
    assert state["kind"] == "relay_state" and state["job_id"] == world.job_id
    # Only the fixed protocol is selected; the ticket is never echoed back.
    assert selected == PROTOCOL
    assert ticket not in repr(extra)
    stored = await asyncio.to_thread(_ticket_rows)
    assert stored and all(ticket not in repr(row) for row in stored)  # only a hash is kept


def _ticket_rows():
    import psycopg

    with psycopg.connect(TEST_DATABASE_URL.replace("+psycopg", "")) as c:
        return c.execute("SELECT * FROM live_tickets").fetchall()


async def test_ticket_url_is_null_without_public_api_base(client, world, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "public_api_base", "")
    body = (await issue(client, world.job_id, world.viewer_h)).json()
    assert body["websocket_url"] is None and body["ticket"]


async def test_ticket_issue_checks_session_origin_and_org(client, world):
    anonymous = await issue(client, world.job_id, {"Origin": PLATFORM_ORIGIN})
    no_origin = await issue(client, world.job_id, {"Cookie": world.viewer_h["Cookie"]})
    stranger = await issue(client, world.job_id, world.stranger_h)
    missing = await issue(client, "0" * 32, world.viewer_h)
    assert anonymous.status_code == 401
    assert (no_origin.status_code, no_origin.json()["error"]["code"]) == (403, "CSRF_REJECTED")
    # Another org's job and a job that does not exist look the same.
    assert stranger.status_code == missing.status_code == 404
    assert stranger.json()["error"]["code"] == missing.json()["error"]["code"] == "NOT_FOUND"


async def test_ticket_issue_is_rate_limited_per_user(client, world, monkeypatch):
    monkeypatch.setattr(service, "TICKETS_PER_MINUTE", 2)
    codes = [(await issue(client, world.job_id, world.viewer_h)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    other_user = await issue(client, world.job_id, world.dev_h)
    assert other_user.status_code == 200


async def test_ticket_is_single_use(client, world, ws):
    ticket = (await issue(client, world.job_id, world.viewer_h)).json()["ticket"]

    def run():
        with connect_ticket(ws, world.job_id, ticket) as socket:
            receive(socket)
        return ticket_close(ws, world.job_id, ticket)

    assert await in_thread(run) == (4401, "TICKET_INVALID")


async def test_expired_ticket_is_rejected(client, world, ws, conn):
    ticket = (await issue(client, world.job_id, world.viewer_h)).json()["ticket"]
    conn.execute("UPDATE live_tickets SET expires_at = now() - interval '1 second'")
    assert await in_thread(ticket_close, ws, world.job_id, ticket) == (4401, "TICKET_INVALID")


async def test_unknown_ticket_is_rejected(world, ws):
    assert await in_thread(ticket_close, ws, world.job_id, "x" * 43) == (4401, "TICKET_INVALID")


async def test_ticket_is_bound_to_its_job_and_org(client, db_session, world, ws, conn):
    other_job = (await submit(client, world.dev_h, world.program_id, description="둘")).json()
    ticket = (await issue(client, world.job_id, world.viewer_h)).json()["ticket"]
    wrong_job = await in_thread(ticket_close, ws, other_job["validation_job_id"], ticket)
    assert wrong_job == (4404, "NOT_FOUND")
    # A ticket presented on the wrong job is burned, not left for another try.
    assert await in_thread(ticket_close, ws, world.job_id, ticket) == (4401, "TICKET_INVALID")

    other_org = await make_org(db_session, name="Third")
    await db_session.commit()
    ticket = (await issue(client, world.job_id, world.viewer_h)).json()["ticket"]
    conn.execute("UPDATE live_tickets SET org_id = %s WHERE used_at IS NULL", (other_org.id,))
    assert await in_thread(ticket_close, ws, world.job_id, ticket) == (4404, "NOT_FOUND")


async def test_ticket_of_a_revoked_session_or_member_is_rejected(client, world, ws, conn):
    ticket = (await issue(client, world.job_id, world.viewer_h)).json()["ticket"]
    conn.execute("DELETE FROM sessions WHERE user_id = %s", (world.viewer.id,))
    assert await in_thread(ticket_close, ws, world.job_id, ticket) == (4401, "TICKET_INVALID")

    ticket = (await issue(client, world.job_id, world.dev_h)).json()["ticket"]
    conn.execute("UPDATE memberships SET is_active = false WHERE role = 'developer'")
    assert await in_thread(ticket_close, ws, world.job_id, ticket) == (4404, "NOT_FOUND")


async def test_concurrent_handshakes_consume_a_ticket_once(client, world, session_factory):
    ticket = (await issue(client, world.job_id, world.viewer_h)).json()["ticket"]

    async def attempt():
        async with session_factory() as db:
            try:
                return await service.consume_ticket(db, ticket, world.job_id)
            except Exception as error:  # noqa: BLE001 - counted below
                return error

    results = await asyncio.gather(*(attempt() for _ in range(8)))
    granted = [r for r in results if not isinstance(r, Exception)]
    assert len(granted) == 1
    assert all(getattr(r, "code", None) == "TICKET_INVALID" for r in results if r not in granted)


async def test_session_revoked_mid_connection_closes_ticket_viewer(
    client, world, ws, conn, monkeypatch
):
    monkeypatch.setattr(endpoint, "RECHECK_SECONDS", 0)
    ticket = (await issue(client, world.job_id, world.viewer_h)).json()["ticket"]

    def run():
        with connect_ticket(ws, world.job_id, ticket) as socket:
            assert receive(socket)["preview"] == "queued"
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (world.viewer.id,))
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    receive(socket)
        return closed.value.code, closed.value.reason

    assert await in_thread(run) == (4401, "UNAUTHENTICATED")


async def test_malformed_ticket_handshakes_are_refused(client, world, ws):
    ticket = (await issue(client, world.job_id, world.viewer_h)).json()["ticket"]

    def run():
        no_protocol = ticket_close(ws, world.job_id, ticket, protocols=[f"ticket.{ticket}"])
        two = ticket_close(
            ws, world.job_id, ticket, protocols=[PROTOCOL, f"ticket.{ticket}", "ticket.b"]
        )
        bad_origin = ticket_close(ws, world.job_id, ticket, origin="https://evil.example")
        return no_protocol[0], two[0], bad_origin[0]

    assert await in_thread(run) == (1008, 1008, 1008)


async def test_ticket_never_reaches_logs(client, world, ws, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    ticket = (await issue(client, world.job_id, world.viewer_h)).json()["ticket"]

    def run():
        with connect_ticket(ws, world.job_id, ticket) as socket:
            receive(socket)
        return ticket_close(ws, world.job_id, ticket)  # rejected reuse logs nothing either

    await in_thread(run)
    sid = world.viewer_h["Cookie"].removeprefix("sid=")
    assert ticket not in caplog.text and sid not in caplog.text


async def test_cookie_connection_may_offer_the_protocol(world, ws):
    def run():
        with ws.websocket_connect(
            f"/api/v1/validation-jobs/{world.job_id}/live",
            headers={"cookie": world.viewer_h["Cookie"], "origin": PLATFORM_ORIGIN},
            subprotocols=[PROTOCOL],
        ) as socket:
            return receive(socket)["kind"], socket.accepted_subprotocol

    assert await in_thread(run) == ("relay_state", PROTOCOL)
