from datetime import datetime

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.columns import created_at, fk, updated_at

EVAL_ID_LENGTH = 64


class ReportEvalTag(Base):
    """Evaluation slot (eval_run_id, case_id, repeat_id) of a report filed for an AI evaluation.

    Kept out of `reports` on purpose: the worker loads `reports.*` as engine input, so these ids
    (and anything an evaluator might encode in them) can never reach the model, the engine, the
    target site or report text. Only the eval-tag API and the evaluation export read this table.
    One report per slot, so a re-filed report can't be counted twice for the same repeat."""

    __tablename__ = "report_eval_tags"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "eval_run_id", "case_id", "repeat_id", name="uq_report_eval_tags_slot"
        ),
    )

    report_id: Mapped[str] = fk("reports.id", ondelete="CASCADE", primary_key=True)
    org_id: Mapped[str] = fk("organizations.id", index=False)
    eval_run_id: Mapped[str] = mapped_column(String(EVAL_ID_LENGTH))
    case_id: Mapped[str] = mapped_column(String(EVAL_ID_LENGTH))
    repeat_id: Mapped[str] = mapped_column(String(EVAL_ID_LENGTH))
    tagged_by: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL", index=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
