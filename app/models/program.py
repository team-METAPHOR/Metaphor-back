from datetime import datetime

from sqlalchemy import ARRAY, Boolean, CheckConstraint, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.columns import created_at, fk, id_pk, one_of, updated_at

PROGRAM_STATUSES = ("draft", "active", "paused", "closed")


def text_array() -> Mapped[list[str]]:
    return mapped_column(ARRAY(Text), server_default=text("'{}'"), nullable=False)


class Program(Base):
    """A customer web app under test. `id` is also the engine `program_id`."""

    __tablename__ = "programs"
    __table_args__ = (
        one_of("status", PROGRAM_STATUSES, "status"),
        CheckConstraint("auto_fresh_retries BETWEEN 0 AND 2", name="auto_fresh_retries"),
    )

    id: Mapped[str] = id_pk()
    owner_org_id: Mapped[str] = fk("organizations.id")
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), server_default="draft")
    current_revision: Mapped[int] = mapped_column(Integer, server_default="1")
    external_ai_allowed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    daily_job_limit: Mapped[int] = mapped_column(Integer, server_default="50")
    retention_days: Mapped[int] = mapped_column(Integer, server_default="30")
    # Automatic `fresh` jobs (scenario regenerated) allowed per report after an inconclusive run.
    auto_fresh_retries: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class ProgramScopeRevision(Base):
    """Immutable approved scope + run settings. A scope change inserts a new revision."""

    __tablename__ = "program_scope_revisions"

    program_id: Mapped[str] = fk("programs.id", ondelete="CASCADE", primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_url: Mapped[str] = mapped_column(String(2048))
    allowed_path_prefixes: Mapped[list[str]] = text_array()
    excluded_path_prefixes: Mapped[list[str]] = text_array()
    width: Mapped[int] = mapped_column(Integer, server_default="1280")
    height: Mapped[int] = mapped_column(Integer, server_default="800")
    app_version: Mapped[str] = mapped_column(String(100), server_default="unspecified")
    approved_app_versions: Mapped[list[str]] = text_array()
    test_context_id: Mapped[str] = mapped_column(String(100), server_default="anonymous")
    mask_selectors: Mapped[list[str]] = text_array()
    # Reference into the worker secret store; never the secret itself.
    session_secret_ref: Mapped[str | None] = mapped_column(String(100))
    reset_adapter: Mapped[str] = mapped_column(String(64))
    # Operator-only (not settable through the customer API).
    allow_private_targets: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    private_cidrs: Mapped[list[str]] = text_array()
    created_by: Mapped[str | None] = fk("users.id", nullable=True, index=False)
    created_at: Mapped[datetime] = created_at()
