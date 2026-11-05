from datetime import datetime

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, Organization, Report, User, ValidationJob


async def get(db: AsyncSession, report_id: str) -> Report | None:
    return await db.get(Report, report_id)


async def get_by_number(db: AsyncSession, org_id: str, number: int) -> Report | None:
    return await db.scalar(select(Report).where(Report.org_id == org_id, Report.number == number))


def count_by_author(author_id: str, since: datetime) -> Select:
    return (
        select(func.count())
        .select_from(Report)
        .where(Report.author_id == author_id, Report.created_at >= since)
    )


def member_org_ids(user_id: str):
    return (
        select(Membership.org_id)
        .join(Organization, Organization.id == Membership.org_id)
        .where(Membership.user_id == user_id, Membership.is_active, Organization.is_active)
    )


def assignee_active():
    """True when the report's assignee is an active user with an active membership in the
    report's org. Correlated to `Report`, so it is evaluated in the same query."""
    return (
        exists()
        .where(
            Membership.user_id == Report.assignee_id,
            Membership.org_id == Report.org_id,
            Membership.is_active,
        )
        .correlate(Report)
        & User.is_active
    )


async def get_assignee(db: AsyncSession, report_id: str) -> tuple[User, bool] | None:
    """The report's assignee and whether they are still an active member, in one query."""
    query = (
        select(User, assignee_active())
        .select_from(Report)
        .join(User, User.id == Report.assignee_id)
        .where(Report.id == report_id)
    )
    row = (await db.execute(query)).first()
    return (row[0], row[1]) if row else None


async def list_visible(
    db: AsyncSession,
    user_id: str,
    *,
    limit: int,
    cursor: tuple[datetime, str] | None,
    source: str | None,
    program_id: str | None,
    assignee_id: str | None = None,
) -> list[tuple[Report, ValidationJob | None, User | None, bool | None]]:
    """Reports of the user's active orgs, newest first, with latest job, assignee and whether
    the assignee is still an active member of the report's org."""
    query = (
        select(Report, ValidationJob, User, assignee_active())
        .outerjoin(ValidationJob, ValidationJob.id == Report.latest_job_id)
        .outerjoin(User, User.id == Report.assignee_id)
        .where(Report.org_id.in_(member_org_ids(user_id)))
        .order_by(Report.created_at.desc(), Report.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        moment, last_id = cursor
        query = query.where(
            or_(Report.created_at < moment, and_(Report.created_at == moment, Report.id < last_id))
        )
    if source:
        query = query.where(Report.source == source)
    if program_id:
        query = query.where(Report.program_id == program_id)
    if assignee_id:
        query = query.where(Report.assignee_id == assignee_id)
    return [tuple(row) for row in (await db.execute(query)).all()]
