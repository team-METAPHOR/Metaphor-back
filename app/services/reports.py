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
from app.models import Artifact, ProgramScopeRevision, Report, User, ValidationJob
from app.models.validation_job import ACTIVE_JOB_STATUSES
from app.repositories import programs as programs_repo
from app.repositories import reports as reports_repo
from app.schemas.report import Assignee, ReportCreated, ReportDetail, ReportItem, ReportList
from app.schemas.validation_job import JobBrief
from app.services.access import org_role
from app.services.audit import record_audit
from app.services.idempotency import run_once
from app.services.intake import (
    ReportSubmission,
    check_target_url,
    parse_mask_boxes,
    prepare_screenshot,
)
from app.services.jobs import create_job
from app.services.limits import enforce_rate_limit, lock_key, since

# Platform reports per user per hour (widget reports have their own limits).
REPORT_HOURLY_LIMIT = 20
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
        # Any active member of the owning org may file; everyone else gets 404 (cross-org rule).
        if program is None or await org_role(db, user_id, program.owner_org_id) is None:
            raise ApiError(404, "NOT_FOUND", "프로그램을 찾을 수 없습니다.")
        if program.status != "active":
            raise ApiError(422, "PROGRAM_NOT_ACTIVE", "현재 제보를 받지 않는 프로그램입니다.")

        async def create() -> tuple[int, dict]:
            await lock_key(db, f"reports:{user_id}")
            await enforce_rate_limit(
                db,
                reports_repo.count_by_author(user_id, since(hours=1)),
                limit=REPORT_HOURLY_LIMIT,
                code="REPORT_RATE_LIMITED",
            )
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


async def load_visible_report(db: AsyncSession, user_id: str, report_id: str) -> Report:
    """The report, for active members of its org only; 404 for everyone else."""
    report = await reports_repo.get(db, report_id)
    if report is None or await org_role(db, user_id, report.org_id) is None:
        raise ApiError(404, "NOT_FOUND", "제보를 찾을 수 없습니다.")
    return report


def _brief(job: ValidationJob | None) -> JobBrief | None:
    if job is None:
        return None
    return JobBrief(id=job.id, kind=job.kind, status=job.status, verdict=job.verdict)


def report_progress(workflow_status: str, job: JobBrief | ValidationJob | None) -> str:
    """Dashboard stage; the mapping table is documented on `ReportItem`."""
    if workflow_status in ("closed", "dismissed", "fixing"):
        return workflow_status
    if job is None:
        return "new"
    if job.status in ACTIVE_JOB_STATUSES:
        return "reproducing"
    return "reproduced" if job.verdict == "reproduced" else "triaged"


def _assignee(user: User | None) -> Assignee | None:
    if user is None:
        return None
    return Assignee(id=user.id, display_name=user.display_name, email=user.email)


def _item(report: Report, job: ValidationJob | None, assignee: User | None) -> dict:
    return {
        "id": report.id,
        "program_id": report.program_id,
        "title": report.title,
        "source": report.source,
        "error_type": report.error_type,
        "origin_kind": report.origin_kind,
        "duplicate_count": report.duplicate_count,
        "workflow_status": report.workflow_status,
        "progress": report_progress(report.workflow_status, job),
        "closed_at": report.closed_at,
        "assignee": _assignee(assignee),
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
    assignee: str | None = None,
) -> ReportList:
    """`assignee` is a user id, or "me" for the caller."""
    position = decode_cursor(cursor) if cursor else None
    async with db.begin():
        rows = await reports_repo.list_visible(
            db,
            user_id,
            limit=limit,
            cursor=position,
            source=source,
            program_id=program_id,
            assignee_id=user_id if assignee == "me" else assignee,
        )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = encode_cursor(last.created_at, last.id)
    items = [ReportItem(**_item(r, j, u)) for r, j, u in page]
    return ReportList(items=items, next_cursor=next_cursor)


async def get_report(db: AsyncSession, user_id: str, report_id: str) -> ReportDetail:
    async with db.begin():
        report = await load_visible_report(db, user_id, report_id)
        return await _detail(db, report)


async def _detail(db: AsyncSession, report: Report) -> ReportDetail:
    job = await db.get(ValidationJob, report.latest_job_id) if report.latest_job_id else None
    assignee = await db.get(User, report.assignee_id) if report.assignee_id else None
    return ReportDetail(
        **_item(report, job, assignee),
        target_url=report.target_url,
        description=report.description,
        expected_result=report.expected_result,
        test_data=report.test_data,
        has_screenshot=report.input_artifact_id is not None,
        input_artifact_id=report.input_artifact_id,
        client_context=report.client_context,
        scrubbed=report.scrubbed_at is not None,
    )


WORKFLOW_ROLES = ("reviewer", "admin")


async def _require_triage_role(db: AsyncSession, user_id: str, report: Report) -> None:
    if await org_role(db, user_id, report.org_id) not in WORKFLOW_ROLES:
        raise ApiError(403, "FORBIDDEN", "제보 상태를 바꿀 권한이 없습니다.")


async def set_workflow_status(
    db: AsyncSession, user_id: str, report_id: str, status: str
) -> ReportDetail:
    """Manual review status (received / fixing / dismissed). `closed` is system-only (passing
    retest) and final: a closed report can no longer be changed here."""
    async with db.begin():
        report = await load_visible_report(db, user_id, report_id)
        await _require_triage_role(db, user_id, report)
        # Row lock so a concurrent auto-close is not overwritten.
        await db.refresh(report, with_for_update=True)
        if report.workflow_status == "closed":
            raise ApiError(409, "REPORT_CLOSED", "재검증으로 마감된 제보는 변경할 수 없습니다.")
        previous = report.workflow_status
        if previous != status:
            report.workflow_status = status
            await db.flush()
            await record_audit(
                db,
                actor_id=user_id,
                org_id=report.org_id,
                action="report.workflow",
                target=report.id,
                detail={"from": previous, "to": status},
            )
        await db.refresh(report)
        return await _detail(db, report)


async def _assignable(db: AsyncSession, user_id: str, org_id: str) -> bool:
    """Active user with an active membership in the active org."""
    user = await db.get(User, user_id)
    return user is not None and user.is_active and await org_role(db, user_id, org_id) is not None


async def set_assignee(
    db: AsyncSession, user_id: str, report_id: str, assignee_id: str | None
) -> ReportDetail:
    """Assign (or with None, unassign) a report to an active member of its org."""
    async with db.begin():
        report = await load_visible_report(db, user_id, report_id)
        await _require_triage_role(db, user_id, report)
        # One message whether the user is unknown or just not a member (no account probing).
        if assignee_id is not None and not await _assignable(db, assignee_id, report.org_id):
            raise ApiError(
                422, "ASSIGNEE_NOT_MEMBER", "이 조직의 활성 멤버만 담당자가 될 수 있습니다."
            )
        previous = report.assignee_id
        if previous != assignee_id:
            report.assignee_id = assignee_id
            await db.flush()
            await record_audit(
                db,
                actor_id=user_id,
                org_id=report.org_id,
                action="report.assign",
                target=report.id,
                detail={"from": previous, "to": assignee_id},
            )
        await db.refresh(report)
        return await _detail(db, report)
