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
