from datetime import datetime

from pydantic import BaseModel


class JobOut(BaseModel):
    """Public job view. No engine id, storage paths or scope snapshot."""

    id: str
    report_id: str
    kind: str
    status: str
    stage: str | None
    verdict: str | None
    reason_code: str | None
    classification: str | None
    questions: list[str]
    error_code: str | None
    backend_error_code: str | None
    regression: str | None
    analysis_available: bool
    human_review_required: bool
    parent_job_id: str | None
    baseline_job_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobList(BaseModel):
    items: list[JobOut]
    next_cursor: str | None


class JobBrief(BaseModel):
    id: str
    kind: str
    status: str
    verdict: str | None


class JobResult(BaseModel):
    job_id: str
    result: dict


class EventOut(BaseModel):
    id: int
    kind: str
    created_at: datetime
    detail: dict


class EventList(BaseModel):
    items: list[EventOut]


class JobCancelled(BaseModel):
    id: str
    status: str  # "cancelled" (was queued) or "cancelling" (worker stopping it)
