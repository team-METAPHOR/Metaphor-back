from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentUser, DbSession
from app.schemas.validation_job import EventList, JobOut, JobResult
from app.services import validation_jobs as service

router = APIRouter(prefix="/validation-jobs", tags=["validation-jobs"])


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str, user: CurrentUser, db: DbSession):
    return await service.get_job(db, user.id, job_id)


@router.get("/{job_id}/result", response_model=JobResult)
async def get_result(job_id: str, user: CurrentUser, db: DbSession):
    return await service.get_result(db, user.id, job_id)


@router.get("/{job_id}/events", response_model=EventList)
async def list_events(
    job_id: str,
    user: CurrentUser,
    db: DbSession,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    return await service.list_events(db, user.id, job_id, after=after, limit=limit)
