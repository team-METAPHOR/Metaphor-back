from datetime import datetime
from typing import Literal

from pydantic import BaseModel, model_validator

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
    client_context: dict | None = None
    # True once the retention purge blanked the text fields (they read as "").
    scrubbed: bool = False


class ClarificationCreate(BaseModel):
    model_config = {"extra": "forbid"}

    source_job_id: str
    answers: str


class JobQueued(BaseModel):
    validation_job_id: str
    status: str


class JobRequest(BaseModel):
    """fresh: re-analyse from the stored report. retest: replay the baseline's stored plan
    against an approved build. Expectations/steps/selectors are never accepted here."""

    model_config = {"extra": "forbid"}

    mode: Literal["fresh", "retest"]
    baseline_job_id: str | None = None
    app_version: str | None = None

    @model_validator(mode="after")
    def _retest_fields(self):
        if self.mode == "retest" and not (self.baseline_job_id and self.app_version):
            raise ValueError("retest requires baseline_job_id and app_version")
        if self.mode == "fresh" and (self.baseline_job_id or self.app_version):
            raise ValueError("fresh takes no baseline_job_id or app_version")
        return self
