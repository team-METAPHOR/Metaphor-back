from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, Header, Query, UploadFile
from fastapi.responses import JSONResponse

from app.api.dependencies import CurrentUser, DbSession
from app.schemas.report import (
    ClarificationCreate,
    JobQueued,
    JobRequest,
    ReportCreated,
    ReportDetail,
    ReportList,
    WorkflowUpdate,
)
from app.services import clarifications, rerun
from app.services import reports as service
from app.services.intake import read_upload_capped

router = APIRouter(prefix="/reports", tags=["reports"])


@router.post("", status_code=201, response_model=ReportCreated)
async def create_report(
    user: CurrentUser,
    db: DbSession,
    program_id: Annotated[str, Form(max_length=64)],
    target_url: Annotated[str, Form(max_length=2048)],
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    expected_result: Annotated[str, Form()] = "",
    test_data: Annotated[str, Form()] = "",
    mask_boxes: Annotated[str | None, Form(max_length=4096)] = None,
    screenshot: Annotated[UploadFile | None, File()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    image = await read_upload_capped(screenshot) if screenshot is not None else None
    form = service.PlatformReportForm(
        program_id=program_id,
        target_url=target_url,
        title=title,
        description=description,
        expected_result=expected_result,
        test_data=test_data,
        mask_boxes=mask_boxes,
    )
    status, body = await service.submit_report(db, user.id, form, image, idempotency_key)
    return JSONResponse(body, status_code=status)


@router.get("", response_model=ReportList)
async def list_reports(
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    source: Literal["platform", "widget"] | None = None,
    program_id: str | None = None,
):
    return await service.list_reports(
        db, user.id, limit=limit, cursor=cursor, source=source, program_id=program_id
    )


@router.get("/{report_id}", response_model=ReportDetail)
async def get_report(report_id: str, user: CurrentUser, db: DbSession):
    return await service.get_report(db, user.id, report_id)


@router.patch("/{report_id}/workflow", response_model=ReportDetail)
async def set_workflow_status(
    report_id: str, body: WorkflowUpdate, user: CurrentUser, db: DbSession
):
    return await service.set_workflow_status(db, user.id, report_id, body.status)


@router.post("/{report_id}/clarifications", status_code=202, response_model=JobQueued)
async def answer_question(
    report_id: str,
    body: ClarificationCreate,
    user: CurrentUser,
    db: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    status, payload = await clarifications.answer_question(
        db, user.id, report_id, body.source_job_id, body.answers, idempotency_key
    )
    return JSONResponse(payload, status_code=status)


@router.post("/{report_id}/validation-jobs", status_code=202, response_model=JobQueued)
async def request_validation_job(
    report_id: str,
    body: JobRequest,
    user: CurrentUser,
    db: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    status, payload = await rerun.request_job(db, user.id, report_id, body, idempotency_key)
    return JSONResponse(payload, status_code=status)
