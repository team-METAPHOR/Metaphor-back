import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.ids import new_id
from app.models import (
    IdempotencyRecord,
    Organization,
    Program,
    ProgramScopeRevision,
    ProgramWidgetSettings,
    Report,
    User,
    ValidationJob,
)
from tests.conftest import TEST_DATABASE_URL


async def seed(session):
    org = Organization(id=new_id(), name="Acme")
    user = User(id=new_id(), email=f"{new_id()}@example.com", password_hash="x")
    program = Program(id=new_id(), owner_org_id=org.id, name="Shop", status="active")
    session.add_all([org, user])
    await session.flush()
    session.add(program)
    await session.flush()
    session.add(
        ProgramScopeRevision(
            program_id=program.id,
            revision=1,
            base_url="https://staging.example.com",
            allowed_path_prefixes=["/"],
            test_context_id="ctx",
            reset_adapter="stateless",
        )
    )
    await session.flush()
    return org, user, program


def report(org, program, **kwargs):
    values = {
        "id": new_id(),
        "program_id": program.id,
        "org_id": org.id,
        "target_url": "https://staging.example.com/",
        "description": "bug",
    }
    values.update(kwargs)
    return Report(**values)


def job(report_row, status="queued"):
    return ValidationJob(
        id=new_id(),
        report_id=report_row.id,
        org_id=report_row.org_id,
        kind="initial",
        status=status,
        scope_revision=1,
        scope_snapshot={},
    )


async def test_platform_report_requires_author(db_session):
    org, _, program = await seed(db_session)
    db_session.add(report(org, program, source="platform", author_id=None))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_widget_report_allows_null_author_and_defaults(db_session):
    org, _, program = await seed(db_session)
    row = report(org, program, source="widget", public_receipt_id="rcpt_abc")
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    assert row.author_id is None
    assert row.error_type == "manual"
    assert row.origin_kind == "approved"
    assert row.duplicate_count == 0
    assert row.workflow_status == "received"


async def test_report_rejects_unknown_source(db_session):
    org, user, program = await seed(db_session)
    db_session.add(report(org, program, source="email", author_id=user.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_active_job_per_report_index(db_session):
    org, user, program = await seed(db_session)
    row = report(org, program, author_id=user.id)
    db_session.add(row)
    await db_session.flush()
    db_session.add(job(row, "completed"))
    db_session.add(job(row, "queued"))
    await db_session.flush()  # one finished + one active is fine
    db_session.add(job(row, "running"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_job_status_check(db_session):
    org, user, program = await seed(db_session)
    row = report(org, program, author_id=user.id)
    db_session.add(row)
    await db_session.flush()
    db_session.add(job(row, "succeeded"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_idempotency_unique_with_null_user(db_session):
    def record():
        return IdempotencyRecord(
            user_id=None,
            scope="widget:p1",
            key="k1",
            request_hash="h",
            status_code=201,
            response={},
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )

    db_session.add(record())
    await db_session.flush()
    db_session.add(record())
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_widget_settings_defaults_off(db_session):
    _, _, program = await seed(db_session)
    settings = ProgramWidgetSettings(program_id=program.id)
    db_session.add(settings)
    await db_session.flush()
    await db_session.refresh(settings)
    assert settings.widget_enabled is False
    assert settings.public_reporting_enabled is False
    assert settings.auto_detect_enabled is False
    assert settings.live_screenshot_enabled is False
    assert settings.breadcrumbs_enabled is False
    assert settings.allowed_origins == []
    assert settings.public_project_key is None


def test_alembic_upgrade_matches_models():
    """Fresh DB → `alembic upgrade head` → `alembic check` finds no model drift."""
    name = TEST_DATABASE_URL.rsplit("/", 1)[1] + "_migration"
    admin_url = TEST_DATABASE_URL.replace("+psycopg", "")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL.rsplit("/", 1)[0] + "/" + name}
        for command in (["upgrade", "head"], ["check"]):
            result = subprocess.run(
                [sys.executable, "-m", "alembic", *command],
                env=env,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stdout + result.stderr
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
