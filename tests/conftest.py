import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db_session
from app.main import app
from app.models import Base

_BASE_URL = "postgresql+psycopg://metaphor:metaphor@localhost:5432"


def _default_test_database_url() -> str:
    """One test DB per checkout/worktree so parallel agents don't TRUNCATE each other's data."""
    suffix = hashlib.sha1(str(Path(__file__).resolve().parent).encode()).hexdigest()[:8]
    name = f"metaphor_test_{suffix}"
    with psycopg.connect(_BASE_URL.replace("+psycopg", "") + "/metaphor", autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            conn.execute(f'CREATE DATABASE "{name}"')
    return f"{_BASE_URL}/{name}"


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _default_test_database_url()


@pytest.fixture(scope="session")
async def engine():
    # Tests build the schema from models; the app itself only uses Alembic.
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    if tables:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def db_session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(session_factory, tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    async def override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_root", tmp_path / "storage")
    app.dependency_overrides[get_db_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    app.dependency_overrides.clear()


@pytest.fixture
def conn(engine):
    """Autocommit sync connection, as the worker uses."""
    with psycopg.connect(TEST_DATABASE_URL.replace("+psycopg", ""), autocommit=True) as connection:
        yield connection


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    """Point the worker (sync psycopg, storage) at the test DB and a temp storage root."""
    from app.core.config import get_settings
    from app.worker import runner

    settings = get_settings()
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "database_url", TEST_DATABASE_URL)
    monkeypatch.setattr(runner, "MONITOR_INTERVAL", 0.05)
    return settings
