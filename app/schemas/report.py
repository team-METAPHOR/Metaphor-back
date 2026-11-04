from datetime import datetime

from pydantic import BaseModel

from app.schemas.validation_job import JobBrief


class ReportCreated(BaseModel):
    id: str
    program_id: str
    validation_job_id: str
    status: str
    created_at: datetime


class ReportItem(BaseModel):
    id: str
    program_id: str
    title: str
    source: str
    error_type: str
    origin_kind: str
    duplicate_count: int
    workflow_status: str
    latest_job: JobBrief | None
    created_at: datetime


class ReportList(BaseModel):
    items: list[ReportItem]
    next_cursor: str | None


class ReportDetail(ReportItem):
    target_url: str
    description: str
    expected_result: str
    test_data: str
    has_screenshot: bool
    input_artifact_id: str | None
    # Org members only; None for the reporter view.
    client_context: dict | None = None


class ClarificationCreate(BaseModel):
    model_config = {"extra": "forbid"}

    source_job_id: str
    answers: str


class JobQueued(BaseModel):
    validation_job_id: str
    status: str
