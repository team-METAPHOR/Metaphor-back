from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.evidence import Diagnosis


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
    has_conditions: bool  # condition matrix job: per-condition rows at /conditions
    # MODEL_RATE_LIMITED only: the provider's Retry-After and the time a rerun is accepted.
    retry_after_seconds: float | None = None
    retry_available_at: datetime | None = None
    # Queued automatic retry after a provider 429: not started before this time.
    run_after: datetime | None = None
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


ConditionOutcome = Literal[
    "reproduced", "not_reproduced", "blocked", "inconclusive", "not_run", "running"
]


class ConditionInfo(BaseModel):
    """The registered condition as frozen in the job snapshot (no session reference)."""

    label: str
    browser_name: str
    width: int
    height: int
    response_delay_ms: int
    test_context_id: str
    record_video: bool
    has_session: bool


class ConditionResultItem(BaseModel):
    index: int  # 1-based run order
    condition_id: str
    condition: ConditionInfo | None
    outcome: ConditionOutcome
    status: str
    verdict: str | None
    error_code: str | None
    stop_point: str | None  # plain text; never contains query strings or typed values
    duration_ms: int | None
    # The child's execution diagnosis (same whitelist as the developer handoff), or null.
    diagnosis: Diagnosis | None = None


class ConditionResultList(BaseModel):
    items: list[ConditionResultItem]
    attempted: int  # conditions that actually ran (not `not_run`)
    reproduced: int
    matched_condition_ids: list[str]
    # Conditions with the most completed steps. Step count only, NOT symptom similarity.
    most_progressed_condition_ids: list[str]
    progress_basis: str
    not_selected_condition_ids: list[str]
