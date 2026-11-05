"""Origin ownership verification (`/.well-known/metaphor-verify.txt`) and the create_job gate."""

import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.models import OriginVerification, Report, ValidationJob
from app.services import origin_verification
from app.services.intake import ReportSubmission
from app.services.reports import create_report
from app.services.rerun import auto_fresh
from tests.conftest import TEST_DATABASE_URL
from tests.factories import login_as, make_org, make_program, make_user

STAGING = "https://staging.example.com"
LOOPBACK = {"allow_private_targets": True, "private_cidrs": ["127.0.0.1/32"]}


@contextmanager
def verify_site():
    """Loopback server for the verification file. `state` controls the reply."""
    state = {"status": 200, "body": b"", "headers": {}, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"].append({"path": self.path, "host": self.headers.get("Host")})
            body = state["body"]
            self.send_response(state["status"])
            for name, value in state["headers"].items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()


@pytest.fixture
def site():
    with verify_site() as value:
        yield value


@pytest.fixture(autouse=True)
def ip_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "ip_hash_secret", "test-ip-secret")


async def seeded(client, db_session, *, verified=False, **revision):
    org = await make_org(db_session)
    other = await make_org(db_session, name="Other")
    program = await make_program(db_session, org, verified=verified, **revision)
    await db_session.commit()
    _, admin = await login_as(client, db_session, org, "admin")
    _, dev = await login_as(client, db_session, org, "developer")
    _, outsider = await login_as(client, db_session, other, "admin")
    return {"org": org, "program": program, "admin": admin, "dev": dev, "out": outsider}


def url(world, suffix=""):
    return f"/api/v1/programs/{world['program'].id}/origin-verification{suffix}"


async def check(client, world, headers=None):
    return await client.post(url(world, "/check"), headers=headers or world["admin"])


def assert_failed(response, reason):
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert (error["code"], error["detail"]["reason"]) == ("ORIGIN_VERIFY_FAILED", reason)


async def file_report(client, world, target=f"{STAGING}/"):
    return await client.post(
        "/api/v1/reports",
        data={"program_id": world["program"].id, "target_url": target, "description": "bug"},
        headers=world["dev"],
    )


async def enable_widget(client, world) -> str:
    response = await client.patch(
        f"/api/v1/programs/{world['program'].id}/widget-settings",
        json={
            "widget_enabled": True,
            "public_reporting_enabled": True,
            "allowed_origins": [STAGING],
        },
        headers=world["admin"],
    )
    assert response.status_code == 200, response.text
    return response.json()["public_project_key"]


async def patch_scope(client, world, **scope):
    response = await client.patch(
        f"/api/v1/programs/{world['program'].id}", json=scope, headers=world["admin"]
    )
    assert response.status_code == 200, response.text


async def test_verification_token_stable_per_origin(client, db_session):
    world = await seeded(client, db_session)
    first = await client.get(url(world), headers=world["admin"])
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["origin"] == STAGING
    assert body["file_url"] == f"{STAGING}/.well-known/metaphor-verify.txt"
    assert body["verified_at"] is None
    assert len(body["token"]) == 43
    again = await client.get(url(world), headers=world["admin"])
    assert again.json()["token"] == body["token"]

    await patch_scope(client, world, base_url="https://other.example.com")
    moved = (await client.get(url(world), headers=world["admin"])).json()
    assert moved["origin"] == "https://other.example.com"
    assert moved["token"] != body["token"]
    await patch_scope(client, world, base_url=STAGING)
    back = (await client.get(url(world), headers=world["admin"])).json()
    assert back["token"] == body["token"]

    assert (await client.get(url(world), headers=world["dev"])).status_code == 403
    assert (await client.get(url(world), headers=world["out"])).status_code == 404
    assert (await check(client, world, world["dev"])).status_code == 403
    assert (await check(client, world, world["out"])).status_code == 404


async def test_check_success_marks_verified(client, db_session, site):
    base, state = site
    world = await seeded(client, db_session, base_url=base, **LOOPBACK)
    assert (await file_report(client, world, f"{base}/")).status_code == 422
    token = (await client.get(url(world), headers=world["admin"])).json()["token"]
    state["body"] = f"  {token}\n".encode()

    response = await check(client, world)
    assert response.status_code == 200, response.text
    assert response.json()["verified_at"] is not None
    assert state["requests"] == [
        {"path": "/.well-known/metaphor-verify.txt", "host": base.removeprefix("http://")}
    ]
    assert (await file_report(client, world, f"{base}/")).status_code == 201


async def test_check_mismatch_and_404_fail(client, db_session, site):
    base, state = site
    world = await seeded(client, db_session, base_url=base, **LOOPBACK)
    token = (await client.get(url(world), headers=world["admin"])).json()["token"]

    state["body"] = b"someone-else"
    assert_failed(await check(client, world), "MISMATCH")
    state["body"] = token.encode() + b" " * 1024  # over the 1KB read limit
    assert_failed(await check(client, world), "MISMATCH")
    state["status"], state["body"] = 404, token.encode()
    assert_failed(await check(client, world), "NOT_FOUND")

    row = await db_session.get(OriginVerification, (world["program"].id, base))
    await db_session.refresh(row)
    assert row.verified_at is None and row.last_checked_at is not None


async def test_verify_refuses_private_ip_and_redirects(client, db_session, site, monkeypatch):
    base, state = site

    # Loopback target on a customer-configured program: refused before any connection.
    world = await seeded(client, db_session, base_url=base)
    assert_failed(await check(client, world), "PRIVATE_ADDRESS")
    assert state["requests"] == []

    # A public-looking name that resolves to a private address is refused too.
    resolved = {"internal.example.com": "10.0.0.5", "meta.example.com": "169.254.169.254"}

    def fake_resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved[host], port))]

    monkeypatch.setattr(origin_verification, "resolver", fake_resolve)
    for host in resolved:
        await patch_scope(client, world, base_url=f"https://{host}")
        assert_failed(await check(client, world), "PRIVATE_ADDRESS")
    # Link-local (cloud metadata) stays blocked even with operator private-target approval.
    meta = await seeded(client, db_session, base_url="https://meta.example.com", **LOOPBACK)
    assert_failed(await check(client, meta), "PRIVATE_ADDRESS")
    monkeypatch.undo()

    # Redirects are never followed.
    world = await seeded(client, db_session, base_url=base, **LOOPBACK)
    state["status"], state["headers"] = 302, {"Location": "http://10.0.0.5/steal"}
    assert_failed(await check(client, world), "REDIRECT")
    assert len(state["requests"]) == 1

    # Nothing listening.
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    world = await seeded(client, db_session, base_url=f"http://127.0.0.1:{port}", **LOOPBACK)
    assert_failed(await check(client, world), "UNREACHABLE")


async def test_verify_connects_to_the_checked_address(client, db_session, site, monkeypatch):
    """DNS is resolved once; the connection is pinned to that IP with the original Host."""
    base, state = site
    port = base.rsplit(":", 1)[1]
    calls = []

    def resolve_once(host, port, *args, **kwargs):
        calls.append(host)
        if len(calls) > 1:
            raise OSError("second lookup would allow rebinding")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    world = await seeded(client, db_session, base_url=f"http://verify.test:{port}", **LOOPBACK)
    token = (await client.get(url(world), headers=world["admin"])).json()["token"]
    state["body"] = token.encode()
    monkeypatch.setattr(origin_verification, "resolver", resolve_once)
    response = await check(client, world)
    assert response.status_code == 200, response.text
    assert calls == ["verify.test"]
    assert state["requests"][0]["host"] == f"verify.test:{port}"


async def test_check_rate_limited_per_program_429(client, db_session):
    world = await seeded(client, db_session, base_url="http://127.0.0.1:9")
    for _ in range(origin_verification.CHECKS_PER_MINUTE):
        assert_failed(await check(client, world), "PRIVATE_ADDRESS")
    response = await check(client, world)
    assert (response.status_code, response.json()["error"]["code"]) == (
        429,
        "ORIGIN_VERIFY_RATE_LIMITED",
    )


async def test_unverified_program_rejects_jobs_422(client, db_session):
    world = await seeded(client, db_session, allowed_path_prefixes=["/search"])
    response = await file_report(client, world, f"{STAGING}/search")
    assert (response.status_code, response.json()["error"]["code"]) == (
        422,
        "ORIGIN_NOT_VERIFIED",
    )

    key = await enable_widget(client, world)
    widget = await client.post(
        f"/api/v1/widget/reports?project_key={key}",
        data={"payload": json.dumps({"description": "안 돼요", "page_url": f"{STAGING}/search"})},
        headers={"Origin": STAGING},
    )
    assert (widget.status_code, widget.json()["error"]["code"]) == (422, "ORIGIN_NOT_VERIFIED")
    assert await db_session.scalar(select(func.count()).select_from(Report)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ValidationJob)) == 0


async def test_widget_config_reporting_disabled_until_verified(client, db_session):
    world = await seeded(client, db_session)
    key = await enable_widget(client, world)

    async def config():
        response = await client.get(
            f"/api/v1/widget/config?project_key={key}", headers={"Origin": STAGING}
        )
        assert response.status_code == 200, response.text
        return response.json()

    assert (await config())["reporting_enabled"] is False
    assert (await client.get(url(world), headers=world["admin"])).status_code == 200
    assert (await config())["reporting_enabled"] is False
    await db_session.execute(
        update(OriginVerification)
        .where(OriginVerification.program_id == world["program"].id)
        .values(verified_at=datetime.now(UTC))
    )
    await db_session.commit()
    assert (await config())["reporting_enabled"] is True


async def test_scope_origin_change_requires_reverification(client, db_session):
    world = await seeded(client, db_session, verified=True)
    assert (await file_report(client, world)).status_code == 201

    await patch_scope(client, world, base_url="https://new.example.com")
    response = await file_report(client, world, "https://new.example.com/")
    assert (response.status_code, response.json()["error"]["code"]) == (
        422,
        "ORIGIN_NOT_VERIFIED",
    )
    body = (await client.get(url(world), headers=world["admin"])).json()
    assert (body["origin"], body["verified_at"]) == ("https://new.example.com", None)
    old = await db_session.get(OriginVerification, (world["program"].id, STAGING))
    assert old.verified_at is not None  # the old origin's record is kept

    await patch_scope(client, world, base_url=STAGING)
    report = await file_report(client, world)
    assert report.status_code == 201, report.text


async def test_auto_fresh_skips_unverified_origin(db_session, session_factory):
    org = await make_org(db_session)
    user = await make_user(db_session, org, "admin")
    program = await make_program(db_session, org)
    await db_session.commit()
    sub = ReportSubmission(
        program=program,
        target_url=f"{STAGING}/",
        source="platform",
        author_id=user.id,
        description="결제 버튼이 반응하지 않아요",
    )
    async with db_session.begin():
        _, job = await create_report(db_session, sub)
    job.status = job.verdict = "inconclusive"
    await db_session.execute(
        update(OriginVerification)
        .where(OriginVerification.program_id == program.id)
        .values(verified_at=None)
    )
    await db_session.commit()
    async with session_factory() as session:
        assert await auto_fresh(session, job.id) is None
    assert await db_session.scalar(select(func.count()).select_from(ValidationJob)) == 1


def test_existing_programs_backfilled_verified():
    """0004 data → upgrade head: each program's current origin is verified (demo keeps running)."""
    name = TEST_DATABASE_URL.rsplit("/", 1)[1] + "_backfill"
    admin_url = TEST_DATABASE_URL.replace("+psycopg", "")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')
    url_ = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/" + name
    env = {**os.environ, "DATABASE_URL": url_}

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr

    try:
        alembic("upgrade", "0004")
        with psycopg.connect(url_.replace("+psycopg", ""), autocommit=True) as conn:
            conn.execute("INSERT INTO organizations (id, name) VALUES ('o1', 'Acme')")
            conn.execute(
                "INSERT INTO programs (id, owner_org_id, name, current_revision)"
                " VALUES ('p1', 'o1', 'Shop', 2)"
            )
            for revision, base in ((1, "https://old.example.com/"), (2, f"{STAGING}/")):
                conn.execute(
                    "INSERT INTO program_scope_revisions"
                    " (program_id, revision, base_url, reset_adapter)"
                    " VALUES ('p1', %s, %s, 'stateless')",
                    (revision, base),
                )
        alembic("upgrade", "head")
        with psycopg.connect(url_.replace("+psycopg", ""), autocommit=True) as conn:
            rows = conn.execute(
                "SELECT program_id, origin, length(token), verified_at IS NOT NULL"
                " FROM origin_verifications"
            ).fetchall()
        assert rows == [("p1", STAGING, 43, True)]
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
