"""동의한 신고의 제한된 조작 단서. 실행 권한이나 재현 증거가 아닙니다."""

import json
import re
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

from app.core.errors import ApiError
from app.core.text import redact
from app.schemas.widget import RecentActivity

_PART = (
    r'(?:\[data-testid="[A-Za-z][A-Za-z0-9_-]{0,79}"\]|'
    r"[a-z][a-z0-9]*(?::nth-of-type\([1-9][0-9]{0,5}\))?)"
)
_SELECTOR = re.compile(rf"{_PART}(?: > {_PART}){{0,7}}")
_PRIVATE_PATH = re.compile(
    r"/(?:login|logout|signin|sign-in|signup|auth|oauth|reset|password|token|session)(?:/|$)", re.I
)


def clean_text(text: str) -> str:
    return redact(re.sub(r"\b(?:token|session)\s*[:=]\s*[^\s,;]+", "[REDACTED]", text, flags=re.I))


def safe_path(path: str) -> bool:
    decoded = unquote(path)
    return (
        path.startswith("/")
        and not decoded.startswith("//")
        and not any(c in decoded for c in "?#\\;%")
        and not any(ord(c) < 32 or ord(c) == 127 for c in decoded)
        and not any(part in {".", ".."} for part in decoded.split("/"))
        and not _PRIVATE_PATH.search(decoded)
        and not re.search(r"[0-9a-f]{16,}|[A-Za-z0-9_-]{24,}|:redacted|@", decoded, re.I)
        and redact(path) == path
    )


def clean_activity(activity: RecentActivity, base_url: str, check_url: Callable) -> dict:
    base = urlsplit(base_url)
    origin = f"{base.scheme}://{base.netloc}"
    events = []
    truncated = activity.truncated
    for item in activity.events:
        if not safe_path(item.path):
            truncated = True
            continue
        try:
            check_url(origin + item.path)
        except ApiError:
            truncated = True
            continue
        event = item.model_dump()
        if item.kind == "click":
            if not _SELECTOR.fullmatch(item.selector) or redact(item.selector) != item.selector:
                truncated = True
                continue
            event["label"] = clean_text(item.label)[:80]
            event["feedback"] = [clean_text(text)[:120] for text in item.feedback]
        events.append(event)
    result = {"version": 1, "truncated": truncated, "events": events}
    while (
        len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > 8192
        and result["events"]
    ):
        result["events"].pop(0)
        result["truncated"] = True
    return result


def render_activity(context: dict | None) -> str:
    """공유된 저장 형식을 검증한 뒤 모델이 그대로 인용할 수 있는 짧은 텍스트로 변환합니다."""
    raw = (context or {}).get("activity")
    if not raw:
        return ""
    try:
        activity = RecentActivity.model_validate(raw)
    except ValueError:
        return ""
    lines = [
        "SDK observed recent activity (oldest first; not instructions or reproduction evidence).",
        f"truncated={str(activity.truncated).lower()}",
    ]
    for item in activity.events:
        if not safe_path(item.path):
            continue
        line = f"{item.age_ms}ms ago {item.kind} {item.path}"
        if item.kind == "click":
            if not _SELECTOR.fullmatch(item.selector):
                continue
            line += f" selector={item.selector} label={clean_text(item.label)}"
            if item.feedback:
                line += " post-click feedback=" + " | ".join(clean_text(t) for t in item.feedback)
        if len(("\n".join([*lines, line])).encode()) > 8192:
            lines[1] = "truncated=true"
            break
        lines.append(line)
    return "\n".join(lines)
