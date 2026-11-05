from datetime import datetime
from typing import Literal

from pydantic import BaseModel, model_validator

from app.schemas.validation_job import JobBrief


class ReportCreated(BaseModel):
    id: str
    program_id: str
    # null with status "waiting_quota": stored over the daily quota, no job until a rerun.
    validation_job_id: str | None
    status: str
    created_at: datetime


class Assignee(BaseModel):
    """`active: false` = no longer an active member of the report's org; the assignment is kept
    but display_name/email are withheld (null) so a former member's contact is not exposed."""

    id: str
    display_name: str | None
    email: str | None
    active: bool


Progress = Literal[
    "new",
    "reproducing",
    "reproduced",
    "triaged",
    "verified",
    "waiting_quota",
    "fixing",
    "dismissed",
    "closed",
]


class ReportItem(BaseModel):
    """`progress` is the dashboard stage, computed from `workflow_status`, `fix_verified` and
    the latest job (first matching row wins):

    | workflow_status | latest job                                    | progress      |
    |-----------------|-----------------------------------------------|---------------|
    | closed          | (any)                                         | `closed`      |
    | dismissed       | (any)                                         | `dismissed`   |
    | received/fixing | (any), with `fix_verified`                    | `verified`    |
    | fixing          | (any)                                         | `fixing`      |
    | received        | none, `run_blocked = quota` (parked)          | `waiting_quota` |
    | received        | none                                          | `new`         |
    | received        | queued / running / cancelling                 | `reproducing` |
    | received        | finished, verdict `reproduced`                | `reproduced`  |
    | received        | finished, any other verdict or none           | `triaged`     |

    Finished = completed / inconclusive / needs_info / failed / cancelled. `fix_verified` = the
    latest *finished* job is a retest that ended completed + not_reproduced. Dashboard stages:
    new → 신규 신고, reproducing → 재현 중, reproduced/triaged → 재현 완료, fixing → 버그 수정 중,
    verified → 수정 확인됨 (awaiting a manual close), waiting_quota → 한도 대기 (stored over
    the daily quota; a rerun `fresh` starts it), closed → 마감 (closed by a person, only
    once the fix is verified). dismissed is a manual side exit.
    """

    id: str
    program_id: str
    # Per-org human number; the frontend shows it as BUG-<number>.
    number: int
    title: str
    source: str
    error_type: str
    origin_kind: str
    duplicate_count: int
    workflow_status: str
    progress: Progress
    # Latest finished job is a passing retest; required to close (PATCH workflow "closed").
    fix_verified: bool
    closed_at: datetime | None
    # Shown as is even if the user was later deactivated or left the org.
    assignee: Assignee | None
    latest_job: JobBrief | None
    created_at: datetime


class RequiredAction(BaseModel):
    code: str
    message: str


class BestAttempt(BaseModel):
    """Latest finished job's stored engine result, as redacted plain text (never HTML)."""

    job_id: str
    verdict: str | None
    reason_code: str | None
    summary: str | None
    hypothesis: str | None


class ReportList(BaseModel):
    """Cursor mode (`cursor` given): `total`/`page` are null. Otherwise page mode: `page` is
    the 1-based page and `total` the number of matching reports. `next_cursor` is always set
    when more rows follow, so a page-mode client can also continue with the cursor."""

    items: list[ReportItem]
    next_cursor: str | None
    total: int | None = None
    page: int | None = None


class AttentionItem(BaseModel):
    id: str
    number: int
    title: str
    progress: Progress
    # First required action, or null (e.g. reproduced and only needs review).
    reason: RequiredAction | None = None


class ReportSummary(BaseModel):
    """All of the org's reports (closed included), counted by `progress`; every value is
    present, zero when none. `needs_attention`: newest 5 reports that are reproduced, parked
    over the quota (waiting_quota), or still `received` with a required action from the latest
    finished job (see `required_actions`)."""

    counts: dict[str, int]
    needs_attention: list[AttentionItem]


class PageCount(BaseModel):
    path: str
    count: int


class ReportStats(BaseModel):
    """Over jobs/reports created in the last `days` days (1..90)."""

    days: int
    auto_reproduction_rate: float | None
    avg_reproduction_seconds: float | None
    fix_verified_count: int
    by_page: list[PageCount]


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
    required_actions: list[RequiredAction] = []
    best_attempt: BestAttempt | None = None


class WorkflowUpdate(BaseModel):
    """`closed` is accepted only while `fix_verified` is true (else 409 FIX_NOT_VERIFIED)."""

    model_config = {"extra": "forbid"}

    status: Literal["received", "fixing", "dismissed", "closed"]


class AssigneeUpdate(BaseModel):
    """`user_id: null` unassigns. The user must be an active member of the report's org."""

    model_config = {"extra": "forbid"}

    user_id: str | None


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
