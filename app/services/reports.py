"""Report creation shared by the platform API and the Web SDK widget API."""

import hashlib
import json
import math
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

from core_engine import Screenshot
from sqlalchemy import ColumnElement, and_, case, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.pagination import decode_cursor, encode_cursor
from app.core.text import sanitize_text
from app.integrations import storage
from app.models import Artifact, Program, ProgramScopeRevision, Report, User, ValidationJob
from app.models.validation_job import ACTIVE_JOB_STATUSES
from app.repositories import orgs as orgs_repo
from app.repositories import programs as programs_repo
from app.repositories import report_duplicates as duplicates_repo
from app.repositories import reports as reports_repo
from app.schemas.report import (
    Assignee,
    AttentionItem,
    PageCount,
    Progress,
    ReportCreated,
    ReportDetail,
    ReportItem,
    ReportList,
    ReportStats,
    ReportSummary,
)
from app.schemas.validation_job import JobBrief
from app.services.access import WRITE_ROLES, org_role
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
from app.services.report_actions import (
    actionable_sql,
    best_attempt,
    derive_actions,
    load_result,
    origin_verified,
)

# Platform reports per user per hour (widget reports have their own limits).
REPORT_HOURLY_LIMIT = 20
TEXT_LIMITS = {"title": 200, "description": 8000, "expected_result": 2000, "test_data": 2000}
# Characters of the newest duplicate's description shown on list items and detail.
EXCERPT_LENGTH = 120


# create_job refusal that parks a new report instead of rejecting it.
QUOTA_CODES = ("DAILY_LIMIT_EXCEEDED",)


def add_input_image(
    session: AsyncSession, report: Report, shot: Screenshot, path: Path, *, expires_at: datetime
) -> Artifact:
    """Write an already-masked screenshot (0600) and add its `input_image` artifact row. The
    caller removes `path` again if its transaction fails."""
    storage.write_private(path, shot.data)
    artifact = Artifact(
        id=new_id(),
        org_id=report.org_id,
        report_id=report.id,
        kind="input_image",
        storage_key=storage.relative_key(path),
        sha256=shot.sha256,
        size=len(shot.data),
        expires_at=expires_at,
    )
    session.add(artifact)
    return artifact


async def create_report(
    session: AsyncSession, sub: ReportSubmission, *, park_code: str | None = None
) -> tuple[Report, ValidationJob | None]:
    """Store a report, its masked input image and its initial queued job.

    Over quota (create_job's DAILY_LIMIT_EXCEEDED, or the caller's own cap passed as
    `park_code`) the report is still stored but *parked*: no job, `run_blocked = "quota"`,
    audit `report.parked`, and the returned job is None. A later rerun starts it.

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
        number=await orgs_repo.next_report_number(session, program.owner_org_id),
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
            artifact = add_input_image(
                session,
                report,
                shot,
                written,
                expires_at=datetime.now(UTC) + timedelta(days=program.retention_days),
            )
            report.input_artifact_id = artifact.id
            await session.flush()
        job = None
        if park_code is None:
            try:
                job = await create_job(session, report, "initial")
            except ApiError as error:
                if error.code not in QUOTA_CODES:
                    raise
                park_code = error.code
        if job is None:
            report.run_blocked = "quota"
            await session.flush()
            await record_audit(
                session,
                actor_id=sub.author_id,
                org_id=program.owner_org_id,
                action="report.parked",
                target=report.id,
                detail={"reason": "quota", "code": park_code},
            )
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
        # Non-members get 404 (cross-org rule); viewers are members but read only (403).
        role = None if program is None else await org_role(db, user_id, program.owner_org_id)
        if role is None:
            raise ApiError(404, "NOT_FOUND", "프로그램을 찾을 수 없습니다.")
        if role not in WRITE_ROLES:
            raise ApiError(403, "FORBIDDEN", "제보를 등록할 권한이 없습니다.")
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
                validation_job_id=job.id if job else None,
                status=job.status if job else "waiting_quota",
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


def report_progress(
    workflow_status: str,
    job: JobBrief | ValidationJob | None,
    fix_verified: bool = False,
    run_blocked: str | None = None,
) -> str:
    """Dashboard stage; the mapping table is documented on `ReportItem`."""
    if workflow_status in ("closed", "dismissed"):
        return workflow_status
    if fix_verified:
        return "verified"
    if workflow_status == "fixing":
        return workflow_status
    if run_blocked == "quota":
        return "waiting_quota"
    if job is None:
        return "new"
    if job.status in ACTIVE_JOB_STATUSES:
        return "reproducing"
    return "reproduced" if job.verdict == "reproduced" else "triaged"


PROGRESS_VALUES: tuple[str, ...] = get_args(Progress)


def progress_case(
    workflow_status, job_id, job_status, job_verdict, fix_verified, run_blocked
) -> ColumnElement:
    """SQL twin of `report_progress` (keep both in step; a test checks every combination).
    Arguments are SQL expressions for the report's workflow_status, its latest job's id,
    status and verdict (all NULL when there is no job), whether the fix is verified (boolean)
    and the report's run_blocked."""
    return case(
        (workflow_status.in_(("closed", "dismissed")), workflow_status),
        (fix_verified, "verified"),
        (workflow_status == "fixing", workflow_status),
        (run_blocked == "quota", "waiting_quota"),
        (job_id.is_(None), "new"),
        (job_status.in_(ACTIVE_JOB_STATUSES), "reproducing"),
        (job_verdict == "reproduced", "reproduced"),
        else_="triaged",
    )


def report_progress_sql() -> ColumnElement:
    """`progress_case` over `Report` and its latest `ValidationJob` (outer-joined)."""
    return progress_case(
        Report.workflow_status,
        ValidationJob.id,
        ValidationJob.status,
        ValidationJob.verdict,
        reports_repo.fix_verified_sql(),
        Report.run_blocked,
    )


def _assignee(user: User | None, active: bool | None) -> Assignee | None:
    if user is None:
        return None
    if not active:
        return Assignee(id=user.id, display_name=None, email=None, active=False)
    return Assignee(id=user.id, display_name=user.display_name, email=user.email, active=True)


def _item(
    report: Report,
    job: ValidationJob | None,
    assignee: User | None = None,
    assignee_active: bool | None = None,
    *,
    fix_verified: bool,
    duplicate_excerpt: str | None = None,
) -> dict:
    return {
        "id": report.id,
        "program_id": report.program_id,
        "number": report.number,
        "title": report.title,
        "source": report.source,
        "error_type": report.error_type,
        "origin_kind": report.origin_kind,
        "duplicate_count": report.duplicate_count,
        "workflow_status": report.workflow_status,
        "progress": report_progress(report.workflow_status, job, fix_verified, report.run_blocked),
        "fix_verified": fix_verified,
        "closed_at": report.closed_at,
        "assignee": _assignee(assignee, assignee_active),
        "latest_job": _brief(job),
        "created_at": report.created_at,
        "latest_duplicate_excerpt": duplicate_excerpt,
    }


def _utc(moment: datetime | None) -> datetime | None:
    """Naive query datetimes are read as UTC."""
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


async def list_reports(
    db: AsyncSession,
    user_id: str,
    *,
    limit: int,
    cursor: str | None,
    source: str | None,
    program_id: str | None,
    assignee: str | None = None,
    progress: list[str] | None = None,
    include_closed: bool = False,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    q: str | None = None,
    page: int | None = None,
    sort: str = "created_desc",
) -> ReportList:
    """`assignee` is a user id, or "me" for the caller. Closed reports are left out unless
    `include_closed` or `progress` asks for them. With `cursor` the list is cursor-paged
    (no total); otherwise `page` (default 1) is used and `total` is returned."""
    if cursor and page is not None:
        raise ApiError(422, "VALIDATION_FAILED", "cursor와 page는 함께 쓸 수 없습니다.")
    created_from, created_to = _utc(created_from), _utc(created_to)
    if created_from and created_to and created_from > created_to:
        raise ApiError(422, "VALIDATION_FAILED", "from은 to보다 늦을 수 없습니다.")
    position = decode_cursor(cursor) if cursor else None
    where = reports_repo.filters(
        source=source,
        program_id=program_id,
        assignee_id=user_id if assignee == "me" else assignee,
        created_from=created_from,
        created_to=created_to,
        q=q,
    )
    if progress:
        where.append(report_progress_sql().in_(progress))
    elif not include_closed:
        where.append(Report.workflow_status != "closed")
    page_no = None if cursor else (page or 1)
    async with db.begin():
        rows = await reports_repo.list_visible(
            db,
            user_id,
            limit=limit,
            cursor=position,
            offset=(page_no - 1) * limit if page_no else 0,
            where=where,
            ascending=sort == "created_asc",
        )
        total = await reports_repo.count_visible(db, user_id, where) if page_no else None
        page_ids = [row[0].id for row in rows[:limit]]
        verified = await reports_repo.fix_verified_ids(db, page_ids)
        excerpts = await duplicates_repo.latest_excerpts(db, page_ids, EXCERPT_LENGTH)
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page_rows[-1][0]
        next_cursor = encode_cursor(last.created_at, last.id)
    items = [
        ReportItem(
            **_item(
                *row,
                fix_verified=row[0].id in verified,
                duplicate_excerpt=excerpts.get(row[0].id),
            )
        )
        for row in page_rows
    ]
    return ReportList(items=items, next_cursor=next_cursor, total=total, page=page_no)


async def _require_member(db: AsyncSession, user_id: str, org_id: str) -> None:
    if await org_role(db, user_id, org_id) is None:
        raise ApiError(404, "NOT_FOUND", "조직을 찾을 수 없습니다.")


async def _require_program(db: AsyncSession, org_id: str, program_id: str) -> None:
    program = await db.get(Program, program_id)
    if program is None or program.owner_org_id != org_id:
        raise ApiError(404, "NOT_FOUND", "프로그램을 찾을 수 없습니다.")


ATTENTION_LIMIT = 5


def needs_attention_sql() -> ColumnElement:
    """Reproduced, parked over the daily quota, or still `received` with a job-derived
    required action."""
    return or_(
        report_progress_sql().in_(("reproduced", "waiting_quota")),
        and_(Report.workflow_status == "received", actionable_sql()),
    )


async def report_summary(db: AsyncSession, user_id: str, org_id: str) -> ReportSummary:
    async with db.begin():
        await _require_member(db, user_id, org_id)
        counts = await reports_repo.count_by(db, org_id, report_progress_sql())
        rows = await reports_repo.newest_matching(
            db, org_id, needs_attention_sql(), ATTENTION_LIMIT
        )
        verified = await reports_repo.fix_verified_ids(db, [report.id for report, _ in rows])
        items = []
        for report, job in rows:
            actions = derive_actions(
                report.workflow_status,
                job,
                await load_result(db, job),
                run_blocked=report.run_blocked,
            )
            items.append(
                AttentionItem(
                    id=report.id,
                    number=report.number,
                    title=report.title,
                    progress=report_progress(
                        report.workflow_status, job, report.id in verified, report.run_blocked
                    ),
                    reason=actions[0] if actions else None,
                )
            )
    return ReportSummary(
        counts={value: counts.get(value, 0) for value in PROGRESS_VALUES}, needs_attention=items
    )


STATS_MAX_DAYS = 90
TOP_PAGES = 10


async def report_stats(
    db: AsyncSession,
    user_id: str,
    org_id: str,
    days: int,
    *,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
    program_id: str | None = None,
) -> ReportStats:
    """Window: `from` (inclusive) .. `to` (exclusive, default now); without `from` it is the
    last `days` days (clamped 1..90). An explicit window may span at most 90 days."""
    window_from, window_to = _utc(window_from), _utc(window_to)
    until = window_to or datetime.now(UTC)
    if window_from is None:
        days = min(max(days, 1), STATS_MAX_DAYS)
        since = until - timedelta(days=days)
    else:
        since = window_from
        if since >= until:
            raise ApiError(422, "VALIDATION_FAILED", "from은 to보다 앞서야 합니다.")
        if until - since > timedelta(days=STATS_MAX_DAYS):
            raise ApiError(422, "VALIDATION_FAILED", "기간은 최대 90일입니다.")
        days = math.ceil((until - since) / timedelta(days=1))
    async with db.begin():
        await _require_member(db, user_id, org_id)
        if program_id is not None:
            await _require_program(db, org_id, program_id)
        window = (since, until)
        finished, reproduced, avg_seconds = await reports_repo.reproduction_stats(
            db, org_id, *window, program_id
        )
        verified = await reports_repo.count_passed_retests(db, org_id, *window, program_id)
        pages = await reports_repo.top_pages(db, org_id, *window, TOP_PAGES, program_id)
    return ReportStats(
        days=days,
        window_from=since,
        window_to=until,
        auto_reproduction_rate=reproduced / finished if finished else None,
        avg_reproduction_seconds=avg_seconds,
        fix_verified_count=verified,
        by_page=[PageCount(path=path, count=n) for path, n in pages],
    )


async def get_report(db: AsyncSession, user_id: str, report_id: str) -> ReportDetail:
    async with db.begin():
        report = await load_visible_report(db, user_id, report_id)
        return await _detail(db, report)


async def get_report_by_number(
    db: AsyncSession, user_id: str, org_id: str, number: int
) -> ReportDetail:
    """Same as `get_report`, addressed by the org's human number (BUG-<number>)."""
    async with db.begin():
        if await org_role(db, user_id, org_id) is None:
            raise ApiError(404, "NOT_FOUND", "제보를 찾을 수 없습니다.")
        report = await reports_repo.get_by_number(db, org_id, number)
        if report is None:
            raise ApiError(404, "NOT_FOUND", "제보를 찾을 수 없습니다.")
        return await _detail(db, report)


async def _detail(db: AsyncSession, report: Report) -> ReportDetail:
    job = await db.get(ValidationJob, report.latest_job_id) if report.latest_job_id else None
    assignee = await reports_repo.get_assignee(db, report.id) if report.assignee_id else None
    raw = await load_result(db, job)
    open_ = report.workflow_status not in ("closed", "dismissed")
    origin_ok = await origin_verified(db, report.program_id) if open_ else True
    verified = report.id in await reports_repo.fix_verified_ids(db, [report.id])
    excerpts = await duplicates_repo.latest_excerpts(db, [report.id], EXCERPT_LENGTH)
    return ReportDetail(
        **_item(
            report,
            job,
            *(assignee or ()),
            fix_verified=verified,
            duplicate_excerpt=excerpts.get(report.id),
        ),
        target_url=report.target_url,
        description=report.description,
        expected_result=report.expected_result,
        test_data=report.test_data,
        has_screenshot=report.input_artifact_id is not None,
        input_artifact_id=report.input_artifact_id,
        client_context=report.client_context,
        scrubbed=report.scrubbed_at is not None,
        required_actions=derive_actions(
            report.workflow_status,
            job,
            raw,
            origin_verified=origin_ok,
            run_blocked=report.run_blocked,
        ),
        best_attempt=best_attempt(job, raw),
    )


async def _require_triage_role(db: AsyncSession, user_id: str, report: Report) -> None:
    if await org_role(db, user_id, report.org_id) not in WRITE_ROLES:
        raise ApiError(403, "FORBIDDEN", "제보 상태를 바꿀 권한이 없습니다.")


async def set_workflow_status(
    db: AsyncSession, user_id: str, report_id: str, status: str
) -> ReportDetail:
    """Review status. `closed` (마감) needs a verified fix at that moment (latest finished job is
    a passing retest) and is final: a closed report can no longer be changed here."""
    async with db.begin():
        report = await load_visible_report(db, user_id, report_id)
        await _require_triage_role(db, user_id, report)
        # Row lock so two concurrent changes (e.g. a double close) serialize.
        await db.refresh(report, with_for_update=True)
        if report.workflow_status == "closed":
            raise ApiError(409, "REPORT_CLOSED", "마감된 제보는 변경할 수 없습니다.")
        if status == "closed" and report.id not in await reports_repo.fix_verified_ids(
            db, [report.id]
        ):
            raise ApiError(
                409, "FIX_NOT_VERIFIED", "수정 확인(재검증)을 통과한 제보만 마감할 수 있습니다."
            )
        previous = report.workflow_status
        if previous != status:
            report.workflow_status = status
            if status == "closed":
                report.closed_at = datetime.now(UTC)
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
