"""Live preview relay rows (written by the worker, read by WebSocket subscribers)."""

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LivePreview, ValidationJob

CHANNEL = "live_preview"


async def snapshot(db: AsyncSession, job_id: str, known_version: int | None):
    """Job status/lease plus the job's preview row. Messages and frame only when the row's
    version differs from `known_version` (a re-created row restarts at 1)."""
    changed = LivePreview.version.is_distinct_from(known_version)
    query = (
        select(
            ValidationJob.status,
            ValidationJob.started_at,
            (ValidationJob.lease_expires_at < func.now()).label("lease_expired"),
            func.now().label("now"),
            LivePreview.enabled,
            LivePreview.stream,
            LivePreview.condition_id,
            LivePreview.version,
            LivePreview.updated_at,
            LivePreview.frame_sequence,
            case((changed, LivePreview.messages), else_=None).label("messages"),
            case((changed, LivePreview.frame), else_=None).label("frame"),
        )
        .outerjoin(LivePreview, LivePreview.job_id == ValidationJob.id)
        .where(ValidationJob.id == job_id)
    )
    return (await db.execute(query)).one_or_none()


async def delete_for_job(db: AsyncSession, job_id: str) -> None:
    """Drop the preview and wake subscribers (NOTIFY is sent at commit)."""
    await db.execute(delete(LivePreview).where(LivePreview.job_id == job_id))
    await db.execute(select(func.pg_notify(CHANNEL, job_id)))
