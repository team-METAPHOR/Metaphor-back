"""Recent user actions sent by the Web SDK: cleaning for storage and rendering for the engine."""

from core_engine import PolicyError

from app.core.text import redact, strip_query
from app.models import ProgramScopeRevision
from app.schemas.widget import Breadcrumb
from app.services.intake import run_policy

_FORM_TAGS = {"input", "textarea", "select"}
_FORM_ROLES = {"textbox", "combobox", "searchbox"}


def _is_form_field(item: Breadcrumb) -> bool:
    # Typed text must never leave the page, even if the SDK sends it by mistake.
    target = item.target
    return (
        item.type == "input"
        or target.tag.lower() in _FORM_TAGS
        or target.role.lower() in _FORM_ROLES
        or "contenteditable" in target.selector.lower()
    )


def clean_breadcrumbs(items: list[Breadcrumb]) -> list[dict]:
    """Oldest first; query/fragment dropped from paths; every string redacted; empty fields
    omitted; form-field text removed."""
    cleaned = []
    for item in sorted(items, key=lambda b: -b.t):
        entry: dict = {"type": item.type, "t": item.t, "path": redact(strip_query(item.path))}
        if item.target is not None:
            target = item.target.model_dump()
            if _is_form_field(item):
                target["text"] = ""
            entry["target"] = {k: redact(v) for k, v in target.items() if v}
        cleaned.append(entry)
    return cleaned


HEADER = "[사용자 최근 동작] (오래된 순)"
_ACTIONS = {"click": "클릭", "navigation": "페이지 이동", "input": "입력", "submit": "제출"}
# Target identity in order of preference: (field, label inside the brackets).
_IDENTITY = (("test_id", "data-testid="), ("id", "id="), ("name", "name="), ("selector", ""))


def _target(target: dict) -> str:
    parts = [target.get("tag", "")]
    if target.get("text"):
        parts.append(f'"{target["text"]}"')
    for field, label in _IDENTITY:
        if target.get(field):
            parts.append(f"[{label}{target[field]}]")
            break
    return " ".join(p for p in parts if p)


def _in_scope(policy, base_url: str, path: str) -> bool:
    try:
        policy.check_url(base_url.rstrip("/") + path)
    except PolicyError:
        return False
    return True


def render_breadcrumbs(
    items: list[dict], revision: ProgramScopeRevision, budget: int = 2000
) -> str:
    """Numbered step list for the engine description, at most `budget` characters.

    Paths outside the approved scope are hidden so the model never navigates there. When the
    list is too long the oldest actions are dropped first."""
    if not items:
        return ""
    policy = run_policy(revision)
    lines = []
    for number, item in enumerate(items, 1):
        page = item["path"] if _in_scope(policy, revision.base_url, item["path"]) else None
        action = _ACTIONS[item["type"]]
        if item.get("target"):
            action = f"{action}: {_target(item['target'])}"
        lines.append(
            f"{number}. {item['t'] / 1000:.1f}초 전 · {page or '(범위 밖 페이지)'} · {action}"
        )
    dropped = 0
    while True:
        head = [HEADER] + ([f"(이전 동작 {dropped}개 생략)"] if dropped else [])
        section = "\n".join(head + lines[dropped:])
        if len(section) <= budget or dropped == len(lines) - 1:
            return section[:budget]
        dropped += 1
