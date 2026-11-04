from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JobEvent, QaResult, ValidationJob


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
