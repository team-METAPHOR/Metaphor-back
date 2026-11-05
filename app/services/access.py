from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models.identity import MEMBERSHIP_ROLES
from app.repositories import users as repo
from app.services.auth import revoke_user_sessions

# The single place role tuples live. Every active member may read its org (READ_ROLES).
READ_ROLES = MEMBERSHIP_ROLES
WRITE_ROLES = ("admin", "developer")  # reports, jobs, clarifications, triage
ADMIN_ROLES = ("admin",)  # org, members, invites, programs, widget, origin verification


async def org_role(db: AsyncSession, user_id: str, org_id: str) -> str | None:
    """Role of an active membership in an active org, else None."""
    return await repo.get_active_role(db, user_id, org_id)


async def require_org_role(db: AsyncSession, user_id: str, org_id: str, *roles: str) -> str:
    role = await org_role(db, user_id, org_id)
    if role is None:
        # Non-members get 404 so org existence is not leaked.
        raise ApiError(404, "NOT_FOUND", "Not found")
    if role not in roles:
        raise ApiError(403, "FORBIDDEN", "Insufficient role")
    return role


async def deactivate_membership(db: AsyncSession, org_id: str, user_id: str) -> None:
    """Deactivate a membership and revoke the user's sessions. Runs in the caller's transaction."""
    await repo.deactivate_membership(db, org_id, user_id)
    await revoke_user_sessions(db, user_id)
