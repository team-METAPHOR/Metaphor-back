from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.columns import created_at, fk


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_org_created", "org_id", "created_at"),
        Index("ix_audit_events_action_target", "action", "target", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    actor_id: Mapped[str | None] = fk("users.id", nullable=True, index=False)
    org_id: Mapped[str | None] = fk("organizations.id", nullable=True, index=False)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()


class IdempotencyRecord(Base):
    """Stored first response per (user, scope, key). user_id is NULL for widget requests."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "scope",
            "key",
            name="uq_idempotency_records_user_scope_key",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[str | None] = fk("users.id", nullable=True, index=False, ondelete="CASCADE")
    scope: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = created_at()
