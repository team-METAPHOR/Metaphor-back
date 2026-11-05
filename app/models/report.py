from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.columns import ID_LENGTH, created_at, fk, id_pk, one_of, updated_at

REPORT_SOURCES = ("platform", "widget")
ERROR_TYPES = ("manual", "js_error", "unhandled_rejection", "http_5xx")
ORIGIN_KINDS = ("approved", "mapped")
# received: 신규, fixing: 버그 수정 중, dismissed: 버그 아님/중복 (manual terminal),
# closed: 마감 — set only by the worker when a retest passes.
WORKFLOW_STATUSES = ("received", "fixing", "dismissed", "closed")
MANUAL_WORKFLOW_STATUSES = ("received", "fixing", "dismissed")


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        one_of("source", REPORT_SOURCES, "source"),
        one_of("error_type", ERROR_TYPES, "error_type"),
        one_of("origin_kind", ORIGIN_KINDS, "origin_kind"),
        one_of("workflow_status", WORKFLOW_STATUSES, "workflow_status"),
        CheckConstraint("source <> 'platform' OR author_id IS NOT NULL", name="platform_author"),
        Index("ix_reports_program_fingerprint", "program_id", "fingerprint", "created_at"),
        Index("ix_reports_program_ip", "program_id", "client_ip_hash", "created_at"),
        Index("ix_reports_org_created", "org_id", "created_at"),
        UniqueConstraint("org_id", "number", name="uq_reports_org_number"),
    )

    id: Mapped[str] = id_pk()
    program_id: Mapped[str] = fk("programs.id", index=False)
    org_id: Mapped[str] = fk("organizations.id", index=False)
    # Human number per org (BUG-<number>), from organizations.report_seq. Gap-free except
    # where a report creation rolled back after taking its number.
    number: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20), server_default="platform")
    # NULL for anonymous widget reports.
    author_id: Mapped[str | None] = fk("users.id", nullable=True)
    public_receipt_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Entry URL handed to the engine (already mapped to the approved origin).
    target_url: Mapped[str] = mapped_column(String(2048))
    # Text fields are stored redacted.
    title: Mapped[str] = mapped_column(String(200), server_default="")
    description: Mapped[str] = mapped_column(Text, server_default="")
    expected_result: Mapped[str] = mapped_column(Text, server_default="")
    test_data: Mapped[str] = mapped_column(Text, server_default="")
    # Plain ids (no FK) to avoid report↔artifact/job reference cycles.
    input_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    latest_job_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    workflow_status: Mapped[str] = mapped_column(String(20), server_default="received")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Kept (and still shown) when the member is later deactivated; no cascade.
    assignee_id: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    error_type: Mapped[str] = mapped_column(String(30), server_default="manual")
    origin_kind: Mapped[str] = mapped_column(String(20), server_default="approved")
    # Whitelisted SDK environment/error metadata, redacted.
    client_context: Mapped[dict | None] = mapped_column(JSONB)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    # HMAC of the client IP; the raw IP is never stored.
    client_ip_hash: Mapped[str | None] = mapped_column(String(64))
    duplicate_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Set by the retention purge once the free text and client metadata are blanked; the row
    # stays for statistics (status, verdicts, timestamps, counts).
    scrubbed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Clarification(Base):
    __tablename__ = "clarifications"

    id: Mapped[str] = id_pk()
    report_id: Mapped[str] = fk("reports.id", ondelete="CASCADE")
    # The needs_info job being answered; one answer per source job.
    job_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("validation_jobs.id"), unique=True
    )
    author_id: Mapped[str] = fk("users.id")
    answers: Mapped[str] = mapped_column(Text)
    created_job_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("validation_jobs.id"), unique=True
    )
    created_at: Mapped[datetime] = created_at()
