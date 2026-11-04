from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent


async def record_audit(
    session: AsyncSession,
    *,
    actor_id: str | None,
    org_id: str | None,
    action: str,
    target: str | None = None,
    detail: dict | None = None,
) -> None:
    """Append an audit row in the caller's transaction. Never put secrets or raw PII in detail."""
    session.add(
        AuditEvent(actor_id=actor_id, org_id=org_id, action=action, target=target, detail=detail)
    )
    await session.flush()


def count_actions(actor_id: str, actions: tuple[str, ...], since: datetime) -> Select:
    return (
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.actor_id == actor_id,
            AuditEvent.action.in_(actions),
            AuditEvent.created_at >= since,
        )
    )
