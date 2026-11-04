import hashlib
import os
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.ids import new_id
from app.integrations import storage
from app.models import Artifact, IdempotencyRecord
from app.worker import purge
from tests.conftest import TEST_DATABASE_URL
from tests.factories import make_org, make_program, make_report

OLD = time.time() - 2 * 24 * 3600


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(s, "database_url", TEST_DATABASE_URL)
    return s


@pytest.fixture
async def report(db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    report = await make_report(db_session, org, program)
    await db_session.commit()
    return report


def run_purge():
    with psycopg.connect(TEST_DATABASE_URL.replace("+psycopg", ""), autocommit=True) as conn:
        return purge.purge(conn)


def write(report, *parts: str, data: bytes = b"x", age: float | None = OLD):
    path = storage.safe_path(report.org_id, report.id, *parts)
    storage.write_private(path, data)
    if age is not None:
        os.utime(path, (age, age))
    return path


async def add_artifact(db_session, report, path, *, expires_in: timedelta, key=None) -> str:
    artifact_id = new_id()
    db_session.add(
        Artifact(
            id=artifact_id,
            org_id=report.org_id,
            report_id=report.id,
            kind="json",
            storage_key=key or storage.relative_key(path),
            sha256=hashlib.sha256(b"x").hexdigest(),
            size=1,
            visibility="org",
            expires_at=datetime.now(UTC) + expires_in,
        )
    )
    await db_session.commit()
    return artifact_id


async def artifact_ids(db_session) -> set[str]:
    db_session.expire_all()
    return set((await db_session.scalars(select(Artifact.id))).all())


async def test_purge_removes_expired_files_and_rows(db_session, report):
    engine_dir = "e" * 32
    expired = write(report, engine_dir, "result.json")
    expired_id = await add_artifact(db_session, report, expired, expires_in=timedelta(seconds=-1))
    stats = run_purge()
    assert not expired.exists()
    assert expired_id not in await artifact_ids(db_session)
    assert stats["artifacts"] == 1
    # Unlinking just touched the engine dir, so it is kept today (it may be a running engine's)
    # and removed by a later run once it is a day old.
    engine = expired.parent
    assert engine.is_dir()
    os.utime(engine, (OLD, OLD))
    assert run_purge()["dirs"] == 1
    assert not engine.exists()


async def test_purge_keeps_unexpired(db_session, report):
    kept = write(report, "e" * 32, "result.json")
    kept_id = await add_artifact(db_session, report, kept, expires_in=timedelta(days=1))
    run_purge()
    assert kept.exists()
    assert kept_id in await artifact_ids(db_session)


async def test_orphan_older_than_one_day_removed_fresh_kept(db_session, report):
    old_orphan = write(report, "input", "a" * 64 + ".png")
    fresh_orphan = write(report, "input", "b" * 64 + ".png", age=None)
    stats = run_purge()
    assert not old_orphan.exists()
    assert fresh_orphan.exists()
    assert stats["orphans"] == 1


async def test_fresh_empty_engine_dir_kept(report):
    # A running engine creates its directory first and writes into it afterwards.
    fresh = storage.safe_path(report.org_id, report.id, "f" * 32)
    fresh.mkdir(parents=True)
    old = storage.safe_path(report.org_id, report.id, "d" * 32)
    old.mkdir(parents=True)
    os.utime(old, (OLD, OLD))
    run_purge()
    assert fresh.is_dir()
    assert not old.exists()


@pytest.mark.parametrize("key", ["../outside.json", "/tmp/outside.json", "a/../../outside.json"])
async def test_path_traversal_in_storage_key_ignored(db_session, report, tmp_path, key):
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    if key.startswith("/"):
        key = str(outside)
    artifact_id = await add_artifact(
        db_session, report, None, expires_in=timedelta(seconds=-1), key=key
    )
    run_purge()
    assert outside.exists()  # never touched
    assert artifact_id not in await artifact_ids(db_session)  # expired row still removed


async def test_symlinked_file_is_unlinked_not_followed(db_session, report, tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = storage.safe_path(report.org_id, report.id, "e" * 32, "link.json")
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    os.utime(link, (OLD, OLD), follow_symlinks=False)
    run_purge()
    assert not link.is_symlink()
    assert target.exists()


async def test_expired_idempotency_records_removed(db_session):
    now = datetime.now(UTC)
    for key, expires in (("old", now - timedelta(seconds=1)), ("new", now + timedelta(hours=1))):
        db_session.add(
            IdempotencyRecord(
                user_id=None,
                scope="s",
                key=key,
                request_hash="h",
                status_code=201,
                response={},
                expires_at=expires,
            )
        )
    await db_session.commit()
    stats = run_purge()
    assert stats["idempotency_records"] == 1
    keys = (await db_session.scalars(select(IdempotencyRecord.key))).all()
    assert keys == ["new"]


async def test_purge_is_idempotent(db_session, report):
    path = write(report, "e" * 32, "result.json")
    await add_artifact(db_session, report, path, expires_in=timedelta(seconds=-1))
    run_purge()
    stats = run_purge()
    assert stats == {"artifacts": 0, "orphans": 0, "dirs": 0, "idempotency_records": 0}
    assert await db_session.scalar(select(func.count()).select_from(Artifact)) == 0


async def test_missing_storage_root_is_fine():
    assert run_purge()["orphans"] == 0
