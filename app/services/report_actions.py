"""What a person must do before a report can move on (`required_actions`), and a plain-text
summary of the latest engine attempt (`best_attempt`).

Only codes the engine (core_engine d04c232) or our worker actually emit are mapped; anything
else yields no action rather than a guess.
"""

from sqlalchemy import ColumnElement, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.text import redact
from app.models import Program, ProgramScopeRevision, QaResult, ValidationJob
from app.models.validation_job import ACTIVE_JOB_STATUSES
from app.repositories import programs as programs_repo
from app.schemas.report import BestAttempt, RequiredAction
from app.services.origin_verification import origin_of

ACTION_MESSAGES = {
    "VERIFY_ORIGIN": "테스트 대상 도메인의 소유 확인이 필요합니다",
    "WAIT_FOR_QUOTA": "오늘 실행 한도에 도달해 한도가 풀리면 다시 시도합니다",
    "TEST_ACCOUNT_REQUIRED": "테스트 계정을 등록하면 다시 시도합니다",
    "CHECK_TARGET_URL": "테스트 대상 URL과 허용 범위를 확인해 주세요",
    "ANSWER_QUESTIONS": "추가 질문에 답하면 다시 검증합니다",
}

CODE_ACTIONS = {
    # Worker (app/worker/runner.py): the program's test session secret is missing or malformed.
    "SESSION_SECRET_MISSING": "TEST_ACCOUNT_REQUIRED",
    "SESSION_SECRET_INVALID": "TEST_ACCOUNT_REQUIRED",
    # Engine RunPolicy / browser session: the target is outside scope or cannot be opened.
    "ORIGIN_OUT_OF_SCOPE": "CHECK_TARGET_URL",
    "PATH_OUT_OF_SCOPE": "CHECK_TARGET_URL",
    "PATH_EXCLUDED": "CHECK_TARGET_URL",
    "INVALID_URL": "CHECK_TARGET_URL",
    "INVALID_HOST": "CHECK_TARGET_URL",
    "DNS_FAILED": "CHECK_TARGET_URL",
    "DESTINATION_BLOCKED": "CHECK_TARGET_URL",
    "PRIVATE_DESTINATION_BLOCKED": "CHECK_TARGET_URL",
    "NAVIGATION_FAILED": "CHECK_TARGET_URL",
}

# Display order when several apply.
_ORDER = tuple(ACTION_MESSAGES)


def _blockers(raw: dict | None) -> list[str]:
    run = ((raw or {}).get("report") or {}).get("run") or {}
    return [ev.get("blocker") for ev in run.get("evidence") or [] if isinstance(ev, dict)]


def derive_actions(
    workflow_status: str,
    job: ValidationJob | None,
    raw: dict | None,
    *,
    origin_verified: bool = True,
    run_blocked: str | None = None,
) -> list[RequiredAction]:
    """Actions for an open report. `run_blocked` is the report's (parked over the quota: no job
    yet). Job-based actions come from the latest job once it has finished; while it runs they
    would describe an attempt already being replaced."""
    if workflow_status in ("closed", "dismissed"):
        return []
    codes: set[str] = set()
    if not origin_verified:
        codes.add("VERIFY_ORIGIN")
    if run_blocked == "quota":
        codes.add("WAIT_FOR_QUOTA")
    if job is not None:
        if job.status not in ACTIVE_JOB_STATUSES:
            if job.status == "needs_info":
                codes.add("ANSWER_QUESTIONS")
            for code in (job.backend_error_code, job.error_code, *_blockers(raw)):
                if code in CODE_ACTIONS:
                    codes.add(CODE_ACTIONS[code])
    return [RequiredAction(code=c, message=ACTION_MESSAGES[c]) for c in _ORDER if c in codes]


def _text(value) -> str | None:
    return redact(value) if isinstance(value, str) and value.strip() else None


def best_attempt(job: ValidationJob | None, raw: dict | None) -> BestAttempt | None:
    """Verdict and AI summary of the latest finished job's stored result, as plain text."""
    if job is None or raw is None:
        return None
    report = raw.get("report") or {}
    verdict = (report.get("run") or {}).get("verdict") or {}
    analysis = report.get("analysis") or {}
    hypotheses = analysis.get("hypotheses") or []
    first = hypotheses[0] if hypotheses and isinstance(hypotheses[0], dict) else {}
    return BestAttempt(
        job_id=job.id,
        verdict=verdict.get("value") or job.verdict,
        reason_code=verdict.get("reason_code") or job.reason_code,
        summary=_text(analysis.get("summary")),
        hypothesis=_text(first.get("description")),
    )


async def origin_verified(db: AsyncSession, program_id: str) -> bool:
    """Whether the program's current scope origin is ownership-verified (jobs can run)."""
    program = await db.get(Program, program_id)
    revision = await db.get(ProgramScopeRevision, (program.id, program.current_revision))
    return await programs_repo.is_origin_verified(db, program.id, origin_of(revision.base_url))


async def load_result(db: AsyncSession, job: ValidationJob | None) -> dict | None:
    if job is None or job.status in ACTIVE_JOB_STATUSES:
        return None
    result = await db.get(QaResult, job.id)
    return result.raw if result else None


def actionable_sql() -> ColumnElement:
    """SQL twin of the job part of `derive_actions`: the latest job (joined as
    `ValidationJob`) finished with needs_info, a mapped error code or a mapped evidence
    blocker. Codes are constants, so building the jsonpath from them is safe."""
    codes = sorted(CODE_ACTIONS)
    path = "$.report.run.evidence[*].blocker ? (" + " || ".join(f'@ == "{c}"' for c in codes) + ")"
    blocked = (
        select(QaResult.job_id)
        .where(
            QaResult.job_id == ValidationJob.id,
            func.jsonb_path_exists(QaResult.raw, cast(path, JSONPATH)),
        )
        .correlate(ValidationJob)
        .exists()
    )
    return ValidationJob.status.not_in(ACTIVE_JOB_STATUSES) & or_(
        ValidationJob.status == "needs_info",
        ValidationJob.error_code.in_(codes),
        ValidationJob.backend_error_code.in_(codes),
        blocked,
    )
