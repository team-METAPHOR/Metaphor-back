from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, Organization, Session, User


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    return await db.scalar(select(User).where(User.email == email))


async def add_user(db: AsyncSession, user: User) -> User:
    db.add(user)
    await db.flush()
    return user


async def add_session(db: AsyncSession, row: Session) -> None:
    db.add(row)
    await db.flush()


async def delete_session(db: AsyncSession, session_id: str) -> None:
    await db.execute(delete(Session).where(Session.id == session_id))


async def delete_user_sessions(db: AsyncSession, user_id: str) -> None:
    await db.execute(delete(Session).where(Session.user_id == user_id))


async def get_session_user(db: AsyncSession, session_id: str, now: datetime) -> User | None:
    return await db.scalar(
        select(User)
        .join(Session, Session.user_id == User.id)
        .where(Session.id == session_id, Session.expires_at > now, User.is_active)
    )


def _active_memberships():
    return (
        select(Membership.org_id, Organization.name, Membership.role)
        .join(Organization, Organization.id == Membership.org_id)
        .where(Membership.is_active, Organization.is_active)
    )


async def list_memberships(db: AsyncSession, user_id: str) -> list[tuple[str, str, str]]:
    query = _active_memberships().where(Membership.user_id == user_id)
    rows = await db.execute(query.order_by(Organization.name))
    return [tuple(row) for row in rows]


async def get_active_role(db: AsyncSession, user_id: str, org_id: str) -> str | None:
    query = _active_memberships().where(Membership.user_id == user_id, Membership.org_id == org_id)
    row = (await db.execute(query)).first()
    return row.role if row else None


async def deactivate_membership(db: AsyncSession, org_id: str, user_id: str) -> None:
    await db.execute(
        update(Membership)
        .where(Membership.org_id == org_id, Membership.user_id == user_id)
        .values(is_active=False)
    )
