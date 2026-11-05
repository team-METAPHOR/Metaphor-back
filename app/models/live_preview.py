from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, LargeBinary, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.columns import fk


class LivePreview(Base):
    """Relay of a running job's live preview from the worker to API subscribers.

    One row per running job, holding only the latest state: the newest text messages of the
    current engine stream (bounded) and the newest masked JPEG frame in the engine wire format.
    UNLOGGED: a crash or failover loses it, which is fine for a preview. The worker deletes the
    row when the job ends (and sweeps stale rows); never store frames anywhere else.
    """

    __tablename__ = "live_previews"
    __table_args__ = {"prefixes": ["UNLOGGED"]}

    job_id: Mapped[str] = fk("validation_jobs.id", ondelete="CASCADE", primary_key=True)
    # False: the worker runs this job with QA_LIVE_ENABLED off (no stream at all).
    enabled: Mapped[bool] = mapped_column(Boolean)
    # 1-based engine stream of this job (a condition job has one per condition); 0 = none yet.
    stream: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    condition_id: Mapped[str | None] = mapped_column(String(64))
    # Bumped on every content change; subscribers skip rows they have already sent.
    version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    # live-1 metadata dicts (job_id rewritten to ours), oldest first.
    messages: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # [4-byte BE header length][JSON header][JPEG], already masked by the engine.
    frame: Mapped[bytes | None] = mapped_column(LargeBinary)
    frame_sequence: Mapped[int | None] = mapped_column(Integer)
    # Heartbeat: the worker relay touches it even when nothing changed.
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
