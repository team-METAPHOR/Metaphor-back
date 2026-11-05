"""Recent user actions sent by the Web SDK: cleaning for storage."""

from app.core.text import redact, strip_query
from app.schemas.widget import Breadcrumb

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
