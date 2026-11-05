from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Artifact, ReportDuplicate


async def count_for_report(db: AsyncSession, report_id: str) -> int:
    query = select(func.count()).where(ReportDuplicate.report_id == report_id)
    return await db.scalar(query)


async def add(db: AsyncSession, duplicate: ReportDuplicate) -> None:
    db.add(duplicate)
    await db.flush()


async def list_for_report(
    db: AsyncSession,
    report_id: str,
    *,
    limit: int,
    now: datetime,
    cursor: tuple[datetime, str] | None = None,
) -> list[tuple[ReportDuplicate, str | None]]:
    """Newest first, up to `limit + 1` rows, each with its screenshot artifact id when that
    artifact still exists and has not expired."""
    query = (
        select(ReportDuplicate, Artifact.id)
        .outerjoin(
            Artifact,
            and_(Artifact.id == ReportDuplicate.input_artifact_id, Artifact.expires_at > now),
        )
        .where(ReportDuplicate.report_id == report_id)
        .order_by(ReportDuplicate.received_at.desc(), ReportDuplicate.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        moment, last_id = cursor
        query = query.where(
            or_(
                ReportDuplicate.received_at < moment,
                and_(ReportDuplicate.received_at == moment, ReportDuplicate.id < last_id),
            )
        )
    return [tuple(row) for row in (await db.execute(query)).all()]


async def latest_excerpts(db: AsyncSession, report_ids: list[str], length: int) -> dict[str, str]:
    """First `length` characters of each report's newest duplicate, in one query."""
    if not report_ids:
        return {}
    query = (
        select(ReportDuplicate.report_id, func.left(ReportDuplicate.description, length))
        .where(ReportDuplicate.report_id.in_(report_ids))
        .distinct(ReportDuplicate.report_id)
        .order_by(
            ReportDuplicate.report_id,
            ReportDuplicate.received_at.desc(),
            ReportDuplicate.id.desc(),
        )
    )
    return {report_id: text for report_id, text in (await db.execute(query)).all()}
