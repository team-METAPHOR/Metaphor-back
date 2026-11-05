"""Single-use invite links. The raw token is returned once; only its sha256 is stored."""

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.ids import new_id
from app.models import Membership, OrgInvite
from app.repositories import invites as repo
from app.repositories import orgs as orgs_repo
from app.schemas.org import InviteAccepted, InviteCreated, InviteResponse
from app.services.access import require_org_role
from app.services.audit import count_actions, record_audit
from app.services.auth import token_hash
from app.services.limits import enforce_rate_limit, lock_key, since

PENDING_LIMIT = 50
ACCEPT_FAILED = "invite.accept_failed"
ACCEPT_HOURLY_FAILURES = 20


def _invalid() -> ApiError:
    # One response for unknown, used, revoked and expired tokens.
    return ApiError(404, "INVITE_INVALID", "유효하지 않은 초대 링크입니다.")


async def create_invite(
    db: AsyncSession, actor_id: str, org_id: str, role: str, expires_in_days: int
) -> InviteCreated:
    async with db.begin():
        await require_org_role(db, actor_id, org_id, "admin")
        # Row lock: the pending count and the insert below must not interleave.
        await orgs_repo.get(db, org_id, for_update=True)
        now = datetime.now(UTC)
        if await repo.count_pending(db, org_id, now) >= PENDING_LIMIT:
            raise ApiError(409, "INVITE_LIMIT", "대기 중인 초대가 너무 많습니다.")
        token = secrets.token_urlsafe(32)
        invite = OrgInvite(
            id=new_id(),
            org_id=org_id,
            token_hash=token_hash(token),
            role=role,
            created_by=actor_id,
            expires_at=now + timedelta(days=expires_in_days),
        )
        await orgs_repo.add(db, invite)
        await record_audit(
            db,
            actor_id=actor_id,
            org_id=org_id,
            action="invite.create",
            target=invite.id,
            detail={"role": role, "expires_in_days": expires_in_days},
        )
        await db.refresh(invite)
        return InviteCreated(id=invite.id, role=role, expires_at=invite.expires_at, token=token)


async def list_invites(db: AsyncSession, actor_id: str, org_id: str) -> list[InviteResponse]:
    async with db.begin():
        await require_org_role(db, actor_id, org_id, "admin")
        rows = await repo.list_pending(db, org_id, datetime.now(UTC))
        return [
            InviteResponse(id=i.id, role=i.role, created_by=i.created_by, expires_at=i.expires_at)
            for i in rows
        ]


async def revoke_invite(db: AsyncSession, actor_id: str, org_id: str, invite_id: str) -> None:
    async with db.begin():
        await require_org_role(db, actor_id, org_id, "admin")
        invite = await repo.get_in_org(db, org_id, invite_id)
        if invite is None or invite.used_at is not None or invite.revoked_at is not None:
            raise ApiError(404, "NOT_FOUND", "초대를 찾을 수 없습니다.")
        invite.revoked_at = datetime.now(UTC)
        await db.flush()
        await record_audit(
            db, actor_id=actor_id, org_id=org_id, action="invite.revoke", target=invite.id
        )


async def accept_invite(db: AsyncSession, user_id: str, token: str) -> InviteAccepted:
    """Join (or rejoin) the invite's org with the invite's role. 20 failures per user per hour."""
    hashed = token_hash(token)
    async with db.begin():
        await lock_key(db, f"invite.accept:{user_id}")
        await enforce_rate_limit(
            db,
            count_actions(user_id, (ACCEPT_FAILED,), since(hours=1)),
            limit=ACCEPT_HOURLY_FAILURES,
            code="INVITE_ACCEPT_LIMITED",
            message="초대 수락 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.",
        )
        # Row lock: a concurrent accept of the same token waits here and then sees used_at.
        invite = await repo.get_by_hash_for_update(db, hashed)
        now = datetime.now(UTC)
        org = await orgs_repo.get(db, invite.org_id) if invite else None
        if (
            invite is not None
            and invite.used_at is None
            and invite.revoked_at is None
            and invite.expires_at > now
            and org.is_active
        ):
            membership = await orgs_repo.get_membership(db, org.id, user_id)
            if membership is not None and membership.is_active:
                raise ApiError(409, "ALREADY_MEMBER", "이미 이 회사의 멤버입니다.")
            if membership is None:
                await orgs_repo.add(
                    db, Membership(org_id=org.id, user_id=user_id, role=invite.role)
                )
            else:
                # Rejoin with the invite's role, never the role held before removal.
                membership.role, membership.is_active = invite.role, True
            invite.used_at, invite.used_by = now, user_id
            await db.flush()
            await record_audit(
                db,
                actor_id=user_id,
                org_id=org.id,
                action="invite.accept",
                target=invite.id,
                detail={"role": invite.role},
            )
            return InviteAccepted(org_id=org.id, org_name=org.name, role=invite.role)
        # Recorded in this transaction, which commits on block exit; raise only after that.
        await record_audit(
            db, actor_id=user_id, org_id=None, action=ACCEPT_FAILED, target=hashed[:12]
        )
    raise _invalid()
