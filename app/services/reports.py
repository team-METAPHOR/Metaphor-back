"""Report creation shared by the platform API and the Web SDK widget API."""

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.pagination import decode_cursor, encode_cursor
from app.core.text import sanitize_text
from app.integrations import storage
from app.models import Artifact, ProgramScopeRevision, Report, ValidationJob
from app.repositories import programs as programs_repo
from app.repositories import reports as reports_repo
from app.schemas.report import ReportCreated, ReportDetail, ReportItem, ReportList
from app.schemas.validation_job import JobBrief
from app.services.access import can_submit, org_role
from app.services.audit import record_audit
from app.services.idempotency import run_once
from app.services.intake import (
    ReportSubmission,
    check_target_url,
    parse_mask_boxes,
    prepare_screenshot,
)
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


# --- platform API ----------------------------------------------------------


@dataclass(frozen=True)
class PlatformReportForm:
    program_id: str
    target_url: str
    title: str = ""
    description: str = ""
    expected_result: str = ""
    test_data: str = ""
    mask_boxes: str | None = None


async def submit_report(
    db: AsyncSession,
    user_id: str,
    form: PlatformReportForm,
    image: bytes | None,
    idempotency_key: str | None,
) -> tuple[int, dict]:
    """POST /reports. Returns (status, body); a replayed Idempotency-Key returns the first body."""
    boxes = parse_mask_boxes(form.mask_boxes)
    if boxes and image is None:
        raise ApiError(422, "MASK_BOX_INVALID", "마스킹 영역은 스크린샷과 함께 보내야 합니다.")
    screenshot = prepare_screenshot(image, boxes) if image is not None else None
    request_hash = hashlib.sha256(
        json.dumps(
            {**asdict(form), "image": screenshot.sha256 if screenshot else None}, sort_keys=True
        ).encode()
    ).hexdigest()

    async with db.begin():
        program = await programs_repo.get(db, form.program_id)
        if program is None:
            raise ApiError(404, "NOT_FOUND", "프로그램을 찾을 수 없습니다.")
        if program.status != "active":
            raise ApiError(422, "PROGRAM_NOT_ACTIVE", "현재 제보를 받지 않는 프로그램입니다.")
        if not await can_submit(db, user_id, program):
            raise ApiError(403, "SUBMISSION_FORBIDDEN", "이 프로그램에 제보할 권한이 없습니다.")

        async def create() -> tuple[int, dict]:
            report, job = await create_report(
                db,
                ReportSubmission(
                    program=program,
                    target_url=form.target_url,
                    source="platform",
                    author_id=user_id,
                    title=form.title,
                    description=form.description,
                    expected_result=form.expected_result,
                    test_data=form.test_data,
                    screenshot=screenshot,
                ),
            )
            await db.refresh(report, ["created_at"])
            body = ReportCreated(
                id=report.id,
                program_id=program.id,
                validation_job_id=job.id,
                status=job.status,
                created_at=report.created_at,
            )
            return 201, body.model_dump(mode="json")

        return await run_once(
            db,
            user_id=user_id,
            scope="reports:create",
            key=idempotency_key,
            request_hash=request_hash,
            fn=create,
        )


async def report_access(db: AsyncSession, user_id: str, report: Report) -> str | None:
    """'org' for active org members, 'author' for the platform reporter, else None."""
    if await org_role(db, user_id, report.org_id):
        return "org"
    if report.author_id is not None and report.author_id == user_id:
        return "author"
    return None


async def load_visible_report(db: AsyncSession, user_id: str, report_id: str):
    report = await reports_repo.get(db, report_id)
    access = await report_access(db, user_id, report) if report else None
    if access is None:
        raise ApiError(404, "NOT_FOUND", "제보를 찾을 수 없습니다.")
    return report, access


def _brief(job: ValidationJob | None) -> JobBrief | None:
    if job is None:
        return None
    return JobBrief(id=job.id, kind=job.kind, status=job.status, verdict=job.verdict)


def _item(report: Report, job: ValidationJob | None) -> dict:
    return {
        "id": report.id,
        "program_id": report.program_id,
        "title": report.title,
        "source": report.source,
        "error_type": report.error_type,
        "origin_kind": report.origin_kind,
        "duplicate_count": report.duplicate_count,
        "workflow_status": report.workflow_status,
        "latest_job": _brief(job),
        "created_at": report.created_at,
    }


async def list_reports(
    db: AsyncSession,
    user_id: str,
    *,
    limit: int,
    cursor: str | None,
    source: str | None,
    program_id: str | None,
) -> ReportList:
    position = decode_cursor(cursor) if cursor else None
    async with db.begin():
        rows = await reports_repo.list_visible(
            db, user_id, limit=limit, cursor=position, source=source, program_id=program_id
        )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = encode_cursor(last.created_at, last.id)
    return ReportList(items=[ReportItem(**_item(r, j)) for r, j in page], next_cursor=next_cursor)


async def get_report(db: AsyncSession, user_id: str, report_id: str) -> ReportDetail:
    async with db.begin():
        report, access = await load_visible_report(db, user_id, report_id)
        job = await db.get(ValidationJob, report.latest_job_id) if report.latest_job_id else None
    return ReportDetail(
        **_item(report, job),
        target_url=report.target_url,
        description=report.description,
        expected_result=report.expected_result,
        test_data=report.test_data,
        has_screenshot=report.input_artifact_id is not None,
        input_artifact_id=report.input_artifact_id,
        client_context=report.client_context if access == "org" else None,
    )
