"""When the AI provider rate-limited a job, the earliest time a new run is worth trying.

The engine (core_engine 4a710f0) records a numeric `Retry-After` on the failing model call
(`report.model_calls[].retry_after_seconds`, status `MODEL_RATE_LIMITED`); it never retries on
its own. Only that number is read: provider error bodies and keys are never stored.
"""

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import QaResult, Report, ValidationJob

RATE_LIMITED = "MODEL_RATE_LIMITED"
MAX_WAIT_SECONDS = 86400  # same bound as the engine: a longer hint is ignored


def retry_after_seconds(raw: dict | None) -> float | None:
    report = (raw or {}).get("report") or {}
    delays = [
        call["retry_after_seconds"]
        for call in report.get("model_calls") or []
        if isinstance(call, dict)
        and call.get("status") == RATE_LIMITED
        and type(call.get("retry_after_seconds")) in (int, float)
        and 0 <= call["retry_after_seconds"] <= MAX_WAIT_SECONDS
    ]
    return max(delays) if delays else None


def retry_available_at(job: ValidationJob, raw: dict | None) -> datetime | None:
    """`finished_at + Retry-After` for a finished rate-limited job, else None."""
    if job.error_code != RATE_LIMITED or job.finished_at is None:
        return None
    seconds = retry_after_seconds(raw)
    return None if seconds is None else job.finished_at + timedelta(seconds=seconds)


async def job_retry(db: AsyncSession, job: ValidationJob) -> tuple[float | None, datetime | None]:
    if job.error_code != RATE_LIMITED:
        return None, None
    result = await db.get(QaResult, job.id)
    raw = result.raw if result else None
    return retry_after_seconds(raw), retry_available_at(job, raw)


async def enforce_provider_wait(db: AsyncSession, report: Report) -> None:
    """429 PROVIDER_RATE_LIMITED (with Retry-After) while the report's latest job is still
    inside the provider's wait window; a new run now would hit the same limit."""
    if report.latest_job_id is None:
        return
    job = await db.get(ValidationJob, report.latest_job_id)
    _, until = await job_retry(db, job) if job is not None else (None, None)
    if until is None:
        return
    remaining = (until - datetime.now(UTC)).total_seconds()
    if remaining > 0:
        raise ApiError(
            429,
            "PROVIDER_RATE_LIMITED",
            "AI 공급자 사용량 제한이 풀린 뒤 다시 요청해 주세요.",
            detail={"retry_available_at": until.isoformat()},
            headers={"Retry-After": str(math.ceil(remaining))},
        )
