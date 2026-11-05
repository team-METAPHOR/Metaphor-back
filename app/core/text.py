import re

from core_engine.redaction import redact_text

from app.core.errors import ApiError

# A number never starts right after an id/version/date character (hex ids, uuid tails, v1.2.3,
# 20.02.2026) and never ends right before one, so only free-standing numbers match.
_START = r"(?<![0-9A-Za-z_.\-])"
_END = r"(?![0-9A-Za-z_]|[-.]\d)"

_RRN = re.compile(_START + r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-?[1-8]\d{6}" + _END)
_CARD = re.compile(
    _START
    + r"(?:(?P<grouped>\d{4}([- ])\d{4}\2\d{4}\2\d{4})"  # 4-4-4-4: card-shaped on its own
    + r"|\d{4}([- ])\d{6}\3\d{4,5}"  # Amex/Diners 4-6-5, 4-6-4
    + r"|[2-6]\d{12,18})"  # contiguous; 1xxx is left alone (ms timestamps)
    + _END
)
_PHONE = re.compile(
    _START + r"(?:\+82[-. ]?0?|0)(?:1[016789]|2|[3-6][1-5]|70)[-. ]?\d{3,4}[-. ]?\d{4}" + _END
)
_BRN = re.compile(_START + r"\d{3}-\d{2}-\d{5}" + _END)
_ACCOUNT = re.compile(
    _START + r"(?!(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-)\d{2,6}(?:-\d{2,7}){2,3}" + _END
)


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _card(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match[0])
    if match["grouped"] or (13 <= len(digits) <= 19 and _luhn(digits)):
        return "[REDACTED_CARD]"
    return match[0]


def _account(match: re.Match) -> str:
    digits = sum(c.isdigit() for c in match[0])
    return "[REDACTED_ACCOUNT]" if 10 <= digits <= 14 else match[0]


def redact(text: str) -> str:
    """Engine `redact_text` plus Korean PII (phone, RRN, card, business no., account).
    Best effort: names and addresses are not detected."""
    text = redact_text(text)
    text = _RRN.sub("[REDACTED_RRN]", text)
    text = _CARD.sub(_card, text)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    text = _BRN.sub("[REDACTED_BRN]", text)
    return _ACCOUNT.sub(_account, text)


def redact_tree(value):
    """`redact` every string inside dicts/lists; keys and non-string values are kept."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_tree(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact_tree(item) for item in value]
    return value


def sanitize_text(value: str, max_len: int) -> str:
    """Check the raw length first, then return the redacted text."""
    if len(value) > max_len:
        raise ApiError(422, "TEXT_TOO_LONG", f"Text must be at most {max_len} characters")
    return redact(value)


def strip_query(path: str) -> str:
    """Drop the fragment and query of a path or URL."""
    return path.split("#", 1)[0].split("?", 1)[0]
