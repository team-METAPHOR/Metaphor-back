"""Widget duplicates: each merged submission's own text, context and masked screenshot."""

from datetime import UTC, datetime, timedelta

from core_engine import Screenshot
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.core.pagination import decode_cursor, encode_cursor
from app.core.text import sanitize_text
from app.integrations import storage
from app.models import Program, Report, ReportDuplicate
from app.repositories import report_duplicates as repo
from app.repositories import widget as widget_repo
from app.schemas.report import ReportDuplicateItem, ReportDuplicateList
from app.services.reports import TEXT_LIMITS, add_input_image, load_visible_report

# ponytail: fixed cap per report; past it duplicates are only counted. Make it a setting when
# someone needs more than the first 50 originals.
MAX_STORED_DUPLICATES = 50


async def record_duplicate(
    db: AsyncSession,
    original: Report,
    program: Program,
    *,
    description: str,
    client_context: dict | None,
    screenshot: Screenshot | None,
) -> None:
    """Count a duplicate of `original` and, under the cap, store it. Runs inside the caller's
    transaction, which must serialize per program (the widget's advisory lock) so the cap
    check-then-insert is atomic. `description` is raw; it is redacted here exactly like
    `create_report` does."""
    # The UPDATE row-locks the report until commit; the scrub skips locked reports.
    if await widget_repo.bump_duplicate(db, original.id):
        return
    if await repo.count_for_report(db, original.id) >= MAX_STORED_DUPLICATES:
        return
    duplicate = ReportDuplicate(
        id=new_id(),
        report_id=original.id,
        org_id=original.org_id,
        description=sanitize_text(description, TEXT_LIMITS["description"]),
        client_context=client_context,
    )
    written = None
    try:
        if screenshot is not None:
            written = storage.safe_path(
                original.org_id, original.id, "duplicates", f"{duplicate.id}.png"
            )
            # Same lifetime as the original's own files; the scrub also removes it.
            expires_at = original.created_at + timedelta(days=program.retention_days)
            artifact = add_input_image(db, original, screenshot, written, expires_at=expires_at)
            duplicate.input_artifact_id = artifact.id
        await repo.add(db, duplicate)
    except BaseException:
        # A failure after the caller's commit leaves an orphan file for the retention purge.
        if written is not None:
            written.unlink(missing_ok=True)
        raise


async def list_duplicates(
    db: AsyncSession, user_id: str, report_id: str, *, limit: int, cursor: str | None
) -> ReportDuplicateList:
    position = decode_cursor(cursor) if cursor else None
    async with db.begin():
        report = await load_visible_report(db, user_id, report_id)
        rows = await repo.list_for_report(
            db, report.id, limit=limit, now=datetime.now(UTC), cursor=position
        )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = encode_cursor(last.received_at, last.id)
    items = [
        ReportDuplicateItem(
            id=dup.id,
            received_at=dup.received_at,
            description=dup.description,
            has_screenshot=artifact_id is not None,
            input_artifact_id=artifact_id,
            client_context=dup.client_context,
        )
        for dup, artifact_id in page
    ]
    return ReportDuplicateList(items=items, next_cursor=next_cursor)
