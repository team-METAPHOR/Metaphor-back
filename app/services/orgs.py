"""Self-serve organizations: create, read, rename, and member management."""

import hashlib
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.ids import new_id
from app.models import Membership, Organization, User
from app.repositories import orgs as repo
from app.schemas.org import MemberResponse, OrgDetail, OrgResponse
from app.services.access import (
    ADMIN_ROLES,
    READ_ROLES,
    deactivate_membership,
    require_org_role,
)
from app.services.audit import count_actions, record_audit
from app.services.idempotency import run_once
from app.services.limits import enforce_rate_limit, lock_key, since

ORG_CREATE = "org.create"
ORG_CREATE_DAILY_LIMIT = 3


async def create_org(
    db: AsyncSession, user_id: str, name: str, idempotency_key: str | None
) -> tuple[int, dict]:
    """Create an org with the caller as its admin. 3 per user per day (audit `org.create`)."""
    request_hash = hashlib.sha256(json.dumps({"name": name}).encode()).hexdigest()
    async with db.begin():

        async def create() -> tuple[int, dict]:
            await lock_key(db, f"{ORG_CREATE}:{user_id}")
            await enforce_rate_limit(
                db,
                count_actions(user_id, (ORG_CREATE,), since(days=1)),
                limit=ORG_CREATE_DAILY_LIMIT,
                code="ORG_CREATE_LIMITED",
                message="오늘 만들 수 있는 회사 수를 초과했습니다.",
            )
            org = Organization(id=new_id(), name=name)
            await repo.add(db, org)
            await repo.add(db, Membership(org_id=org.id, user_id=user_id, role="admin"))
            await record_audit(
                db, actor_id=user_id, org_id=org.id, action=ORG_CREATE, target=org.id
            )
            await db.refresh(org)
            body = OrgResponse(id=org.id, name=org.name, role="admin", created_at=org.created_at)
            return 201, body.model_dump(mode="json")

        return await run_once(
            db,
            user_id=user_id,
            scope="orgs:create",
            key=idempotency_key,
            request_hash=request_hash,
            fn=create,
        )


async def _detail(db: AsyncSession, org: Organization, role: str) -> OrgDetail:
    return OrgDetail(
        id=org.id,
        name=org.name,
        role=role,
        member_count=await repo.count_active_members(db, org.id),
        created_at=org.created_at,
    )


async def get_org(db: AsyncSession, user_id: str, org_id: str) -> OrgDetail:
    async with db.begin():
        role = await require_org_role(db, user_id, org_id, *READ_ROLES)
        return await _detail(db, await repo.get(db, org_id), role)


async def rename_org(db: AsyncSession, user_id: str, org_id: str, name: str) -> OrgDetail:
    async with db.begin():
        role = await require_org_role(db, user_id, org_id, *ADMIN_ROLES)
        org = await repo.get(db, org_id, for_update=True)
        if org.name != name:
            org.name = name
            await db.flush()
            await record_audit(
                db, actor_id=user_id, org_id=org.id, action="org.update", target=org.id
            )
        return await _detail(db, org, role)


def _member(membership: Membership, user: User) -> MemberResponse:
    return MemberResponse(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role,
        is_active=membership.is_active,
        joined_at=membership.created_at,
    )


async def list_members(db: AsyncSession, user_id: str, org_id: str) -> list[MemberResponse]:
    """Every member sees active members; admins also see deactivated ones."""
    async with db.begin():
        role = await require_org_role(db, user_id, org_id, *READ_ROLES)
        rows = await repo.list_members(db, org_id, include_inactive=role == "admin")
        return [_member(m, u) for m, u in rows]


async def _lock_org(db: AsyncSession, org_id: str) -> None:
    """Row-lock the org so concurrent role changes/removals count admins one at a time."""
    if await repo.get(db, org_id, for_update=True) is None:
        raise ApiError(404, "NOT_FOUND", "Not found")


async def _active_member(db: AsyncSession, org_id: str, user_id: str) -> Membership:
    membership = await repo.get_membership(db, org_id, user_id)
    if membership is None or not membership.is_active:
        raise ApiError(404, "NOT_FOUND", "멤버를 찾을 수 없습니다.")
    return membership


async def _ensure_not_last_admin(db: AsyncSession, membership: Membership) -> None:
    if membership.role == "admin" and await repo.count_active_admins(db, membership.org_id) <= 1:
        raise ApiError(409, "LAST_ADMIN", "회사에는 활성 관리자가 한 명 이상 있어야 합니다.")


async def change_member_role(
    db: AsyncSession, actor_id: str, org_id: str, user_id: str, role: str
) -> MemberResponse:
    async with db.begin():
        # Lock before the role check: a concurrent demotion of the actor must be seen.
        await require_org_role(db, actor_id, org_id, *READ_ROLES)
        await _lock_org(db, org_id)
        await require_org_role(db, actor_id, org_id, *ADMIN_ROLES)
        membership = await _active_member(db, org_id, user_id)
        if membership.role != role:
            if role != "admin":
                await _ensure_not_last_admin(db, membership)
            previous, membership.role = membership.role, role
            await db.flush()
            await record_audit(
                db,
                actor_id=actor_id,
                org_id=org_id,
                action="member.role_change",
                target=user_id,
                detail={"from": previous, "to": role},
            )
        user = await db.get(User, user_id)
        return _member(membership, user)


async def remove_member(db: AsyncSession, actor_id: str, org_id: str, user_id: str) -> None:
    """Admins remove anyone; any member may remove themselves (leave). Sessions are revoked."""
    is_self = actor_id == user_id
    async with db.begin():
        await require_org_role(db, actor_id, org_id, *READ_ROLES)
        await _lock_org(db, org_id)
        await require_org_role(db, actor_id, org_id, *(READ_ROLES if is_self else ADMIN_ROLES))
        membership = await _active_member(db, org_id, user_id)
        await _ensure_not_last_admin(db, membership)
        await deactivate_membership(db, org_id, user_id)
        await record_audit(
            db,
            actor_id=actor_id,
            org_id=org_id,
            action="member.remove",
            target=user_id,
            detail={"self": is_self},
        )
