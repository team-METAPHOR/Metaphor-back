from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.columns import created_at, fk, id_pk, one_of

ARTIFACT_KINDS = ("input_image", "report_md", "screenshot", "json")


class Artifact(Base):
    """Private stored file. Downloads go through this row, never through raw paths."""

    __tablename__ = "artifacts"
    __table_args__ = (one_of("kind", ARTIFACT_KINDS, "kind"),)

    id: Mapped[str] = id_pk()
    org_id: Mapped[str] = fk("organizations.id")
    report_id: Mapped[str] = fk("reports.id", ondelete="CASCADE")
    job_id: Mapped[str | None] = fk("validation_jobs.id", nullable=True, ondelete="CASCADE")
    kind: Mapped[str] = mapped_column(String(20))
    # Path relative to STORAGE_ROOT, built only from server-generated ids.
    storage_key: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = created_at()
