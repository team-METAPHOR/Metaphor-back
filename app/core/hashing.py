"""Keyed hashes for values we must match on but never store raw (client IPs, login emails).

One secret (IP_HASH_SECRET) is stretched into one HMAC key per purpose, so a hash made for
one purpose never matches the same input hashed for another. There is no unkeyed fallback:
plain SHA-256 of an email or IP is reversible by dictionary attack.
"""

import hashlib
import hmac

from app.core.config import Settings, get_settings

MIN_SECRET_BYTES = 32
# Used only when DEBUG=true and IP_HASH_SECRET is empty. Public, so never safe in production.
DEV_HASH_SECRET = "metaphor-dev-only-hash-secret-do-not-use-in-production"

LOGIN_EMAIL = "login-email"
LOGIN_IP_EMAIL = "login-ip-email"
CLIENT_IP = "client-ip"


def hash_secret(settings: Settings) -> bytes | None:
    """The configured secret if long enough, the dev key in debug mode, else None."""
    secret = settings.ip_hash_secret.encode()
    if len(secret) >= MIN_SECRET_BYTES:
        return secret
    if settings.debug and not secret:
        return DEV_HASH_SECRET.encode()
    return None


def check_hash_secret(settings: Settings) -> None:
    """Fail startup outside debug mode when IP_HASH_SECRET is missing or too short."""
    if hash_secret(settings) is None:
        raise RuntimeError(
            f"IP_HASH_SECRET must be at least {MIN_SECRET_BYTES} bytes "
            "(or leave it empty with DEBUG=true for the dev-only key)"
        )


def keyed_hash(purpose: str, value: str) -> str | None:
    """HMAC-SHA256 under the per-purpose key; None when no usable secret is configured."""
    secret = hash_secret(get_settings())
    if secret is None:
        return None
    key = hmac.new(secret, f"metaphor-hash:{purpose}".encode(), hashlib.sha256).digest()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def lookup_hash(purpose: str, value: str) -> str:
    """keyed_hash, raising when no secret is configured (startup normally prevents this)."""
    hashed = keyed_hash(purpose, value)
    if hashed is None:
        raise RuntimeError("IP_HASH_SECRET is not configured")
    return hashed
