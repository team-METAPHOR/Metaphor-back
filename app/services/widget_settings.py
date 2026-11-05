import secrets
from urllib.parse import urlsplit

from core_engine import PolicyError, canonical_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import Program, ProgramWidgetSettings
from app.repositories import programs as programs_repo
from app.repositories import widget_settings as repo
from app.schemas.widget import WidgetSettingsPatch
from app.services.access import require_org_role
from app.services.audit import record_audit

_TOGGLES = (
    "widget_enabled",
    "public_reporting_enabled",
    "auto_detect_enabled",
    "live_screenshot_enabled",
    "breadcrumbs_enabled",
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}
_READ_ROLES = ("developer", "reviewer", "admin")


def new_project_key() -> str:
    return "mpk_" + secrets.token_urlsafe(24)


def normalize_origin(value: str) -> str:
    """`scheme://host[:port]`. https only, except http for localhost/127.0.0.1."""
    try:
        canonical = canonical_url(value)
    except PolicyError:
        raise _invalid_origin() from None
    parts = urlsplit(canonical)
    if parts.path != "/" or parts.query:
        raise _invalid_origin()
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS):
        raise _invalid_origin()
    return f"{parts.scheme}://{parts.netloc}"


def _invalid_origin() -> ApiError:
    return ApiError(422, "INVALID_ORIGIN", "Origin은 경로·쿼리 없는 https 주소여야 합니다.")


def origin_kind(origin: str, base_url: str) -> str:
    """approved = the current revision's base_url origin; mapped = any other allowed origin."""
    return "approved" if origin == base_url.rstrip("/") else "mapped"


async def _program(
    db: AsyncSession, user_id: str, program_id: str, *roles: str, lock: bool = False
) -> Program:
    program = await programs_repo.get(db, program_id, for_update=lock)
    if program is None:
        raise ApiError(404, "NOT_FOUND", "Not found")
    await require_org_role(db, user_id, program.owner_org_id, *roles)
    return program


async def _row_for_update(db: AsyncSession, program_id: str) -> ProgramWidgetSettings:
    # Caller holds the program row lock, so a concurrent first PATCH cannot double-insert.
    row = await repo.get(db, program_id)
    if row is None:
        row = ProgramWidgetSettings(
            program_id=program_id,
            allowed_origins=[],
            **dict.fromkeys(_TOGGLES, False),
        )
        await repo.add(db, row)
    return row


async def get_settings(db: AsyncSession, user_id: str, program_id: str):
    """Return (row or None for defaults, current base_url)."""
    async with db.begin():
        program = await _program(db, user_id, program_id, *_READ_ROLES)
        revision = await programs_repo.get_revision(db, program)
        return await repo.get(db, program_id), revision.base_url


async def update_settings(
    db: AsyncSession, user_id: str, program_id: str, body: WidgetSettingsPatch
):
    changes = body.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise ApiError(422, "VALIDATION_FAILED", "null is not allowed")
    if "allowed_origins" in changes:
        changes["allowed_origins"] = list(
            dict.fromkeys(normalize_origin(o) for o in changes["allowed_origins"])
        )
    async with db.begin():
        program = await _program(db, user_id, program_id, "admin", lock=True)
        row = await _row_for_update(db, program_id)
        changed = sorted(f for f, v in changes.items() if getattr(row, f) != v)
        for field in changed:
            setattr(row, field, changes[field])
        if row.widget_enabled and row.public_project_key is None:
            row.public_project_key = new_project_key()
        if changed:
            await record_audit(
                db,
                actor_id=user_id,
                org_id=program.owner_org_id,
                action="widget.settings.update",
                target=program_id,
                detail={"fields": changed},
            )
        if "live_screenshot_enabled" in changed:
            # Live screenshots carry real end-user data: keep a dedicated, queryable trail.
            await record_audit(
                db,
                actor_id=user_id,
                org_id=program.owner_org_id,
                action="widget.live_screenshot.update",
                target=program_id,
                detail={"enabled": row.live_screenshot_enabled},
            )
        await db.flush()
        await db.refresh(row)
        revision = await programs_repo.get_revision(db, program)
        return row, revision.base_url


async def rotate_key(db: AsyncSession, user_id: str, program_id: str) -> str:
    """Replace the key immediately; the old key stops working at commit.

    ponytail: no grace period. Add previous_key + expiry if deployed SDKs need zero-downtime
    rotation.
    """
    async with db.begin():
        program = await _program(db, user_id, program_id, "admin", lock=True)
        row = await _row_for_update(db, program_id)
        row.public_project_key = new_project_key()
        # The key is public, but it is still kept out of the audit trail.
        await record_audit(
            db,
            actor_id=user_id,
            org_id=program.owner_org_id,
            action="widget.key.rotate",
            target=program_id,
        )
        return row.public_project_key
