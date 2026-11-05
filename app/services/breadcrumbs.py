"""Reporter's recent actions from the Web SDK: server-side sanitizing and engine rendering.

The SDK already avoids sensitive data; everything here is defense in depth. Stored items keep
only whitelisted fields, redacted, with paths stripped of query/fragment and checked against
the approved scope so the model is never pointed outside it.
"""

import re
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from core_engine import PolicyError, canonical_url

from app.core.text import redact, strip_query
from app.models import ProgramScopeRevision
from app.schemas.widget import Breadcrumb

SECTION_BUDGET = 2000
OUT_OF_SCOPE = "(범위 밖)"
HEADER = "[신고 직전 사용자 동작] (시간순, 마지막 줄이 신고 직전. 시간은 신고 시점 기준)"

_FREE_TEXT = ("tag", "role", "text", "testid", "selector", "input_type", "label")
_FREE_TEXT += ("selected_text", "message")
_FORM_TAGS = {"input", "textarea", "select", "option"}
_FORM_ROLES = {"textbox", "searchbox", "combobox", "listbox", "option", "spinbutton"}
_SECRET_INPUTS = {"password", "hidden"}
_CHOICE_INPUTS = {"radio", "checkbox"}
_SENSITIVE_HINT = re.compile(
    r"pass|pwd|secret|token|card|cvc|cvv|ssn|rrn|otp|iban|contenteditable"
    r"|주민|카드|비밀|계좌",
    re.IGNORECASE,
)
_MASK_TOKEN = re.compile(r"[#.][\w-]+|\[[^\]]+\]")
_ATTR_VALUE = re.compile(r"\[\s*[\w-]+\s*[~|^$*]?=\s*['\"]?([^'\"\]]+?)['\"]?\s*\]")


def _matches_mask(haystack: str, masks: Iterable[str]) -> bool:
    """Best effort: does a reported selector/testid look like a masked element?"""
    for mask in masks:
        mask = mask.strip().lower()
        if not mask:
            continue
        if mask in haystack:
            return True
        for token in _MASK_TOKEN.findall(mask):
            value = _ATTR_VALUE.fullmatch(token)
            if token in haystack or (value and value[1] in haystack):
                return True
    return False


def _sensitive(item: dict, masks: Sequence[str]) -> bool:
    haystack = " ".join(item.get(k) or "" for k in ("selector", "testid", "label")).lower()
    return bool(_SENSITIVE_HINT.search(haystack)) or _matches_mask(haystack, masks)


def _path_scope(revision: ProgramScopeRevision, path: str) -> str:
    """ "ok", "excluded" (under an excluded prefix) or "out" (anything else)."""
    if not path.startswith("/"):
        return "out"
    try:
        canonical = urlsplit(canonical_url(revision.base_url.rstrip("/") + path)).path
    except PolicyError:
        return "out"

    def under(prefix: str) -> bool:
        return prefix == "/" or canonical == prefix or canonical.startswith(prefix + "/")

    if any(under(p) for p in revision.excluded_path_prefixes):
        return "excluded"
    if not any(under(p) for p in revision.allowed_path_prefixes):
        return "out"
    return "ok"


def _clean_item(
    crumb: Breadcrumb, revision: ProgramScopeRevision, masks: Sequence[str]
) -> dict | None:
    item = crumb.model_dump(exclude_none=True)
    kind = item["type"]
    tag = (item.get("tag") or "").lower()
    if kind == "click" and (
        tag in _FORM_TAGS
        or (item.get("role") or "").lower() in _FORM_ROLES
        or _sensitive(item, masks)
    ):
        item.pop("text", None)
    if kind == "input":
        input_type = (item.get("input_type") or "").lower()
        if input_type in _SECRET_INPUTS:
            item.pop("length", None)
        # Only a choice (select option, radio/checkbox label) may carry text, never typed text.
        is_choice = tag == "select" or input_type in _CHOICE_INPUTS
        if not is_choice or input_type in _SECRET_INPUTS or _sensitive(item, masks):
            item.pop("selected_text", None)
    if "path" in item:
        path = strip_query(item["path"])
        scope = _path_scope(revision, path)
        if scope == "excluded" and kind == "request":
            return None
        item["path"] = redact(path) if scope == "ok" else OUT_OF_SCOPE
    for key in _FREE_TEXT:
        if key in item:
            item[key] = redact(item[key])
    return {k: v for k, v in item.items() if v != ""}


def clean_breadcrumbs(
    crumbs: Sequence[Breadcrumb], revision: ProgramScopeRevision, masks: Sequence[str]
) -> list[dict]:
    """Oldest first; consecutive identical actions merged into one item with `count`."""
    cleaned: list[dict] = []
    for crumb in sorted(crumbs, key=lambda c: c.t):
        item = _clean_item(crumb, revision, masks)
        if item is None:
            continue
        previous = cleaned[-1] if cleaned else None
        same = previous is not None and {
            k: v for k, v in previous.items() if k not in ("t", "count")
        } == {k: v for k, v in item.items() if k != "t"}
        if same:
            previous["count"] = previous.get("count", 1) + 1
        else:
            cleaned.append(item)
    return cleaned


def _quoted(value: str | None) -> str:
    return f"'{value}'" if value else ""


def _identity(item: dict) -> str:
    if item.get("testid"):
        return f"[data-testid={item['testid']}]"
    if item.get("selector"):
        return f"[css={item['selector']}]"
    return ""


def _line(item: dict) -> str:
    kind = item["type"]
    if kind == "navigation":
        parts = ["이동", item.get("path", "")]
    elif kind == "click":
        what = item.get("tag") or item.get("role") or "요소"
        parts = ["클릭", what, _quoted(item.get("text")), _identity(item)]
    elif kind == "input":
        field = item.get("tag") or "필드"
        if item.get("input_type"):
            field += f"[type={item['input_type']}]"
        parts = ["입력", field, _quoted(item.get("label")), _identity(item)]
        if "checked" in item:
            parts.append("(체크함)" if item["checked"] else "(체크 해제)")
        if item.get("selected_text"):
            parts.append(f"→ {_quoted(item['selected_text'])}")
        if "length" in item:
            parts.append(f"({item['length']}자)")
    elif kind == "submit":
        parts = ["제출", "form", _quoted(item.get("label")), _identity(item)]
    elif kind == "key":
        parts = ["키", item.get("key", "")]
    elif kind == "request":
        parts = ["요청", item.get("method", ""), item.get("path", "")]
        if "status" in item:
            parts.append(f"→ {item['status']}")
        if "duration_ms" in item:
            parts.append(f"({item['duration_ms']}ms)")
    else:
        parts = ["오류", _quoted(item.get("message"))]
    if item.get("count", 1) > 1:
        parts.append(f"(×{item['count']})")
    return " ".join([f"{item['t'] / 1000:.1f}s", *(p for p in parts if p)])


def render_breadcrumbs(items: Sequence[dict], budget: int = SECTION_BUDGET) -> str:
    """Chronological action list within `budget` characters; the oldest are dropped first.
    Returns "" when nothing fits."""
    lines = [_line(item) for item in items]
    for dropped in range(len(lines)):
        head = [HEADER] + ([f"(이전 동작 {dropped}개 생략)"] if dropped else [])
        section = "\n".join(head + lines[dropped:])
        if len(section) <= budget:
            return section
    return ""
