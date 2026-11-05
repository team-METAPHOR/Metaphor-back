"""Read side of validation jobs (status, result, events); org members of the report only."""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import ValidationJob
from app.models.validation_job import ACTIVE_JOB_STATUSES
from app.repositories import validation_jobs as jobs_repo
from app.schemas.validation_job import EventList, EventOut, JobOut, JobResult
from app.services.access import org_role
from app.services.audit import record_audit
from app.services.reports import load_visible_report


async def load_visible_job(db: AsyncSession, user_id: str, job_id: str):
    job = await jobs_repo.get(db, job_id)
    if job is None:
        raise ApiError(404, "NOT_FOUND", "검증 작업을 찾을 수 없습니다.")
    report = await load_visible_report(db, user_id, job.report_id)
    return job, report


async def get_job(db: AsyncSession, user_id: str, job_id: str) -> JobOut:
    async with db.begin():
        job, _ = await load_visible_job(db, user_id, job_id)
        return JobOut.model_validate(job, from_attributes=True)


async def get_result(db: AsyncSession, user_id: str, job_id: str) -> JobResult:
    """Engine result and evidence metadata."""
    async with db.begin():
        job, report = await load_visible_job(db, user_id, job_id)
        result = await jobs_repo.get_result(db, job.id)
        if (
            result is None
            and report.scrubbed_at is not None
            and job.status not in ACTIVE_JOB_STATUSES
        ):
            raise ApiError(410, "RESULT_EXPIRED", "보존 기간이 지나 검증 결과가 삭제되었습니다.")
        if result is None:
            raise ApiError(409, "RESULT_NOT_READY", "아직 검증 결과가 없습니다.")
        return JobResult(job_id=job.id, result=result.raw)


async def list_events(
    db: AsyncSession, user_id: str, job_id: str, *, after: int, limit: int
) -> EventList:
    async with db.begin():
        job, _ = await load_visible_job(db, user_id, job_id)
        events = await jobs_repo.events_after(db, job.id, after, limit)
        return EventList(items=[EventOut.model_validate(e, from_attributes=True) for e in events])


CANCEL_ROLES = ("reviewer", "admin")


async def cancel_job(db: AsyncSession, user_id: str, job_id: str) -> dict:
    """queued → cancelled at once; running → cancelling (the worker confirms the stop).
    Conditional updates make cancel-vs-finish races resolve to exactly one terminal state."""
    async with db.begin():
        job, report = await load_visible_job(db, user_id, job_id)
        if await org_role(db, user_id, report.org_id) not in CANCEL_ROLES:
            raise ApiError(403, "FORBIDDEN", "작업을 취소할 권한이 없습니다.")

        now = func.now()
        cancelled = await db.execute(
            update(ValidationJob)
            .where(ValidationJob.id == job.id, ValidationJob.status == "queued")
            .values(status="cancelled", cancel_requested_at=now, finished_at=now)
        )
        if cancelled.rowcount == 0:
            await db.execute(
                update(ValidationJob)
                .where(ValidationJob.id == job.id, ValidationJob.status == "running")
                .values(status="cancelling", cancel_requested_at=now)
            )
        status = await db.scalar(select(ValidationJob.status).where(ValidationJob.id == job.id))
        if status not in ("cancelled", "cancelling") or (
            status == "cancelled" and cancelled.rowcount == 0
        ):
            raise ApiError(
                409,
                "JOB_ALREADY_FINISHED",
                "이미 종료된 작업입니다.",
                detail={"status": status},
            )
        await record_audit(
            db,
            actor_id=user_id,
            org_id=report.org_id,
            action="job.cancel",
            target=job.id,
            detail={"status": status},
        )
        return {"id": job.id, "status": status}
