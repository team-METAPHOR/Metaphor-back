from app.core.ids import new_id


async def test_health_ok(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_db_reachable(db_session):
    from sqlalchemy import text

    assert (await db_session.execute(text("select 1"))).scalar() == 1


def test_engine_importable_and_ids_compatible():
    from core_engine import QARequest, RunPolicy, Screenshot  # noqa: F401

    value = new_id()
    assert len(value) == 32
    RunPolicy(
        program_id=value,
        scope_revision=1,
        base_url="https://staging.example.com",
        allowed_path_prefixes=("/",),
    )
    QARequest(
        report_id=new_id(),
        program_id=value,
        entry_url="https://staging.example.com/",
        description="x",
    )
