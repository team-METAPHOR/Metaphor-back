from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OrgInvite


def _pending(org_id: str, now: datetime):
    return (
        OrgInvite.org_id == org_id,
        OrgInvite.used_at.is_(None),
        OrgInvite.revoked_at.is_(None),
        OrgInvite.expires_at > now,
    )


async def count_pending(db: AsyncSession, org_id: str, now: datetime) -> int:
    return await db.scalar(select(func.count()).where(*_pending(org_id, now)))


async def list_pending(db: AsyncSession, org_id: str, now: datetime) -> list[OrgInvite]:
    query = select(OrgInvite).where(*_pending(org_id, now)).order_by(OrgInvite.created_at)
    return list(await db.scalars(query))


async def get_in_org(db: AsyncSession, org_id: str, invite_id: str) -> OrgInvite | None:
    query = select(OrgInvite).where(OrgInvite.id == invite_id, OrgInvite.org_id == org_id)
    return await db.scalar(query.with_for_update())


async def get_by_hash_for_update(db: AsyncSession, token_hash: str) -> OrgInvite | None:
    query = select(OrgInvite).where(OrgInvite.token_hash == token_hash).with_for_update()
    return await db.scalar(query)
