"""Re-runs: customer-requested `fresh` (new analysis) and `retest` (same stored plan), plus the
automatic `fresh` that regenerates the scenario after an inconclusive result."""

import hashlib
import json
from urllib.parse import urlsplit

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.text import redact
from app.models import AuditEvent, Program, ProgramScopeRevision, QaResult, Report, ValidationJob
from app.schemas.report import JobRequest
from app.services.access import org_role
from app.services.audit import record_audit
from app.services.idempotency import run_once
from app.services.jobs import create_job, enforce_job_request_limit
from app.services.reports import load_visible_report

RERUN_ROLES = ("reviewer", "admin")
COMPARABLE_VERDICTS = ("reproduced", "not_reproduced")


async def _retest_baseline(
    db: AsyncSession, report_id: str, program: Program, body: JobRequest
) -> tuple[ValidationJob, dict]:
    baseline = await db.get(ValidationJob, body.baseline_job_id)
    if baseline is None or baseline.report_id != report_id:
        raise ApiError(404, "NOT_FOUND", "기준 검증 작업을 찾을 수 없습니다.")
    # Product policy (stricter than the engine): only a confirmed verdict is a baseline.
    if (
        baseline.status != "completed"
        or baseline.verdict not in COMPARABLE_VERDICTS
        or baseline.engine_job_id is None
        or await db.get(QaResult, baseline.id) is None
    ):
        raise ApiError(
            409,
            "BASELINE_NOT_COMPARABLE",
            "재현 여부가 확정된 작업만 재검증 기준이 될 수 있습니다.",
        )
    if baseline.scope_revision != program.current_revision:
        raise ApiError(
            409,
            "SCOPE_CHANGED_USE_FRESH",
            "승인 범위가 바뀌어 같은 조건으로 비교할 수 없습니다. 새 검증(fresh)을 요청하세요.",
        )
    revision = await db.get(ProgramScopeRevision, (program.id, program.current_revision))
    if body.app_version not in revision.approved_app_versions:
        raise ApiError(422, "APP_VERSION_NOT_APPROVED", "승인된 테스트 배포 버전이 아닙니다.")
    # Same viewport/test context/session mode as the baseline; only the build changes.
    return baseline, {**baseline.scope_snapshot, "app_version": body.app_version}


async def request_job(
    db: AsyncSession, user_id: str, report_id: str, body: JobRequest, idempotency_key: str | None
) -> tuple[int, dict]:
    request_hash = hashlib.sha256(
        json.dumps(body.model_dump(), sort_keys=True).encode()
    ).hexdigest()
    async with db.begin():
        report = await load_visible_report(db, user_id, report_id)
        if await org_role(db, user_id, report.org_id) not in RERUN_ROLES:
            raise ApiError(403, "FORBIDDEN", "재실행 권한이 없습니다.")

        async def create() -> tuple[int, dict]:
            await enforce_job_request_limit(db, user_id)
            program = await db.get(Program, report.program_id)
            if program.status != "active" or not program.external_ai_allowed:
                raise ApiError(
                    422, "PROGRAM_NOT_ACTIVE", "현재 검증을 실행할 수 없는 프로그램입니다."
                )
            if body.mode == "fresh":
                job = await create_job(db, report, "fresh")
            else:
                baseline, snapshot = await _retest_baseline(db, report.id, program, body)
                job = await create_job(
                    db,
                    report,
                    "retest",
                    baseline=baseline,
                    app_version=body.app_version,
                    snapshot=snapshot,
                )
            await record_audit(
                db,
                actor_id=user_id,
                org_id=report.org_id,
                action=f"report.{body.mode}",
                target=report.id,
                detail={"job_id": job.id, "baseline_job_id": body.baseline_job_id},
            )
            return 202, {"validation_job_id": job.id, "status": job.status}

        return await run_once(
            db,
            user_id=user_id,
            scope=f"jobs:{report.id}",
            key=idempotency_key,
            request_hash=request_hash,
            fn=create,
        )


AUTO_FRESH_ACTION = "job.auto_fresh"
# Sources an automatic fresh may follow. A customer-requested fresh is never auto-retried.
AUTO_FRESH_SOURCE_KINDS = ("initial", "resume")
# create_job refusals that mean "not now": active job, scrubbed report, daily limit.
AUTO_FRESH_SKIP_STATUSES = (409, 410, 429)
NOTE_HEADER = "[이전 재현 시도] 아래 계획은 결론을 내지 못했습니다. 다른 해석·경로로 시도하세요."
NOTE_MAX_STEPS = 20
NOTE_MAX_TARGET = 200


def _step_target(step: dict) -> str:
    target = str(step.get("target") or "")
    if step.get("action") == "navigate":
        # Path only: the origin is fixed by the scope, and the query may carry user data.
        target = urlsplit(target).path or "/"
    return " ".join(target.split())[:NOTE_MAX_TARGET]


def previous_attempt_note(raw: dict) -> str:
    """Summarize the plan that ended inconclusive (steps, failed step, blocker) for a fresh run.

    The text is appended to the engine description of the regenerated scenario."""
    report = raw.get("report") or {}
    steps = list(((report.get("scenario") or {}).get("steps")) or [])
    evidence = list(((report.get("run") or {}).get("evidence")) or [])
    failed = next(
        (ev for ev in evidence if ev.get("blocker") and ev.get("completed_steps", 0) < len(steps)),
        None,
    )
    lines = [NOTE_HEADER]
    for index, step in enumerate(steps[:NOTE_MAX_STEPS]):
        line = f"{index + 1}. {step.get('action', '')} {_step_target(step)}".rstrip()
        if failed is not None and failed["completed_steps"] == index:
            line += f"  ← 실패: {failed['blocker']}"
        lines.append(line)
    if failed is None or failed["completed_steps"] >= NOTE_MAX_STEPS:
        code = next((ev["blocker"] for ev in evidence if ev.get("blocker")), None)
        code = code or raw.get("error_code")
        if code:
            lines.append(f"결론 없음 사유: {code}")
    return redact("\n".join(lines))


def _auto_fresh_of(job_id: str):
    return exists().where(AuditEvent.action == AUTO_FRESH_ACTION, AuditEvent.target == job_id)


async def auto_fresh(session: AsyncSession, job_id: str) -> str | None:
    """Queue one automatic `fresh` job after `job_id` ended inconclusive; return its id.

    Returns None (nothing created) unless the job is inconclusive, is an initial/resume job or
    itself an automatic fresh, is still the report's latest job, and the report has fewer
    automatic fresh jobs than `program.auto_fresh_retries`. Limits enforced by `create_job`
    (409 active job, 410 scrubbed report, 429 daily limit) also skip silently.
    Automatic jobs are counted by their `job.auto_fresh` audit rows (target = new job id).
    """
    try:
        async with session.begin():
            job = await session.get(ValidationJob, job_id)
            if job is None or job.status != "inconclusive":
                return None
            report = await session.get(Report, job.report_id)
            program = await session.get(Program, report.program_id)
            if report.latest_job_id != job.id:
                return None
            if program.status != "active" or not program.external_ai_allowed:
                return None
            if job.kind not in AUTO_FRESH_SOURCE_KINDS and not await session.scalar(
                select(_auto_fresh_of(job.id))
            ):
                return None
            used = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .join(ValidationJob, ValidationJob.id == AuditEvent.target)
                .where(
                    AuditEvent.action == AUTO_FRESH_ACTION,
                    ValidationJob.report_id == report.id,
                )
            )
            if used >= program.auto_fresh_retries:
                return None
            result = await session.get(QaResult, job.id)
            note = previous_attempt_note(result.raw if result else {})
            fresh = await create_job(session, report, "fresh", parent=job, answers=note)
            await record_audit(
                session,
                actor_id=None,
                org_id=report.org_id,
                action=AUTO_FRESH_ACTION,
                target=fresh.id,
                detail={"report_id": report.id, "source_job_id": job.id},
            )
            return fresh.id
    except ApiError as error:
        if error.status in AUTO_FRESH_SKIP_STATUSES:
            return None
        raise
