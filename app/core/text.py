from core_engine.redaction import redact_text

from app.core.errors import ApiError


def sanitize_text(value: str, max_len: int) -> str:
    """Check the raw length first, then return the redacted text."""
    if len(value) > max_len:
        raise ApiError(422, "TEXT_TOO_LONG", f"Text must be at most {max_len} characters")
    return redact_text(value)
