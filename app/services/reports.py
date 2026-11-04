"""Report creation shared by the platform API and the Web SDK widget API."""

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.text import sanitize_text
from app.integrations import storage
from app.models import Artifact, ProgramScopeRevision, Report, ValidationJob
from app.services.audit import record_audit
from app.services.intake import ReportSubmission, check_target_url
from app.services.jobs import create_job

TEXT_LIMITS = {"title": 200, "description": 8000, "expected_result": 2000, "test_data": 2000}


async def create_report(
    session: AsyncSession, sub: ReportSubmission
) -> tuple[Report, ValidationJob]:
    """Store a report, its masked input image and its initial queued job.

    Runs inside the caller's transaction (wrap with `run_once` for Idempotency-Key). The image
    file is written first and removed again if anything after it fails.
    """
    program = sub.program
    if program.status != "active":
        raise ApiError(422, "PROGRAM_NOT_ACTIVE", "현재 제보를 받지 않는 프로그램입니다.")
    if not program.external_ai_allowed:
        raise ApiError(
            422, "EXTERNAL_AI_NOT_ALLOWED", "이 프로그램은 AI 분석이 허용되지 않았습니다."
        )
    texts = {key: sanitize_text(getattr(sub, key), limit) for key, limit in TEXT_LIMITS.items()}
    if sub.screenshot is None and not texts["description"].strip():
        raise ApiError(
            422, "SCREENSHOT_OR_DESCRIPTION_REQUIRED", "스크린샷 또는 설명 중 하나는 필요합니다."
        )
    revision = await session.get(ProgramScopeRevision, (program.id, program.current_revision))
    target_url, _ = check_target_url(revision, sub.target_url)

    report = Report(
        id=new_id(),
        program_id=program.id,
        org_id=program.owner_org_id,
        source=sub.source,
        author_id=sub.author_id,
        public_receipt_id="rcpt_" + secrets.token_urlsafe(16) if sub.source == "widget" else None,
        target_url=target_url,
        error_type=sub.error_type,
        origin_kind=sub.origin_kind,
        client_context=sub.client_context,
        fingerprint=sub.fingerprint,
        client_ip_hash=sub.client_ip_hash,
        **texts,
    )
    written = None
    try:
        session.add(report)
        await session.flush()
        if sub.screenshot is not None:
            shot = sub.screenshot
            written = storage.safe_path(
                program.owner_org_id, report.id, "input", f"{shot.sha256}.png"
            )
            storage.write_private(written, shot.data)
            artifact = Artifact(
                id=new_id(),
                org_id=program.owner_org_id,
                report_id=report.id,
                kind="input_image",
                storage_key=storage.relative_key(written),
                sha256=shot.sha256,
                size=len(shot.data),
                visibility="reporter" if sub.source == "platform" else "org",
                expires_at=datetime.now(UTC) + timedelta(days=program.retention_days),
            )
            session.add(artifact)
            report.input_artifact_id = artifact.id
            await session.flush()
        job = await create_job(session, report, "initial")
        await record_audit(
            session,
            actor_id=sub.author_id,
            org_id=program.owner_org_id,
            action="report.create",
            target=report.id,
            detail={"source": sub.source},
        )
    except BaseException:
        # ponytail: a failure *after* the caller's commit cannot be undone here; such orphans are
        # removed by the retention purge (files without an artifact row).
        if written is not None:
            written.unlink(missing_ok=True)
        raise
    return report, job
