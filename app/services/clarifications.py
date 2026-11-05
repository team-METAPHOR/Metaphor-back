"""Answer a needs_info job: store the answer and queue a resume job linked to it."""

import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.text import sanitize_text
from app.models import Clarification, Program, ValidationJob
from app.services.access import WRITE_ROLES, org_role
from app.services.audit import record_audit
from app.services.idempotency import run_once
from app.services.jobs import create_job, enforce_job_request_limit
from app.services.reports import load_visible_report

# Must match core_engine QAService.resume, which appends this to the stored description.
ANSWER_SEPARATOR = "\n추가 답변:\n"
ENGINE_DESCRIPTION_LIMIT = 8000


async def answer_question(
    db: AsyncSession,
    user_id: str,
    report_id: str,
    source_job_id: str,
    answers: str,
    idempotency_key: str | None,
) -> tuple[int, dict]:
    if not answers.strip():
        raise ApiError(422, "ANSWER_REQUIRED", "답변을 입력해 주세요.")
    clean = sanitize_text(answers, ENGINE_DESCRIPTION_LIMIT)
    request_hash = hashlib.sha256(f"{source_job_id}\0{answers}".encode()).hexdigest()

    async with db.begin():
        report = await load_visible_report(db, user_id, report_id)
        if await org_role(db, user_id, report.org_id) not in WRITE_ROLES:
            raise ApiError(403, "FORBIDDEN", "추가 답변 권한이 없습니다.")

        async def create() -> tuple[int, dict]:
            await enforce_job_request_limit(db, user_id)
            source = await db.get(ValidationJob, source_job_id)
            if source is None or source.report_id != report.id:
                raise ApiError(404, "NOT_FOUND", "검증 작업을 찾을 수 없습니다.")
            answered = await db.scalar(
                select(Clarification.id).where(Clarification.job_id == source.id)
            )
            if answered:
                raise ApiError(409, "ALREADY_ANSWERED", "이미 답변한 질문입니다.")
            if source.status != "needs_info" or report.latest_job_id != source.id:
                raise ApiError(409, "NOT_AWAITING_ANSWER", "답변을 기다리는 검증 작업이 아닙니다.")
            # The engine resumes from the stored (redacted) description plus every earlier answer.
            previous = (
                await db.scalars(
                    select(Clarification.answers)
                    .where(Clarification.report_id == report.id)
                    .order_by(Clarification.created_at)
                )
            ).all()
            total = len(report.description) + sum(
                len(ANSWER_SEPARATOR) + len(text) for text in [*previous, clean]
            )
            if total > ENGINE_DESCRIPTION_LIMIT:
                raise ApiError(
                    422, "ANSWER_TOO_LONG", "설명과 누적 답변이 8000자를 넘을 수 없습니다."
                )
            program = await db.get(Program, report.program_id)
            if program.status != "active":
                raise ApiError(
                    422, "PROGRAM_NOT_ACTIVE", "현재 검증을 실행할 수 없는 프로그램입니다."
                )
            job = await create_job(db, report, "resume", parent=source, answers=clean)
            try:
                async with db.begin_nested():
                    db.add(
                        Clarification(
                            id=new_id(),
                            report_id=report.id,
                            job_id=source.id,
                            author_id=user_id,
                            answers=clean,
                            created_job_id=job.id,
                        )
                    )
                    await db.flush()
            except IntegrityError:
                raise ApiError(409, "ALREADY_ANSWERED", "이미 답변한 질문입니다.") from None
            await record_audit(
                db,
                actor_id=user_id,
                org_id=report.org_id,
                action="report.clarify",
                target=report.id,
                detail={"source_job_id": source.id, "job_id": job.id},
            )
            return 202, {"validation_job_id": job.id, "status": job.status}

        return await run_once(
            db,
            user_id=user_id,
            scope=f"clarifications:{report.id}",
            key=idempotency_key,
            request_hash=request_hash,
            fn=create,
        )
