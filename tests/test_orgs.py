from sqlalchemy import func, select

from app.models import AuditEvent, Membership, Organization
from tests.factories import login_as, make_org


async def create(client, headers, name="Acme", key=None):
    if key:
        headers = {**headers, "Idempotency-Key": key}
    return await client.post("/api/v1/orgs", json={"name": name}, headers=headers)


async def test_create_org_makes_creator_admin(client, db_session):
    user, headers = await login_as(client, db_session)
    response = await create(client, headers, name="  New Co  ")
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "name", "role", "created_at"}
    assert body["name"] == "New Co" and body["role"] == "admin"

    membership = await db_session.get(Membership, (body["id"], user.id))
    assert membership.role == "admin" and membership.is_active
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    assert me["memberships"] == [{"org_id": body["id"], "org_name": "New Co", "role": "admin"}]
    detail = (await client.get(f"/api/v1/orgs/{body['id']}", headers=headers)).json()
    assert detail["member_count"] == 1 and detail["role"] == "admin"
    audit = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "org.create"))
    assert audit.actor_id == user.id and audit.org_id == body["id"]


async def test_org_create_rejects_blank_name(client, db_session):
    _, headers = await login_as(client, db_session)
    assert (await create(client, headers, name="   ")).status_code == 422
    assert (await create(client, headers, name="x" * 201)).status_code == 422


async def test_org_create_daily_limit_429(client, db_session):
    _, headers = await login_as(client, db_session)
    for i in range(3):
        assert (await create(client, headers, name=f"Co {i}")).status_code == 201
    response = await create(client, headers, name="Co 4")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "ORG_CREATE_LIMITED"
    assert await db_session.scalar(select(func.count()).select_from(Organization)) == 3


async def test_org_hidden_from_non_member_404(client, db_session):
    org = await make_org(db_session)
    _, member = await login_as(client, db_session, org, "developer")
    _, outsider = await login_as(client, db_session)
    assert (await client.get(f"/api/v1/orgs/{org.id}", headers=member)).status_code == 200
    for method, kwargs in (("GET", {}), ("PATCH", {"json": {"name": "Hijack"}})):
        response = await client.request(
            method, f"/api/v1/orgs/{org.id}", headers=outsider, **kwargs
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
    missing = await client.get("/api/v1/orgs/" + "0" * 32, headers=outsider)
    assert missing.status_code == 404


async def test_only_admin_renames_org(client, db_session):
    org = await make_org(db_session)
    _, admin = await login_as(client, db_session, org, "admin")
    _, developer = await login_as(client, db_session, org, "developer")
    url = f"/api/v1/orgs/{org.id}"

    denied = await client.patch(url, json={"name": "Dev Co"}, headers=developer)
    assert denied.status_code == 403
    response = await client.patch(url, json={"name": "Renamed"}, headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Renamed" and body["member_count"] == 2 and body["role"] == "admin"
    await db_session.refresh(org)
    assert org.name == "Renamed"
    audit = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "org.update"))
    assert audit.target == org.id


async def test_create_org_idempotent_replay(client, db_session):
    _, headers = await login_as(client, db_session)
    first = await create(client, headers, key="k-1")
    second = await create(client, headers, key="k-1")
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert await db_session.scalar(select(func.count()).select_from(Organization)) == 1
    reused = await create(client, headers, name="Other", key="k-1")
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_org_endpoints_require_login(client):
    response = await client.post(
        "/api/v1/orgs", json={"name": "Acme"}, headers={"Origin": "http://localhost:3000"}
    )
    assert response.status_code == 401
