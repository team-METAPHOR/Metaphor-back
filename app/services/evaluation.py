"""Evaluation bookkeeping (issue #105). Tags are evaluator-only metadata: nothing here is ever
read by the worker or copied into report text, engine input or report views."""

import csv
import io
import json
from datetime import UTC, datetime
from typing import get_args

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import Report, ReportEvalTag, ValidationJob
from app.models.validation_job import ACTIVE_JOB_STATUSES
from app.repositories import evaluation as repo
from app.schemas.evaluation import (
    EvalCondition,
    EvalExport,
    EvalJob,
    EvalModelCall,
    EvalModelLimitEvent,
    EvalRecord,
    EvalTag,
    EvalTagUpdate,
    EvalTiming,
    EvalTotals,
    Outcome,
)
from app.services.access import READ_ROLES, WRITE_ROLES, org_role, require_org_role
from app.services.audit import record_audit
from app.services.diagnosis import model_id
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
                model=model_id(record.get("model")),
                configured_model=model_id(record.get("configured_model")),
                usage_known=usage is not None,
                input_tokens=_int((usage or {}).get("input_tokens")),
                output_tokens=_int((usage or {}).get("output_tokens")),
                latency_ms=_seconds_ms(record.get("elapsed_seconds")),
                retry_after_seconds=retry if type(retry) in (int, float) else None,
            )
        )
    return calls


def usage_calls(rows) -> list[EvalModelCall]:
    """Model calls from `model_usage` rows (they outlive the raw result's retention purge)."""
    return [
        EvalModelCall(
            phase=row.phase,
            role=row.role,
            status=row.status,
            model=model_id(row.model),
            configured_model=model_id(row.configured_model),
            usage_known=row.input_tokens is not None or row.output_tokens is not None,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            latency_ms=row.latency_ms,
            retry_after_seconds=row.retry_after_seconds,
        )
        for row in rows
    ]


def model_limit_events(records) -> list[EvalModelLimitEvent]:
    """Budget refusals only (`call_started` false); anything else is not a refusal record."""
    events = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict) or record.get("call_started") is not False:
            continue
        events.append(
            EvalModelLimitEvent(
                phase=_code(record.get("phase")),
                role=_code(record.get("role")),
                configured_model=model_id(record.get("configured_model")),
                error_code=_code(record.get("error_code")),
            )
        )
    return events


def critic_matches(readiness, review) -> bool | None:
    configured = ((readiness or {}).get("critic") or {}).get("enabled")
    ran = review.get("enabled") if isinstance(review, dict) else None
    if type(configured) is not bool or type(ran) is not bool:
        return None
    return configured == ran


def readiness_key(readiness: dict | None) -> str | None:
    """The configuration part of a snapshot (no time, scope or account): equal = same setup."""
    if not isinstance(readiness, dict):
        return None
    keep = {k: readiness.get(k) for k in ("engine", "models", "critic", "limits")}
    return json.dumps(keep, sort_keys=True, default=str)


def _ordered(codes) -> list[str]:
    seen: list[str] = []
    for code in codes:
        if isinstance(code, str) and code and code not in seen:
            seen.append(code)
    return seen


def job_error_codes(
    job: ValidationJob,
    parts: dict,
    calls: list[EvalModelCall],
    limits: list[EvalModelLimitEvent] = (),
) -> list[str]:
    evidence = parts.get("evidence") if isinstance(parts.get("evidence"), list) else []
    review = parts.get("plan_review") if isinstance(parts.get("plan_review"), dict) else {}
    return _ordered(
        [
            job.backend_error_code,
            job.error_code,
            *(e.get("blocker") for e in evidence if isinstance(e, dict)),
            review.get("error_code"),
            *(c.status for c in calls if c.status not in (None, "completed", "started")),
            *(event.error_code for event in limits),
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
    readable = job.report_readable_at
    done = readable or persisted
    return EvalTiming(
        queued_at=job.created_at,
        claimed_at=job.started_at,
        engine_started_at=engine_start,
        engine_finished_at=engine_end,
        persisted_at=persisted,
        report_readable_at=readable,
        queue_wait_ms=_ms(job.created_at, job.started_at),
        setup_ms=_ms(job.started_at, engine_start),
        engine_ms=_ms(engine_start, engine_end),
        model_ms=_seconds_ms(timings.get("model_seconds")),
        browser_ms=_seconds_ms(sum(attempts)) if attempts else None,
        persist_ms=_ms(engine_end, done),
        total_ms=_ms(job.created_at, done),
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
    usage = await repo.model_usage_for(db, job_ids)
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
        source = "result" if job.id in parts else None
        if source is None and usage.get(job.id):
            calls, source = usage_calls(usage[job.id]), "model_usage"
        limits = model_limit_events(job_parts.get("model_limit_events"))
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
                error_codes=job_error_codes(job, job_parts, calls, limits),
                browser_attempts=len(
                    [e for e in job_parts.get("evidence") or [] if isinstance(e, dict)]
                    if isinstance(job_parts.get("evidence"), list)
                    else []
                ),
                result_available=job.id in parts,
                report_md_available=job.id in with_md,
                plan_review=(
                    {k: review.get(k) for k in ("enabled", "status", "error_code")}
                    if isinstance(review, dict)
                    else None
                ),
                timing=job_timing(job, bounds.get(job.id, (None, None)), job_parts),
                model_calls=calls,
                model_calls_source=source,
                model_limit_events=limits,
                readiness=job.readiness,
                critic_config_matches=critic_matches(job.readiness, review),
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
        ready_at = (ready.timing.report_readable_at or ready.timing.persisted_at) if ready else None
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


MAX_EXPORT_REPORTS = 2000


def _totals(items: list[EvalRecord]) -> EvalTotals:
    jobs = [job for item in items for job in item.jobs]
    calls = [call for job in jobs for call in job.model_calls]
    outcomes = {value: 0 for value in get_args(Outcome)}
    for item in items:
        outcomes[item.outcome] += 1
    return EvalTotals(
        reports=len(items),
        duplicate_submissions=sum(item.duplicate_submissions for item in items),
        qa_jobs=sum(item.qa_jobs for item in items),
        retests=sum(item.retests for item in items),
        model_calls=len(calls),
        model_calls_usage_unknown=sum(not call.usage_known for call in calls),
        model_limit_events=sum(len(job.model_limit_events) for job in jobs),
        readiness_configs=len({k for job in jobs if (k := readiness_key(job.readiness))}),
        browser_attempts=sum(job.browser_attempts for job in jobs),
        report_files=sum(job.report_md_available for job in jobs),
        outcomes=outcomes,
    )


async def export_run(db: AsyncSession, user_id: str, org_id: str, eval_run_id: str) -> EvalExport:
    """Every report tagged with `eval_run_id` in the org, whatever its state (success, failure,
    pending, cancelled, not run). Sorted by case, repeat. An unknown run is an empty export."""
    async with db.begin():
        await require_org_role(db, user_id, org_id, *READ_ROLES)
        rows = await repo.run_reports(db, org_id, eval_run_id, MAX_EXPORT_REPORTS + 1)
        if len(rows) > MAX_EXPORT_REPORTS:
            raise ApiError(
                422, "EVAL_RUN_TOO_LARGE", "평가 실행 하나의 제보가 너무 많아 내보낼 수 없습니다."
            )
        items = await build_records(
            db, [report for report, _ in rows], {tag.report_id: tag for _, tag in rows}
        )
    return EvalExport(
        eval_run_id=eval_run_id, generated_at=datetime.now(UTC), totals=_totals(items), items=items
    )


CSV_COLUMNS = (
    "eval_run_id",
    "case_id",
    "repeat_id",
    "report_id",
    "report_number",
    "program_id",
    "source",
    "received_at",
    "outcome",
    "verdict",
    "final_job_id",
    "final_status",
    "report_ready_at",
    "processing_ms",
    "ended_at",
    "ended_ms",
    "qa_jobs",
    "retries",
    "retests",
    "duplicate_submissions",
    "run_blocked",
    "error_codes",
    "model_calls",
    "failed_model_calls",
    "usage_unknown_calls",
    "model_limit_events",
    "input_tokens",
    "output_tokens",
    "models",
    "configured_models",
    "critic_enabled",
    "queue_wait_ms",
    "setup_ms",
    "engine_ms",
    "model_ms",
    "browser_ms",
    "persist_ms",
    "browser_attempts",
    "engine_version",
    "engine_commit",
    "primary_model",
    "critic_model",
    "critic_configured",
    "critic_config_matches",
    "report_url",
)


def csv_cell(value) -> str:
    """Plain text cell; a string that a spreadsheet would run as a formula is quoted."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _sum_known(values: list[int | None]) -> int | None:
    """Token sum over calls that reported usage; None when none did (unknown, not zero)."""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def csv_row(item: EvalRecord) -> dict:
    qa = [job for job in item.jobs if job.kind in QA_KINDS]
    calls = [call for job in qa for call in job.model_calls]
    # Per-job spans of the job that decided the outcome (first completed, else the last QA job).
    decisive = next((job for job in qa if job.status == "completed"), qa[-1] if qa else None)
    timing = decisive.timing if decisive else None
    review = (decisive.plan_review or {}) if decisive else {}
    ready = (decisive.readiness or {}) if decisive else {}
    engine, models = ready.get("engine") or {}, ready.get("models") or {}
    return {
        "eval_run_id": item.eval.eval_run_id if item.eval else None,
        "case_id": item.eval.case_id if item.eval else None,
        "repeat_id": item.eval.repeat_id if item.eval else None,
        "report_id": item.report_id,
        "report_number": item.report_number,
        "program_id": item.program_id,
        "source": item.source,
        "received_at": item.received_at,
        "outcome": item.outcome,
        "verdict": item.verdict,
        "final_job_id": item.final_job_id,
        "final_status": item.final_status,
        "report_ready_at": item.report_ready_at,
        "processing_ms": item.processing_ms,
        "ended_at": item.ended_at,
        "ended_ms": item.ended_ms,
        "qa_jobs": item.qa_jobs,
        "retries": item.retries,
        "retests": item.retests,
        "duplicate_submissions": item.duplicate_submissions,
        "run_blocked": item.run_blocked,
        "error_codes": "|".join(item.error_codes),
        "model_calls": len(calls),
        "failed_model_calls": sum(c.status not in ("completed", None) for c in calls),
        "usage_unknown_calls": sum(not c.usage_known for c in calls),
        "model_limit_events": sum(len(job.model_limit_events) for job in qa),
        "input_tokens": _sum_known([c.input_tokens for c in calls]),
        "output_tokens": _sum_known([c.output_tokens for c in calls]),
        "models": "|".join(_ordered(c.model for c in calls)),
        "configured_models": "|".join(_ordered(c.configured_model for c in calls)),
        "critic_enabled": review.get("enabled"),
        "queue_wait_ms": timing.queue_wait_ms if timing else None,
        "setup_ms": timing.setup_ms if timing else None,
        "engine_ms": timing.engine_ms if timing else None,
        "model_ms": timing.model_ms if timing else None,
        "browser_ms": timing.browser_ms if timing else None,
        "persist_ms": timing.persist_ms if timing else None,
        "browser_attempts": sum(job.browser_attempts for job in qa),
        "engine_version": engine.get("package_version"),
        "engine_commit": engine.get("commit"),
        "primary_model": (models.get("primary") or {}).get("model"),
        "critic_model": (models.get("critic") or {}).get("model"),
        "critic_configured": (ready.get("critic") or {}).get("enabled"),
        "critic_config_matches": decisive.critic_config_matches if decisive else None,
        "report_url": item.report_url,
    }


def export_csv(export: EvalExport) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for item in export.items:
        writer.writerow({key: csv_cell(value) for key, value in csv_row(item).items()})
    return out.getvalue()
