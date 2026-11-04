"""Read side of validation jobs (status, result, events) with report-level visibility."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import ValidationJob
from app.repositories import validation_jobs as jobs_repo
from app.schemas.validation_job import EventList, EventOut, JobOut, JobResult
from app.services.reports import load_visible_report


def job_out(job: ValidationJob) -> JobOut:
    return JobOut.model_validate(job, from_attributes=True)


async def load_visible_job(db: AsyncSession, user_id: str, job_id: str):
    job = await jobs_repo.get(db, job_id)
    if job is None:
        raise ApiError(404, "NOT_FOUND", "검증 작업을 찾을 수 없습니다.")
    report, access = await load_visible_report(db, user_id, job.report_id)
    return job, report, access


async def get_job(db: AsyncSession, user_id: str, job_id: str) -> JobOut:
    async with db.begin():
        job, _, _ = await load_visible_job(db, user_id, job_id)
        return job_out(job)


async def get_result(db: AsyncSession, user_id: str, job_id: str) -> JobResult:
    """Engine result and evidence metadata: customer org members only."""
    async with db.begin():
        job, _, access = await load_visible_job(db, user_id, job_id)
        if access != "org":
            raise ApiError(403, "FORBIDDEN", "검증 결과는 고객사 구성원만 볼 수 있습니다.")
        result = await jobs_repo.get_result(db, job.id)
        if result is None:
            raise ApiError(409, "RESULT_NOT_READY", "아직 검증 결과가 없습니다.")
        return JobResult(job_id=job.id, result=result.raw)


async def list_events(
    db: AsyncSession, user_id: str, job_id: str, *, after: int, limit: int
) -> EventList:
    async with db.begin():
        job, _, _ = await load_visible_job(db, user_id, job_id)
        events = await jobs_repo.events_after(db, job.id, after, limit)
        return EventList(items=[EventOut.model_validate(e, from_attributes=True) for e in events])
