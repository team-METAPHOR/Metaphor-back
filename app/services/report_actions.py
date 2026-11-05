"""What a person must do before a report can move on (`required_actions`), and a plain-text
summary of the latest engine attempt (`best_attempt`).

Only codes the engine (core_engine 426b3da) or our worker actually emit are mapped; anything
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
    "CHECK_TEST_ACCOUNT": (
        "등록된 테스트 계정으로 로그인하지 못했습니다. 계정과 로그인 설정을 확인해 주세요"
    ),
    "CHECK_TARGET_URL": "테스트 대상 URL과 허용 범위를 확인해 주세요",
    "USE_HTTPS_FOR_CREDENTIALS": (
        "테스트 계정이나 세션은 HTTPS 대상에서만 사용할 수 있습니다. "
        "프로그램 원점을 유효한 인증서가 있는 HTTPS 주소로 다시 등록해 주세요"
    ),
    "CHECK_TEST_RESET": (
        "테스트 데이터 서버 초기화를 하지 못해 쓰기 검증을 멈췄습니다. "
        "초기화 어댑터와 테스트 계정 설정을 확인해 주세요"
    ),
    "REVIEW_WRITE_APPROVAL": (
        "재현에 승인되지 않은 쓰기 요청이 필요했습니다. "
        "필요한 경로만 관리자 쓰기 승인에 추가할지 검토해 주세요"
    ),
    "ANSWER_QUESTIONS": "추가 질문에 답하면 다시 검증합니다",
    "WAIT_FOR_PROVIDER": "AI 사용량 제한이 풀린 뒤 다시 검증을 요청해 주세요",
    "RETRY_PROVIDER_LATER": "AI 응답이 일시적으로 실패했습니다. 잠시 후 다시 요청해 주세요",
    "OPERATOR_CHECK_PROVIDER": "AI 공급자 설정 문제로 중단됐습니다. 운영자 조치가 필요합니다",
}

CODE_ACTIONS = {
    # Worker (app/worker/runner.py): the program's test session secret is missing or malformed.
    "SESSION_SECRET_MISSING": "TEST_ACCOUNT_REQUIRED",
    "SESSION_SECRET_INVALID": "TEST_ACCOUNT_REQUIRED",
    # Worker (app/worker/test_accounts.py): the saved account cannot be opened (sealed for
    # another key/program, or the worker key is missing) — save it again / operator key fix.
    "TEST_ACCOUNT_INVALID": "TEST_ACCOUNT_REQUIRED",
    "TEST_ACCOUNT_KEY_MISSING": "TEST_ACCOUNT_REQUIRED",
    "TEST_ACCOUNT_KEY_INVALID": "TEST_ACCOUNT_REQUIRED",
    # Engine form login (0.4.0): wrong account, form mismatch or no success signal. Job
    # error_code on the first exploration, run.evidence[].blocker on a reproduction attempt.
    "LOGIN_FAILED": "CHECK_TEST_ACCOUNT",
    # Engine 0.4.1 / worker pre-run check: a session or test account on a non-HTTPS origin.
    "CREDENTIALS_REQUIRE_HTTPS": "USE_HTTPS_FOR_CREDENTIALS",
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
    # Engine 0.6.0 approved writes: the reset before an attempt failed (no write sent), or the
    # worker refused the reset setup before the engine ran (app/worker/runner.py, resets.py).
    "RESET_FAILED": "CHECK_TEST_RESET",
    "STATE_RESET_REQUIRED": "CHECK_TEST_RESET",
    "STATE_CHANGES_SESSION_CONFLICT": "CHECK_TEST_RESET",
    "RESET_ACCOUNT_MISSING": "CHECK_TEST_RESET",
    "RESET_ACCOUNT_NOT_APPROVED": "CHECK_TEST_RESET",
    "RESET_TOKEN_MISSING": "CHECK_TEST_RESET",
    "RESET_TARGET_NOT_APPROVED": "CHECK_TEST_RESET",
    # A write the run needed was not approved (read-only run, or outside the approval).
    "METHOD_NOT_ALLOWED": "REVIEW_WRITE_APPROVAL",
    "STATE_CHANGE_OUT_OF_SCOPE": "REVIEW_WRITE_APPROVAL",
    # Engine model providers (openai/groq): 429 → wait for Retry-After (job.retry_available_at);
    # timeout/unreachable/other HTTP status → transient; 401/403 key or oversized response →
    # the operator, since rerunning would fail the same way. Provider bodies are never stored.
    "MODEL_RATE_LIMITED": "WAIT_FOR_PROVIDER",
    "MODEL_TIMEOUT": "RETRY_PROVIDER_LATER",
    "MODEL_UNAVAILABLE": "RETRY_PROVIDER_LATER",
    "MODEL_HTTP_ERROR": "RETRY_PROVIDER_LATER",
    "MODEL_AUTH_FAILED": "OPERATOR_CHECK_PROVIDER",
    "MODEL_RESPONSE_TOO_LARGE": "OPERATOR_CHECK_PROVIDER",
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
