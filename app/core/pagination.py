"""Opaque list cursors: base64url of (created_at, id)."""

import base64
import binascii
import json
from datetime import datetime

from app.core.errors import ApiError


def encode_cursor(created_at: datetime, id: str) -> str:
    raw = json.dumps([created_at.isoformat(), id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        moment, id = json.loads(raw)
        return datetime.fromisoformat(moment), str(id)
    except (ValueError, TypeError, binascii.Error):
        raise ApiError(400, "INVALID_CURSOR", "목록 커서가 올바르지 않습니다.") from None
