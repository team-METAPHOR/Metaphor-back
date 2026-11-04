"""Shared job creation. Every validation job is created here (daily limit, one active job)."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.ids import new_id
from app.models import Program, ProgramScopeRevision, Report, ValidationJob
from app.models.validation_job import JOB_KINDS


def scope_snapshot(program: Program, revision: ProgramScopeRevision) -> dict:
    """RunPolicy + BrowserOptions inputs frozen for one job. Secret *references* only."""
    return {
        "program_id": program.id,
        "org_id": program.owner_org_id,
        "scope_revision": revision.revision,
        "base_url": revision.base_url,
        "allowed_path_prefixes": list(revision.allowed_path_prefixes),
        "excluded_path_prefixes": list(revision.excluded_path_prefixes),
        "allow_private_targets": revision.allow_private_targets,
        "private_cidrs": list(revision.private_cidrs),
        "width": revision.width,
        "height": revision.height,
        "app_version": revision.app_version,
        "test_context_id": revision.test_context_id,
        "mask_selectors": list(revision.mask_selectors),
        "session_secret_ref": revision.session_secret_ref,
        "reset_adapter": revision.reset_adapter,
    }


async def create_job(
    session: AsyncSession,
    report: Report,
    kind: str,
    *,
    parent: ValidationJob | None = None,
    baseline: ValidationJob | None = None,
    answers: str | None = None,
    app_version: str | None = None,
    snapshot: dict | None = None,
) -> ValidationJob:
    """Queue a job for `report` inside the caller's transaction.

    Raises 410 REPORT_EXPIRED (scrubbed by the retention purge), 429 DAILY_LIMIT_EXCEEDED
    (per program, UTC day) or 409 ACTIVE_JOB_EXISTS.
    `snapshot` overrides the current revision (retest reuses its baseline's snapshot).
    """
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown job kind: {kind}")

    # Row lock serializes concurrent creations for the program so the daily count is exact.
    program = await session.scalar(
        select(Program).where(Program.id == report.program_id).with_for_update()
    )
    # Report row lock (held to commit, so it also covers the caller's clarification insert) makes
    # this mutually exclusive with the purge scrub, which locks the same row. Re-read scrubbed_at
    # under the lock: the caller's Report may predate the scrub.
    scrubbed_at = await session.scalar(
        select(Report.scrubbed_at).where(Report.id == report.id).with_for_update()
    )
    if scrubbed_at is not None:
        # Text, raw results and answers are gone; a new run would have nothing to work from.
        raise ApiError(410, "REPORT_EXPIRED", "보존 기간이 지난 신고는 다시 검증할 수 없습니다.")
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    used = await session.scalar(
        select(func.count())
        .select_from(ValidationJob)
        .join(Report, Report.id == ValidationJob.report_id)
        .where(Report.program_id == program.id, ValidationJob.created_at >= day_start)
    )
    if used >= program.daily_job_limit:
        raise ApiError(429, "DAILY_LIMIT_EXCEEDED", "오늘의 검증 실행 한도를 초과했습니다.")

    if snapshot is None:
        revision = await session.get(ProgramScopeRevision, (program.id, program.current_revision))
        snapshot = scope_snapshot(program, revision)
        if app_version is not None:
            snapshot["app_version"] = app_version

    job = ValidationJob(
        id=new_id(),
        report_id=report.id,
        org_id=report.org_id,
        kind=kind,
        status="queued",
        parent_job_id=parent.id if parent else None,
        baseline_job_id=baseline.id if baseline else None,
        scope_revision=snapshot.get("scope_revision", program.current_revision),
        scope_snapshot=snapshot,
        answers=answers,
        app_version=app_version,
    )
    try:
        async with session.begin_nested():
            session.add(job)
            await session.flush()
    except IntegrityError as error:
        if "uq_validation_jobs_active_report" not in str(error.orig):
            raise
        raise ApiError(
            409, "ACTIVE_JOB_EXISTS", "이 제보에 이미 진행 중인 검증 작업이 있습니다."
        ) from None
    report.latest_job_id = job.id
    await session.flush()
    return job
