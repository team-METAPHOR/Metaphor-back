from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
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
from app.models.columns import ID_LENGTH, created_at, fk, id_pk, one_of

JOB_KINDS = ("initial", "fresh", "resume", "retest")
JOB_STATUSES = (
    "queued",
    "running",
    "cancelling",
    "needs_info",
    "completed",
    "inconclusive",
    "failed",
    "cancelled",
)
ACTIVE_JOB_STATUSES = ("queued", "running", "cancelling")
VERDICTS = ("reproduced", "not_reproduced", "inconclusive")
ENGINE_COMMIT = "093535d"

_active = ", ".join(f"'{s}'" for s in ACTIVE_JOB_STATUSES)


class ValidationJob(Base):
    """One engine execution. The table doubles as the durable work queue."""

    __tablename__ = "validation_jobs"
    __table_args__ = (
        one_of("kind", JOB_KINDS, "kind"),
        one_of("status", JOB_STATUSES, "status"),
        one_of("verdict", VERDICTS, "verdict"),
        Index(
            "uq_validation_jobs_active_report",
            "report_id",
            unique=True,
            postgresql_where=text(f"status IN ({_active})"),
        ),
        Index("ix_validation_jobs_queue", "status", "created_at"),
        Index("ix_validation_jobs_report_created", "report_id", "created_at"),
        Index("ix_validation_jobs_org_created", "org_id", "created_at"),
    )

    id: Mapped[str] = id_pk()
    report_id: Mapped[str] = fk("reports.id", index=False)
    org_id: Mapped[str] = fk("organizations.id", index=False)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), server_default="queued")
    stage: Mapped[str | None] = mapped_column(String(30))
    parent_job_id: Mapped[str | None] = fk("validation_jobs.id", nullable=True, index=False)
    baseline_job_id: Mapped[str | None] = fk("validation_jobs.id", nullable=True, index=False)
    # Engine's own 32-hex id; internal only.
    engine_job_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), unique=True)
    scope_revision: Mapped[int] = mapped_column(Integer)
    # RunPolicy/BrowserOptions inputs frozen at creation; secret refs only.
    scope_snapshot: Mapped[dict] = mapped_column(JSONB)
    answers: Mapped[str | None] = mapped_column(Text)
    app_version: Mapped[str | None] = mapped_column(String(100))
    verdict: Mapped[str | None] = mapped_column(String(20))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    classification: Mapped[str | None] = mapped_column(String(30))
    regression: Mapped[str | None] = mapped_column(String(30))
    error_code: Mapped[str | None] = mapped_column(String(64))
    backend_error_code: Mapped[str | None] = mapped_column(String(64))
    questions: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    analysis_available: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    human_review_required: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    engine_commit: Mapped[str] = mapped_column(String(40), server_default=ENGINE_COMMIT)
    created_at: Mapped[datetime] = created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def has_conditions(self) -> bool:
        return bool((self.scope_snapshot or {}).get("conditions"))


CONDITION_RUN_STATUSES = (*JOB_STATUSES, "not_run")


class ValidationJobCondition(Base):
    """One per-condition engine child run of a condition-matrix job.

    Inserted when the engine creates the child (engine id known, condition id filled from
    the engine's condition rows), updated from the final result. `not_run` rows have no engine
    id. The parent's own (matrix) engine id stays in `validation_jobs.engine_job_id`."""

    __tablename__ = "validation_job_conditions"
    __table_args__ = (
        UniqueConstraint("job_id", "ordinal"),
        one_of("status", CONDITION_RUN_STATUSES, "status"),
        one_of("verdict", VERDICTS, "verdict"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("validation_jobs.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    condition_id: Mapped[str | None] = mapped_column(String(64))
    engine_job_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), unique=True)
    status: Mapped[str] = mapped_column(String(20), server_default="running")
    verdict: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at()


class QaResult(Base):
    """Raw engine result (`dataclasses.asdict(QAResult)`), kept verbatim."""

    __tablename__ = "qa_results"

    job_id: Mapped[str] = fk("validation_jobs.id", ondelete="CASCADE", primary_key=True)
    # Engine schema: "2" (single run) or "conditions-1" (condition matrix). Text since 0018.
    schema_version: Mapped[str] = mapped_column(String(20), server_default="1")
    raw: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (UniqueConstraint("job_id", "engine_source", "engine_sequence"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("validation_jobs.id", ondelete="CASCADE")
    )
    # Per-engine-job sequence; never mix across jobs.
    engine_sequence: Mapped[int | None] = mapped_column(Integer)
    # Which engine dir the event came from: "" = the job's own engine job, else the child
    # (per-condition) engine id. Internal only; never serialized.
    engine_source: Mapped[str] = mapped_column(String(ID_LENGTH), server_default="")
    kind: Mapped[str] = mapped_column(String(30))
    detail: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at()


class ModelUsage(Base):
    __tablename__ = "model_usage"
    __table_args__ = (Index("ix_model_usage_org_created", "org_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_id: Mapped[str] = fk("validation_jobs.id", ondelete="CASCADE")
    org_id: Mapped[str] = fk("organizations.id", index=False)
    phase: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(100))
    # NULL = not reported by the provider; never treat as zero cost.
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at()
