from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JobEvent, QaResult, ValidationJob, ValidationJobCondition


async def get(db: AsyncSession, job_id: str) -> ValidationJob | None:
    return await db.get(ValidationJob, job_id)


async def get_result(db: AsyncSession, job_id: str) -> QaResult | None:
    return await db.get(QaResult, job_id)


async def events_after(db: AsyncSession, job_id: str, after: int, limit: int) -> list[JobEvent]:
    query = (
        select(JobEvent)
        .where(JobEvent.job_id == job_id, JobEvent.id > after)
        .order_by(JobEvent.id)
        .limit(limit)
    )
    return list((await db.scalars(query)).all())


async def list_for_report(
    db: AsyncSession, report_id: str, limit: int, cursor: tuple[datetime, str] | None
) -> list[ValidationJob]:
    """Newest first; up to `limit + 1` rows."""
    query = (
        select(ValidationJob)
        .where(ValidationJob.report_id == report_id)
        .order_by(ValidationJob.created_at.desc(), ValidationJob.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        moment, last_id = cursor
        query = query.where(
            or_(
                ValidationJob.created_at < moment,
                and_(ValidationJob.created_at == moment, ValidationJob.id < last_id),
            )
        )
    return list((await db.scalars(query)).all())


async def condition_rows(db: AsyncSession, job_id: str) -> list[ValidationJobCondition]:
    query = (
        select(ValidationJobCondition)
        .where(ValidationJobCondition.job_id == job_id)
        .order_by(ValidationJobCondition.ordinal)
    )
    return list((await db.scalars(query)).all())
