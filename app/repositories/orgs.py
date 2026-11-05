from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, Organization, User


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


async def get_membership(db: AsyncSession, org_id: str, user_id: str) -> Membership | None:
    return await db.get(Membership, (org_id, user_id), populate_existing=True)


async def count_active_admins(db: AsyncSession, org_id: str) -> int:
    query = select(func.count()).where(
        Membership.org_id == org_id, Membership.role == "admin", Membership.is_active
    )
    return await db.scalar(query)


async def list_members(
    db: AsyncSession, org_id: str, *, include_inactive: bool
) -> list[tuple[Membership, User]]:
    query = (
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.org_id == org_id)
        .order_by(Membership.created_at, User.email)
    )
    if not include_inactive:
        query = query.where(Membership.is_active)
    return [tuple(row) for row in await db.execute(query)]


async def next_report_number(db: AsyncSession, org_id: str) -> int:
    """Atomically take the org's next report number. The row lock serializes concurrent
    creates in one org until commit; a rollback leaves a gap (accepted)."""
    query = (
        update(Organization)
        .where(Organization.id == org_id)
        .values(report_seq=Organization.report_seq + 1)
        .returning(Organization.report_seq)
    )
    return (await db.execute(query)).scalar_one()
