"""Customer-requested re-runs: `fresh` (new analysis) and `retest` (same stored plan)."""

import hashlib
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import Program, ProgramScopeRevision, QaResult, ValidationJob
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
        report, access = await load_visible_report(db, user_id, report_id)
        if access != "org" or await org_role(db, user_id, report.org_id) not in RERUN_ROLES:
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
