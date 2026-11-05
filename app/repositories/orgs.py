from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, Organization


async def add(db: AsyncSession, *rows) -> None:
    db.add_all(rows)
    await db.flush()


async def get(db: AsyncSession, org_id: str, *, for_update: bool = False) -> Organization | None:
    query = select(Organization).where(Organization.id == org_id)
    if for_update:
        query = query.with_for_update()
    return await db.scalar(query)


async def count_active_members(db: AsyncSession, org_id: str) -> int:
    query = select(func.count()).where(Membership.org_id == org_id, Membership.is_active)
    return await db.scalar(query)
