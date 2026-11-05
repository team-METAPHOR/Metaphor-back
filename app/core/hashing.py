"""Keyed hashes for values we must match on but never store raw (client IPs, login emails)."""

import hashlib
import hmac

from app.core.config import get_settings


def keyed_hash(value: str) -> str | None:
    """HMAC-SHA256 with settings.ip_hash_secret; None when the secret is unset."""
    secret = get_settings().ip_hash_secret
    if not secret:
        return None
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def lookup_hash(value: str) -> str:
    """keyed_hash, or plain sha256 when no secret is configured.

    ponytail: the sha256 fallback is dev-only (guessable for emails/IPs); set IP_HASH_SECRET in
    every shared environment.
    """
    return keyed_hash(value) or hashlib.sha256(value.encode()).hexdigest()
