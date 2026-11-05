from datetime import datetime

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Report


def count_since(program_id: str, since: datetime, *, ip_hash: str | None = None) -> Select:
    query = (
        select(func.count())
        .select_from(Report)
        .where(
            Report.program_id == program_id, Report.source == "widget", Report.created_at >= since
        )
    )
    return query.where(Report.client_ip_hash == ip_hash) if ip_hash else query


async def find_recent_fingerprint(
    db: AsyncSession, program_id: str, fingerprint: str, since: datetime
) -> Report | None:
    return await db.scalar(
        select(Report)
        .where(
            Report.program_id == program_id,
            Report.fingerprint == fingerprint,
            Report.created_at >= since,
        )
        .order_by(Report.created_at)
        .limit(1)
    )


async def bump_duplicate(db: AsyncSession, report_id: str) -> bool:
    """Count one more duplicate. Returns whether the report is already scrubbed, read from the
    row version this UPDATE locked (so a concurrent retention scrub is either seen or waits)."""
    scrubbed_at = await db.scalar(
        update(Report)
        .where(Report.id == report_id)
        .values(duplicate_count=Report.duplicate_count + 1)
        .returning(Report.scrubbed_at)
    )
    return scrubbed_at is not None
