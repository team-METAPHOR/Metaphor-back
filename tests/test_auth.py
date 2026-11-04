from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.errors import ApiError
from app.models import AuditEvent, Membership, Organization, Session, User
from app.services.access import deactivate_membership, org_role, require_org_role
from app.services.auth import (
    change_password,
    hash_password,
    revoke_user_sessions,
    verify_password,
)
from tests.factories import make_org, make_user

ORIGIN = {"Origin": "http://localhost:3000"}
PASSWORD = "correct-horse-battery"


async def signup(client, email="dev@example.com", password=PASSWORD):
    response = await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": password}, headers=ORIGIN
    )
    assert response.status_code == 201, response.text
    return response.json()


async def login(client, email="dev@example.com", password=PASSWORD):
    return await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, headers=ORIGIN
    )


async def login_token(client, **kwargs) -> str:
    response = await login(client, **kwargs)
    assert response.status_code == 200, response.text
    return response.cookies["sid"]


def as_user(token: str, **headers) -> dict:
    # The cookie is Secure, so httpx will not replay it over http://testserver.
    return {"Cookie": f"sid={token}", **headers}


async def test_login_sets_httponly_cookie(client):
    await signup(client)
    response = await login(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("sid=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api" in cookie


async def test_session_cookie_secure_when_not_debug(client, monkeypatch):
    await signup(client)
    assert "Secure" in (await login(client)).headers["set-cookie"]
    monkeypatch.setattr(get_settings(), "debug", True)
    assert "Secure" not in (await login(client)).headers["set-cookie"]


async def test_db_stores_only_token_hash(client, db_session):
    await signup(client)
    token = await login_token(client)
    ids = (await db_session.scalars(select(Session.id))).all()
    assert token not in ids
    assert len(ids) == 1 and len(ids[0]) == 64


async def test_me_requires_session(client):
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "UNAUTHENTICATED"
    assert error["request_id"]


async def test_me_lists_active_memberships(client, db_session):
    user = await signup(client, email="Dev@Example.com")
    assert user["email"] == "dev@example.com"
    db_session.add_all(
        [
            Organization(id="a" * 32, name="Acme"),
            Organization(id="b" * 32, name="Gone"),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            Membership(org_id="a" * 32, user_id=user["id"], role="admin"),
            Membership(org_id="b" * 32, user_id=user["id"], role="developer", is_active=False),
        ]
    )
    await db_session.commit()

    token = await login_token(client, email="DEV@example.com")
    response = await client.get("/api/v1/auth/me", headers=as_user(token))
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "dev@example.com"
    assert body["memberships"] == [{"org_id": "a" * 32, "org_name": "Acme", "role": "admin"}]
    assert "password_hash" not in response.text


async def test_unsafe_method_rejects_foreign_or_missing_origin(client):
    await signup(client)
    token = await login_token(client)
    evil = await client.post(
        "/api/v1/auth/logout", headers=as_user(token, Origin="https://evil.example")
    )
    assert evil.status_code == 403
    assert evil.json()["error"]["code"] == "CSRF_REJECTED"
    missing = await client.post("/api/v1/auth/logout", headers=as_user(token))
    assert missing.status_code == 403
    # Session survived both rejected requests.
    assert (await client.get("/api/v1/auth/me", headers=as_user(token))).status_code == 200


async def test_wrong_password_401_same_body_as_unknown_email(client):
    await signup(client)
    wrong = await login(client, password="wrong-password-123")
    unknown = await login(client, email="nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"
    strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
    assert strip(wrong) == strip(unknown)


async def test_login_rotates_session(client):
    await signup(client)
    first = await login_token(client)
    second = await login_token(client)
    assert first != second
    assert (await client.get("/api/v1/auth/me", headers=as_user(first))).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=as_user(second))).status_code == 200


async def test_logout_revokes_session(client):
    await signup(client)
    token = await login_token(client)
    response = await client.post("/api/v1/auth/logout", headers=as_user(token, **ORIGIN))
    assert response.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=as_user(token))).status_code == 401


async def test_expired_session_or_inactive_user_401(client, db_session):
    user = await signup(client)
    token = await login_token(client)
    past = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.execute(update(Session).values(expires_at=past))
    await db_session.commit()
    assert (await client.get("/api/v1/auth/me", headers=as_user(token))).status_code == 401

    token = await login_token(client)
    await db_session.execute(update(User).where(User.id == user["id"]).values(is_active=False))
    await db_session.commit()
    assert (await client.get("/api/v1/auth/me", headers=as_user(token))).status_code == 401
    assert (await login(client)).status_code == 401


async def test_signup_validation(client):
    short = await client.post(
        "/api/v1/auth/signup",
        json={"email": "a@example.com", "password": "short"},
        headers=ORIGIN,
    )
    assert short.status_code == 422
    await signup(client, email="a@example.com")
    dup = await client.post(
        "/api/v1/auth/signup",
        json={"email": "A@example.com", "password": PASSWORD},
        headers=ORIGIN,
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "EMAIL_TAKEN"


async def test_revoke_user_sessions(client, session_factory):
    user = await signup(client)
    token = await login_token(client)
    async with session_factory() as db, db.begin():
        await revoke_user_sessions(db, user["id"])
    assert (await client.get("/api/v1/auth/me", headers=as_user(token))).status_code == 401


def test_password_hash_roundtrip():
    stored = hash_password(PASSWORD)
    assert PASSWORD not in stored
    assert stored.startswith("scrypt$")
    assert stored != hash_password(PASSWORD)  # random salt
    assert verify_password(PASSWORD, stored)
    assert not verify_password("wrong-password", stored)


async def test_csrf_guard_not_applied_to_widget_prefix(client):
    from app.api.dependencies import require_csrf
    from app.api.v1.router import api_router

    assert require_csrf not in [d.dependency for d in api_router.dependencies]
    # A route mounted the way /widget/* will be: POST without Origin must pass.
    before = list(api_router.routes)
    api_router.add_api_route("/widget/_probe", lambda: {"ok": True}, methods=["POST"])
    try:
        assert (await client.post("/api/v1/widget/_probe")).status_code == 200
    finally:
        api_router.routes[:] = before
    # Platform routes, by contrast, are guarded.
    assert (await client.post("/api/v1/auth/logout")).status_code == 403


@pytest.fixture
async def members(db_session):
    org = await make_org(db_session)
    closed = await make_org(db_session, name="Closed")
    closed.is_active = False
    dev = await make_user(db_session, org, "developer")
    await make_user(db_session, closed, "admin")  # unrelated member keeps the org populated
    db_session.add(Membership(org_id=closed.id, user_id=dev.id, role="admin"))
    gone = await make_user(db_session, org, "admin")
    await db_session.commit()
    async with db_session.begin():
        await deactivate_membership(db_session, org.id, gone.id)
    return org, closed, dev, gone


async def test_org_role_requires_active_membership_and_org(db_session, members):
    org, closed, dev, gone = members
    assert await org_role(db_session, dev.id, org.id) == "developer"
    assert await org_role(db_session, gone.id, org.id) is None  # deactivated membership
    assert await org_role(db_session, dev.id, closed.id) is None  # inactive org


async def test_require_org_role_404_vs_403(db_session, members):
    org, _, dev, gone = members
    assert await require_org_role(db_session, dev.id, org.id, "developer") == "developer"
    with pytest.raises(ApiError) as forbidden:
        await require_org_role(db_session, dev.id, org.id, "admin")
    assert (forbidden.value.status, forbidden.value.code) == (403, "FORBIDDEN")
    with pytest.raises(ApiError) as hidden:
        await require_org_role(db_session, gone.id, org.id, "admin")
    assert (hidden.value.status, hidden.value.code) == (404, "NOT_FOUND")


async def test_login_throttled_after_5_failures_429(client, db_session):
    await signup(client)
    for _ in range(5):
        assert (await login(client, password="wrong-password-123")).status_code == 401
    throttled = await login(client)  # correct password, still blocked for 15 minutes
    assert throttled.status_code == 429
    assert throttled.json()["error"]["code"] == "LOGIN_THROTTLED"
    targets = (await db_session.scalars(select(AuditEvent.target))).all()
    assert len(targets) == 5
    assert all(t and "@" not in t and len(t) == 64 for t in targets)  # sha256, never raw email
    # Another account is unaffected.
    await signup(client, email="other@example.com")
    assert (await login(client, email="other@example.com")).status_code == 200


async def test_login_failures_outside_window_do_not_count(client, db_session):
    await signup(client)
    for _ in range(5):
        await login(client, password="wrong-password-123")
    old = datetime.now(UTC) - timedelta(minutes=16)
    await db_session.execute(update(AuditEvent).values(created_at=old))
    await db_session.commit()
    assert (await login(client)).status_code == 200


async def test_deactivated_member_session_revoked(client, db_session):
    user = await signup(client)
    org = await make_org(db_session)
    db_session.add(Membership(org_id=org.id, user_id=user["id"], role="developer"))
    await db_session.commit()
    token = await login_token(client)
    async with db_session.begin():
        await deactivate_membership(db_session, org.id, user["id"])
    assert (await client.get("/api/v1/auth/me", headers=as_user(token))).status_code == 401


async def test_password_change_revokes_sessions(client, db_session):
    user = await signup(client)
    token = await login_token(client)
    with pytest.raises(ApiError):
        await change_password(db_session, user["id"], "wrong-password-123", "new-password-456")
    assert (await client.get("/api/v1/auth/me", headers=as_user(token))).status_code == 200
    await change_password(db_session, user["id"], PASSWORD, "new-password-456")
    assert (await client.get("/api/v1/auth/me", headers=as_user(token))).status_code == 401
    assert (await login(client, password="new-password-456")).status_code == 200
