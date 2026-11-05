from datetime import datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, Organization, Report, ValidationJob


async def get(db: AsyncSession, report_id: str) -> Report | None:
    return await db.get(Report, report_id)


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


async def list_visible(
    db: AsyncSession,
    user_id: str,
    *,
    limit: int,
    cursor: tuple[datetime, str] | None,
    source: str | None,
    program_id: str | None,
) -> list[tuple[Report, ValidationJob | None]]:
    """Reports of the user's active orgs, newest first."""
    query = (
        select(Report, ValidationJob)
        .outerjoin(ValidationJob, ValidationJob.id == Report.latest_job_id)
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
    return [tuple(row) for row in (await db.execute(query)).all()]
