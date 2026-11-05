import hashlib
import hmac

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import AuditEvent
from tests.conftest import TEST_HASH_SECRET
from tests.test_auth import login, signup


def _expected(purpose: str, value: str) -> str:
    key = hmac.new(TEST_HASH_SECRET.encode(), f"metaphor-hash:{purpose}".encode(), hashlib.sha256)
    return hmac.new(key.digest(), value.encode(), hashlib.sha256).hexdigest()


async def test_login_failure_keys_are_per_purpose_hmac(client, db_session):
    await signup(client)
    assert (await login(client, password="wrong-password-123")).status_code == 401
    targets = set(
        await db_session.scalars(
            select(AuditEvent.target).where(AuditEvent.action == "auth.login_failed")
        )
    )
    # ASGITransport's peer is 127.0.0.1.
    assert targets == {
        _expected("login-email", "dev@example.com"),
        _expected("login-ip-email", "127.0.0.1\ndev@example.com"),
    }
    assert hashlib.sha256(b"dev@example.com").hexdigest() not in targets


def test_keyed_hash_deterministic_and_separated_by_purpose():
    from app.core.hashing import keyed_hash

    assert keyed_hash("client-ip", "1.2.3.4") == keyed_hash("client-ip", "1.2.3.4")
    assert keyed_hash("client-ip", "1.2.3.4") != keyed_hash("login-email", "1.2.3.4")
    assert keyed_hash("client-ip", "1.2.3.4") != hashlib.sha256(b"1.2.3.4").hexdigest()


def test_no_hash_without_secret_outside_debug(monkeypatch):
    from app.core.hashing import keyed_hash, lookup_hash

    monkeypatch.setattr(get_settings(), "debug", False)
    for secret in ("", "short-secret"):
        monkeypatch.setattr(get_settings(), "ip_hash_secret", secret)
        assert keyed_hash("login-email", "dev@example.com") is None
        with pytest.raises(RuntimeError):
            lookup_hash("login-email", "dev@example.com")


@pytest.mark.parametrize("secret", ["", "x" * 31])
async def test_production_startup_fails_without_long_secret(monkeypatch, secret):
    from app.main import app

    monkeypatch.setattr(get_settings(), "debug", False)
    monkeypatch.setattr(get_settings(), "ip_hash_secret", secret)
    with pytest.raises(RuntimeError, match="IP_HASH_SECRET"):
        async with app.router.lifespan_context(app):
            pass


def test_debug_mode_uses_explicit_dev_key(monkeypatch):
    from app.core.hashing import DEV_HASH_SECRET, check_hash_secret, keyed_hash

    monkeypatch.setattr(get_settings(), "debug", True)
    monkeypatch.setattr(get_settings(), "ip_hash_secret", "")
    check_hash_secret(get_settings())
    monkeypatch.setattr(get_settings(), "ip_hash_secret", DEV_HASH_SECRET)
    explicit = keyed_hash("client-ip", "1.2.3.4")
    monkeypatch.setattr(get_settings(), "ip_hash_secret", "")
    assert keyed_hash("client-ip", "1.2.3.4") == explicit


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


async def test_signup_throttled_per_client_ip_429(client, db_session, monkeypatch):
    from app.services.auth import SIGNUP_HOURLY_LIMIT

    for i in range(SIGNUP_HOURLY_LIMIT):
        await signup(client, email=f"u{i}@example.com")
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": "late@example.com", "password": "correct-horse-battery"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "SIGNUP_THROTTLED"
    targets = (
        await db_session.scalars(
            select(AuditEvent.target).where(AuditEvent.action == "auth.signup")
        )
    ).all()
    assert targets == [_expected("client-ip", "127.0.0.1")] * 5
    # Another client (behind a trusted proxy) is unaffected.
    monkeypatch.setattr(get_settings(), "trusted_proxies", ["127.0.0.1"])
    other = await client.post(
        "/api/v1/auth/signup",
        json={"email": "late@example.com", "password": "correct-horse-battery"},
        headers={"Origin": "http://localhost:3000", "X-Forwarded-For": "198.51.100.7"},
    )
    assert other.status_code == 201, other.text


@pytest.mark.parametrize(
    "hop",
    [
        "198.51.100.7",
        " 198.51.100.7 ",
        "198.51.100.7:5555",
        "::ffff:198.51.100.7",
        "[::ffff:198.51.100.7]:443",
    ],
)
def test_client_ip_canonicalizes_hop_variants(monkeypatch, hop):
    from app.core.client_ip import client_ip

    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.0/8"])
    assert client_ip(_request("10.0.0.5", [hop])) == "198.51.100.7"


@pytest.mark.parametrize("hop", ["[2001:db8::1]", "[2001:DB8:0::1]:8080", "2001:db8:0:0::1"])
def test_client_ip_canonicalizes_ipv6(monkeypatch, hop):
    from app.core.client_ip import client_ip

    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.0/8"])
    assert client_ip(_request("10.0.0.5", [hop])) == "2001:db8::1"


@pytest.mark.parametrize("hop", ["garbage", "unknown", "1.2.3.4.5", "", "[::1"])
def test_client_ip_unparsable_hop_falls_back_to_peer(monkeypatch, hop):
    from app.core.client_ip import client_ip

    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.0/8"])
    assert client_ip(_request("10.0.0.5", [f"6.6.6.6, {hop}"])) == "10.0.0.5"


def test_client_ip_canonicalizes_peer(monkeypatch):
    from app.core.client_ip import client_ip

    monkeypatch.setattr(get_settings(), "trusted_proxies", [])
    assert client_ip(_request("::ffff:203.0.113.9")) == "203.0.113.9"


async def test_oversized_request_413_envelope(client):
    from app.core.body_limit import MAX_REQUEST_BYTES

    response = await client.post(
        "/api/v1/reports",
        content=b"x" * (MAX_REQUEST_BYTES + 1),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "PAYLOAD_TOO_LARGE"
    assert error["request_id"] == response.headers["X-Request-Id"]
    assert MAX_REQUEST_BYTES == 6 * 1024 * 1024


async def test_chunked_oversized_request_413_envelope(client):
    from app.core.body_limit import MAX_REQUEST_BYTES

    async def chunks():
        for _ in range(7):
            yield b"x" * (1024 * 1024)

    response = await client.post(
        "/api/v1/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "PAYLOAD_TOO_LARGE"
    assert error["request_id"] == response.headers["X-Request-Id"]
    assert 7 * 1024 * 1024 > MAX_REQUEST_BYTES


def test_xff_from_untrusted_peer_warns_once_without_value(monkeypatch, caplog):
    from app.core import client_ip as module

    monkeypatch.setattr(get_settings(), "trusted_proxies", [])
    monkeypatch.setattr(module, "_warned_untrusted_xff", False)
    with caplog.at_level("WARNING", logger="app.core.client_ip"):
        for _ in range(3):
            assert module.client_ip(_request("10.0.0.5", ["198.51.100.7"])) == "10.0.0.5"
        module.client_ip(_request("10.0.0.6"))  # no header: no warning
    records = [r for r in caplog.records if r.name == "app.core.client_ip"]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert "TRUSTED_PROXIES" in records[0].getMessage()
    assert "198.51.100.7" not in caplog.text
