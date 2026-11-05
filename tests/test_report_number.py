import asyncio
import os
import subprocess
import sys

import psycopg
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Organization, Report
from tests.conftest import TEST_DATABASE_URL
from tests.factories import login_as, make_org, make_program
from tests.test_reports_api import submit
from tests.test_widget_public import STAGING, payload, post


@pytest.fixture(autouse=True)
def ip_secret(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "ip_hash_secret", "test-ip-secret-0123456789abcdef0123456789"
    )


async def org_world(client, db_session, name):
    org = await make_org(db_session, name)
    program = await make_program(db_session, org)
    await db_session.commit()
    _, admin_h = await login_as(client, db_session, org, "admin")
    return {"org": org, "program": program, "admin_h": admin_h}


@pytest.fixture
async def world(client, db_session):
    return await org_world(client, db_session, "Acme")


async def numbers(db_session, org_id):
    rows = await db_session.scalars(
        select(Report.number).where(Report.org_id == org_id).order_by(Report.created_at)
    )
    return list(rows.all())


async def seq(db_session, org_id):
    org = await db_session.get(Organization, org_id, populate_existing=True)
    return org.report_seq


async def test_numbers_sequential_per_org(client, db_session, world):
    other = await org_world(client, db_session, "Other")
    for i in range(3):
        created = await submit(client, world["admin_h"], world["program"].id, description=f"a{i}")
        assert created.status_code == 201, created.text
    for i in range(2):
        await submit(client, other["admin_h"], other["program"].id, description=f"b{i}")
    assert await numbers(db_session, world["org"].id) == [1, 2, 3]
    assert await numbers(db_session, other["org"].id) == [1, 2]
    assert (await seq(db_session, world["org"].id), await seq(db_session, other["org"].id)) == (
        3,
        2,
    )


async def test_number_in_list_and_detail(client, world):
    report_id = (
        await submit(client, world["admin_h"], world["program"].id, description="x")
    ).json()["id"]
    detail = await client.get(f"/api/v1/reports/{report_id}", headers=world["admin_h"])
    assert detail.json()["number"] == 1
    (item,) = (await client.get("/api/v1/reports", headers=world["admin_h"])).json()["items"]
    assert item["number"] == 1


async def test_concurrent_creates_get_distinct_numbers(client, db_session, world):
    # Different users, so the per-user report lock does not serialize them for us.
    users = [await login_as(client, db_session, world["org"], "developer") for _ in range(6)]
    results = await asyncio.gather(
        *(
            submit(client, h, world["program"].id, description=f"동시 {i}")
            for i, (_, h) in enumerate(users)
        )
    )
    assert [r.status_code for r in results] == [201] * 6
    assert sorted(await numbers(db_session, world["org"].id)) == [1, 2, 3, 4, 5, 6]
    assert await seq(db_session, world["org"].id) == 6


async def test_widget_and_platform_share_sequence(client, db_session, world):
    response = await client.patch(
        f"/api/v1/programs/{world['program'].id}/widget-settings",
        json={
            "widget_enabled": True,
            "public_reporting_enabled": True,
            "allowed_origins": [STAGING],
        },
        headers=world["admin_h"],
    )
    key = response.json()["public_project_key"]
    await submit(client, world["admin_h"], world["program"].id, description="platform")
    widget = await post(client, key, payload())
    assert widget.status_code == 201, widget.text
    await submit(client, world["admin_h"], world["program"].id, description="platform 2")
    rows = (
        await db_session.execute(
            select(Report.source, Report.number)
            .where(Report.org_id == world["org"].id)
            .order_by(Report.number)
        )
    ).all()
    assert [tuple(r) for r in rows] == [("platform", 1), ("widget", 2), ("platform", 3)]


# --- GET /orgs/{org_id}/reports/{number} --------------------------------------


async def test_get_by_number_members_only(client, db_session, world):
    report_id = (
        await submit(client, world["admin_h"], world["program"].id, description="x")
    ).json()["id"]
    url = f"/api/v1/orgs/{world['org'].id}/reports/1"
    _, dev_h = await login_as(client, db_session, world["org"], "developer")
    found = await client.get(url, headers=dev_h)
    assert found.status_code == 200, found.text
    assert (found.json()["id"], found.json()["number"]) == (report_id, 1)
    detail = await client.get(f"/api/v1/reports/{report_id}", headers=dev_h)
    assert found.json() == detail.json()

    missing = await client.get(f"/api/v1/orgs/{world['org'].id}/reports/2", headers=dev_h)
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "NOT_FOUND")
    other = await org_world(client, db_session, "Other")
    for headers in (other["admin_h"], (await login_as(client, db_session))[1]):
        assert (await client.get(url, headers=headers)).status_code == 404
    # Another org's own report #1 is not reachable through this org's path.
    assert (
        await client.get(f"/api/v1/orgs/{world['org'].id}/reports/1", headers=other["admin_h"])
    ).status_code == 404


async def test_get_by_number_rejects_non_positive(client, world):
    response = await client.get(
        f"/api/v1/orgs/{world['org'].id}/reports/0", headers=world["admin_h"]
    )
    assert response.status_code == 422


# --- migration 0009 backfill ----------------------------------------------------


def test_migration_backfills_numbers_per_org():
    name = TEST_DATABASE_URL.rsplit("/", 1)[1] + "_number"
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
        alembic("upgrade", "0008")
        with psycopg.connect(url_.replace("+psycopg", ""), autocommit=True) as conn:
            for org in ("o1", "o2", "o3"):
                conn.execute("INSERT INTO organizations (id, name) VALUES (%s, 'x')", (org,))
                conn.execute(
                    "INSERT INTO programs (id, owner_org_id, name) VALUES (%s, %s, 'S')",
                    ("p" + org, org),
                )
            # (id, org, created_at): r1b/r1a tie on created_at → ordered by id.
            for rid, org, at in (
                ("r1c", "o1", "2026-01-03"),
                ("r1b", "o1", "2026-01-01"),
                ("r1a", "o1", "2026-01-01"),
                ("r2a", "o2", "2026-01-02"),
            ):
                conn.execute(
                    "INSERT INTO reports (id, program_id, org_id, source, target_url, created_at)"
                    " VALUES (%s, %s, %s, 'widget', 'https://x/', %s)",
                    (rid, "p" + org, org, at),
                )
        alembic("upgrade", "head")
        with psycopg.connect(url_.replace("+psycopg", ""), autocommit=True) as conn:
            rows = conn.execute("SELECT id, number FROM reports ORDER BY id").fetchall()
            assert rows == [("r1a", 1), ("r1b", 2), ("r1c", 3), ("r2a", 1)]
            seqs = conn.execute("SELECT id, report_seq FROM organizations ORDER BY id").fetchall()
            assert seqs == [("o1", 3), ("o2", 1), ("o3", 0)]
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute("UPDATE reports SET number = 1 WHERE id = 'r1b'")
        alembic("downgrade", "0008")
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
