from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Opaque labels only: no spaces or free text, so an answer key or reference plan can't be
# smuggled in. Validation errors never echo the value (app/core/errors.py).
EvalId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]


class EvalTagUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_run_id: EvalId
    case_id: EvalId
    repeat_id: EvalId


class EvalTag(BaseModel):
    report_id: str
    eval_run_id: str
    case_id: str
    repeat_id: str
    tagged_at: datetime


Outcome = Literal[
    "report_ready",  # a QA job completed: a decided report is readable
    "needs_info",
    "inconclusive",
    "failed",
    "timeout",  # engine/worker runtime limit (see TIMEOUT_CODES)
    "cancelled",
    "unfinished",  # latest QA job still queued/running/cancelling
    "not_run",  # no QA job (e.g. parked over the daily quota)
]


class EvalModelCall(BaseModel):
    """One engine model call, from the stored (redacted) engine result."""

    phase: str | None  # intake / plan / plan_review / repair / report ...; null on matrix records
    role: str | None  # primary / critic
    status: str | None  # completed or the engine's error code (MODEL_RATE_LIMITED, ...)
    model: str | None  # model id the provider answered with; null when the call failed
    configured_model: str | None = None  # worker-configured id; filled once captured (#105)
    usage_known: bool
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    retry_after_seconds: float | None


class EvalTiming(BaseModel):
    """UTC timestamps and derived spans (ms). null = not reached or not reported."""

    queued_at: datetime
    claimed_at: datetime | None  # worker claim (validation_jobs.started_at)
    engine_started_at: datetime | None  # first engine event
    engine_finished_at: datetime | None  # engine's terminal event
    persisted_at: datetime | None  # result transaction (validation_jobs.finished_at)
    queue_wait_ms: int | None  # queued → claimed
    setup_ms: int | None  # claimed → engine start (context lock, test account, browser setup)
    engine_ms: int | None  # engine start → engine end
    model_ms: int | None  # sum of model call latencies (engine timings.model_seconds)
    browser_ms: int | None  # sum of the browser attempts' elapsed time
    login_reset_ms: int | None = None  # not reported separately by the engine: unknown
    persist_ms: int | None  # engine end → result persisted
    total_ms: int | None  # queued → persisted


class EvalCondition(BaseModel):
    ordinal: int
    condition_id: str | None
    status: str
    verdict: str | None
    error_code: str | None
    duration_ms: int | None


class EvalJob(BaseModel):
    id: str
    kind: str
    status: str
    automatic: bool  # automatic fresh after an inconclusive run
    parent_job_id: str | None
    baseline_job_id: str | None
    claims: int  # worker claims (validation_jobs.attempts)
    verdict: str | None
    reason_code: str | None
    error_code: str | None  # engine
    backend_error_code: str | None  # worker/queue
    # Every distinct code seen in this job, in order: backend, engine, attempt blockers, plan
    # review, failed model calls. Nothing is merged into a single cause.
    error_codes: list[str]
    browser_attempts: int  # browser reproduction attempts in the stored result
    result_available: bool  # stored engine result (false after retention purge or no result)
    report_md_available: bool
    plan_review: dict | None  # {enabled, status, error_code}
    timing: EvalTiming
    model_calls: list[EvalModelCall]
    conditions: list[EvalCondition]
    result_url: str
    artifacts_url: str


class EvalRecord(BaseModel):
    report_id: str
    report_number: int
    program_id: str
    source: str
    eval: EvalTag | None
    received_at: datetime
    # Widget submissions merged into this report; never counted as new QA runs.
    duplicate_submissions: int
    run_blocked: str | None
    outcome: Outcome
    final_job_id: str | None
    final_status: str | None
    verdict: str | None
    # 1건 처리 시간: received_at → first completed QA job persisted. Only for report_ready.
    report_ready_at: datetime | None
    processing_ms: int | None
    # Failures, timeouts, cancellations, needs_info, inconclusive: when the last QA job ended.
    ended_at: datetime | None
    ended_ms: int | None
    qa_jobs: int  # initial/fresh/resume jobs
    retries: int  # qa_jobs - 1
    retests: int  # fix verification jobs, reported apart from QA
    error_codes: list[str]
    report_url: str
    jobs: list[EvalJob]


class EvalTotals(BaseModel):
    reports: int  # tagged reports (each slot once)
    duplicate_submissions: int
    qa_jobs: int
    retests: int
    model_calls: int
    model_calls_usage_unknown: int
    browser_attempts: int
    report_files: int  # jobs with a stored report.md
    outcomes: dict[str, int]


class EvalExport(BaseModel):
    eval_run_id: str
    generated_at: datetime
    totals: EvalTotals
    items: list[EvalRecord]
