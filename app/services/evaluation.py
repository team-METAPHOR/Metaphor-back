"""Evaluation bookkeeping (issue #105). Tags are evaluator-only metadata: nothing here is ever
read by the worker or copied into report text, engine input or report views."""

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import Report, ReportEvalTag, ValidationJob
from app.models.validation_job import ACTIVE_JOB_STATUSES
from app.repositories import evaluation as repo
from app.schemas.evaluation import (
    EvalCondition,
    EvalJob,
    EvalModelCall,
    EvalRecord,
    EvalTag,
    EvalTagUpdate,
    EvalTiming,
)
from app.services.access import WRITE_ROLES, org_role
from app.services.audit import record_audit
from app.services.reports import load_visible_report
from app.services.rerun import AUTO_FRESH_ACTION


def _slot_taken() -> ApiError:
    return ApiError(
        409, "EVAL_SLOT_TAKEN", "같은 평가 실행·사례·반복 번호가 이미 다른 제보에 연결돼 있습니다."
    )


def _out(tag: ReportEvalTag) -> EvalTag:
    return EvalTag(
        report_id=tag.report_id,
        eval_run_id=tag.eval_run_id,
        case_id=tag.case_id,
        repeat_id=tag.repeat_id,
        tagged_at=tag.updated_at,
    )


async def get_eval_tag(db: AsyncSession, user_id: str, report_id: str) -> EvalTag:
    async with db.begin():
        await load_visible_report(db, user_id, report_id)
        tag = await repo.get_tag(db, report_id)
        if tag is None:
            raise ApiError(404, "NOT_FOUND", "평가 태그가 없습니다.")
        return _out(tag)


async def set_eval_tag(
    db: AsyncSession, user_id: str, report_id: str, body: EvalTagUpdate
) -> EvalTag:
    """Attach (or replace) the report's evaluation slot. Developer/admin of the owner org."""
    values = body.model_dump()
    try:
        async with db.begin():
            report = await load_visible_report(db, user_id, report_id)
            if await org_role(db, user_id, report.org_id) not in WRITE_ROLES:
                raise ApiError(403, "FORBIDDEN", "평가 태그를 바꿀 권한이 없습니다.")
            owner = await repo.slot_owner(db, report.org_id, **values)
            if owner not in (None, report.id):
                raise _slot_taken()
            tag = await repo.get_tag(db, report.id)
            if tag is None:
                tag = ReportEvalTag(report_id=report.id, org_id=report.org_id)
                db.add(tag)
            for key, value in values.items():
                setattr(tag, key, value)
            tag.tagged_by = user_id
            await db.flush()
            await record_audit(
                db,
                actor_id=user_id,
                org_id=report.org_id,
                action="report.eval_tag",
                target=report.id,
                detail=values,
            )
            await db.refresh(tag)
            return _out(tag)
    except IntegrityError:
        # A concurrent PUT took the slot between the check and the insert.
        raise _slot_taken() from None


# Engine/worker runtime limits. A provider timeout (MODEL_TIMEOUT) stays a model failure.
TIMEOUT_CODES = frozenset(
    {"QA_RUNTIME_LIMIT", "CONDITION_RUNTIME_LIMIT", "RUNTIME_LIMIT", "WORKER_TIMEOUT"}
)
# Engine statuses that end its event log (plus "interrupted" from a cancelled run).
ENGINE_TERMINAL = ("completed", "needs_info", "inconclusive", "failed", "cancelled", "interrupted")
QA_KINDS = ("initial", "fresh", "resume")


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000)


def _seconds_ms(value) -> int | None:
    return round(value * 1000) if type(value) in (int, float) and value >= 0 else None


def _int(value) -> int | None:
    return value if type(value) is int else None


def _code(value) -> str | None:
    return value if isinstance(value, str) and value else None


def model_calls(records) -> list[EvalModelCall]:
    calls = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else None
        retry = record.get("retry_after_seconds")
        calls.append(
            EvalModelCall(
                phase=_code(record.get("phase")),
                role=_code(record.get("role")),
                status=_code(record.get("status")),
                model=_code(record.get("model")),
                usage_known=usage is not None,
                input_tokens=_int((usage or {}).get("input_tokens")),
                output_tokens=_int((usage or {}).get("output_tokens")),
                latency_ms=_seconds_ms(record.get("elapsed_seconds")),
                retry_after_seconds=retry if type(retry) in (int, float) else None,
            )
        )
    return calls


def _ordered(codes) -> list[str]:
    seen: list[str] = []
    for code in codes:
        if isinstance(code, str) and code and code not in seen:
            seen.append(code)
    return seen


def job_error_codes(job: ValidationJob, parts: dict, calls: list[EvalModelCall]) -> list[str]:
    evidence = parts.get("evidence") if isinstance(parts.get("evidence"), list) else []
    review = parts.get("plan_review") if isinstance(parts.get("plan_review"), dict) else {}
    return _ordered(
        [
            job.backend_error_code,
            job.error_code,
            *(e.get("blocker") for e in evidence if isinstance(e, dict)),
            review.get("error_code"),
            *(c.status for c in calls if c.status not in (None, "completed", "started")),
        ]
    )


def job_timing(
    job: ValidationJob, bounds: tuple[datetime | None, datetime | None], parts: dict
) -> EvalTiming:
    engine_start, engine_end = bounds
    timings = parts.get("timings") if isinstance(parts.get("timings"), dict) else {}
    evidence = parts.get("evidence") if isinstance(parts.get("evidence"), list) else []
    attempts = [
        e["elapsed_seconds"]
        for e in evidence
        if isinstance(e, dict) and type(e.get("elapsed_seconds")) in (int, float)
    ]
    persisted = job.finished_at if job.status not in ACTIVE_JOB_STATUSES else None
    return EvalTiming(
        queued_at=job.created_at,
        claimed_at=job.started_at,
        engine_started_at=engine_start,
        engine_finished_at=engine_end,
        persisted_at=persisted,
        queue_wait_ms=_ms(job.created_at, job.started_at),
        setup_ms=_ms(job.started_at, engine_start),
        engine_ms=_ms(engine_start, engine_end),
        model_ms=_seconds_ms(timings.get("model_seconds")),
        browser_ms=_seconds_ms(sum(attempts)) if attempts else None,
        persist_ms=_ms(engine_end, persisted),
        total_ms=_ms(job.created_at, persisted),
    )


def _outcome(qa_jobs: list[EvalJob], ready: bool) -> str:
    if ready:
        return "report_ready"
    if not qa_jobs:
        return "not_run"
    final = qa_jobs[-1]
    if final.status in ACTIVE_JOB_STATUSES:
        return "unfinished"
    if final.status == "failed" and TIMEOUT_CODES.intersection(final.error_codes):
        return "timeout"
    return final.status  # needs_info / inconclusive / failed / cancelled


async def build_records(
    db: AsyncSession, reports: list[Report], tags: dict[str, ReportEvalTag]
) -> list[EvalRecord]:
    """One record per report, from stored rows only (no engine/worker call). Caller holds the
    transaction and has already checked access."""
    jobs = await repo.jobs_for(db, [r.id for r in reports])
    job_ids = [job.id for job in jobs]
    bounds = await repo.event_bounds(db, job_ids, ENGINE_TERMINAL)
    parts = await repo.result_parts(db, job_ids)
    with_md = await repo.jobs_with_report_md(db, job_ids)
    automatic = await repo.automatic_job_ids(db, job_ids, AUTO_FRESH_ACTION)
    conditions: dict[str, list[EvalCondition]] = {}
    for row in await repo.conditions_for(db, job_ids):
        conditions.setdefault(row.job_id, []).append(
            EvalCondition(
                ordinal=row.ordinal,
                condition_id=row.condition_id,
                status=row.status,
                verdict=row.verdict,
                error_code=row.error_code,
                duration_ms=row.duration_ms,
            )
        )
    by_report: dict[str, list[EvalJob]] = {}
    for job in jobs:
        job_parts = parts.get(job.id) or {}
        calls = model_calls(job_parts.get("model_calls"))
        review = job_parts.get("plan_review")
        by_report.setdefault(job.report_id, []).append(
            EvalJob(
                id=job.id,
                kind=job.kind,
                status=job.status,
                automatic=job.id in automatic,
                parent_job_id=job.parent_job_id,
                baseline_job_id=job.baseline_job_id,
                claims=job.attempts,
                verdict=job.verdict,
                reason_code=job.reason_code,
                error_code=job.error_code,
                backend_error_code=job.backend_error_code,
                error_codes=job_error_codes(job, job_parts, calls),
                result_available=job.id in parts,
                report_md_available=job.id in with_md,
                plan_review=(
                    {k: review.get(k) for k in ("enabled", "status", "error_code")}
                    if isinstance(review, dict)
                    else None
                ),
                timing=job_timing(job, bounds.get(job.id, (None, None)), job_parts),
                model_calls=calls,
                conditions=conditions.get(job.id, []),
                result_url=f"/api/v1/validation-jobs/{job.id}/result",
                artifacts_url=f"/api/v1/validation-jobs/{job.id}/artifacts",
            )
        )
    records = []
    for report in reports:
        report_jobs = by_report.get(report.id, [])
        qa = [job for job in report_jobs if job.kind in QA_KINDS]
        ready = next((job for job in qa if job.status == "completed"), None)
        final = qa[-1] if qa else None
        ready_at = ready.timing.persisted_at if ready else None
        ended_at = None if ready or final is None else final.timing.persisted_at
        tag = tags.get(report.id)
        records.append(
            EvalRecord(
                report_id=report.id,
                report_number=report.number,
                program_id=report.program_id,
                source=report.source,
                eval=_out(tag) if tag else None,
                received_at=report.created_at,
                duplicate_submissions=report.duplicate_count,
                run_blocked=report.run_blocked,
                outcome=_outcome(qa, ready is not None),
                final_job_id=final.id if final else None,
                final_status=final.status if final else None,
                verdict=(ready or final).verdict if (ready or final) else None,
                report_ready_at=ready_at,
                processing_ms=_ms(report.created_at, ready_at),
                ended_at=ended_at,
                ended_ms=_ms(report.created_at, ended_at),
                qa_jobs=len(qa),
                retries=max(len(qa) - 1, 0),
                retests=sum(job.kind == "retest" for job in report_jobs),
                error_codes=_ordered(code for job in qa for code in job.error_codes),
                report_url=f"/api/v1/reports/{report.id}",
                jobs=report_jobs,
            )
        )
    return records


async def get_eval_record(db: AsyncSession, user_id: str, report_id: str) -> EvalRecord:
    async with db.begin():
        report = await load_visible_report(db, user_id, report_id)
        tags = await repo.tags_for(db, [report.id])
        (record,) = await build_records(db, [report], tags)
        return record
