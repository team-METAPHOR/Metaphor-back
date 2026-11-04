import hashlib
import hmac

from sqlalchemy import select

from app.core.config import get_settings
from app.models import AuditEvent
from tests.test_auth import login, signup


async def test_login_failure_key_is_hmac_when_secret_set(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "ip_hash_secret", "k")
    await signup(client)
    assert (await login(client, password="wrong-password-123")).status_code == 401
    target = await db_session.scalar(
        select(AuditEvent.target).where(AuditEvent.action == "auth.login_failed")
    )
    assert target == hmac.new(b"k", b"dev@example.com", hashlib.sha256).hexdigest()


async def test_login_failure_key_falls_back_to_sha256(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "ip_hash_secret", "")
    await signup(client)
    assert (await login(client, password="wrong-password-123")).status_code == 401
    target = await db_session.scalar(
        select(AuditEvent.target).where(AuditEvent.action == "auth.login_failed")
    )
    assert target == hashlib.sha256(b"dev@example.com").hexdigest()


def _request(peer: str | None, xff: list[str] | None = None):
    from starlette.requests import Request

    headers = [(b"x-forwarded-for", v.encode()) for v in xff or []]
    scope = {"type": "http", "headers": headers, "client": (peer, 1234) if peer else None}
    return Request(scope)


def test_client_ip_ignores_xff_from_untrusted_peer(monkeypatch):
    from app.core.client_ip import client_ip

    monkeypatch.setattr(get_settings(), "trusted_proxies", [])
    assert client_ip(_request("203.0.113.9", ["1.2.3.4"])) == "203.0.113.9"
    assert client_ip(_request(None)) == ""


def test_client_ip_takes_rightmost_untrusted_xff_entry(monkeypatch):
    from app.core.client_ip import client_ip

    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.0/8", "192.0.2.1"])
    # Client spoofs "6.6.6.6"; the edge proxy appends the real peer 198.51.100.7.
    req = _request("10.0.0.5", ["6.6.6.6, 198.51.100.7", "192.0.2.1"])
    assert client_ip(req) == "198.51.100.7"
    assert client_ip(_request("10.0.0.5")) == "10.0.0.5"
    assert client_ip(_request("10.0.0.5", ["10.1.1.1"])) == "10.0.0.5"


async def test_widget_uses_forwarded_client_ip_behind_trusted_proxy(client, monkeypatch):
    from app.api.v1.endpoints import widget as endpoint

    seen = []

    async def fake_submit(db, **kwargs):
        seen.append(kwargs["client_ip"])
        return 201, {}

    async def allow(request, db, key):
        return "https://x"

    monkeypatch.setattr(get_settings(), "trusted_proxies", ["127.0.0.1"])
    monkeypatch.setattr(endpoint.service, "submit_report", fake_submit)
    monkeypatch.setattr(endpoint, "_allow_origin", allow)
    response = await client.post(
        "/api/v1/widget/reports?project_key=k",
        data={"payload": "{}"},
        headers={"X-Forwarded-For": "198.51.100.7"},
    )
    assert response.status_code == 201, response.text
    assert seen == ["198.51.100.7"]
