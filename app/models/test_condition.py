from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.columns import created_at, fk, one_of, updated_at

BROWSER_NAMES = ("chromium", "firefox", "webkit")
# Engine limits (core_engine ConditionsService / ConditionLimits at 4a710f0).
MAX_REGISTERED_CONDITIONS = 16
MAX_RUN_CONDITIONS = 8
DEFAULT_RUN_CONDITIONS = 4


class ProgramTestCondition(Base):
    """One registered run condition (account/browser/viewport/delay) of a program.

    Editable registry: a job copies the selected rows into its immutable scope snapshot, so
    later edits never change past runs or their retests."""

    __tablename__ = "program_test_conditions"
    __table_args__ = (
        one_of("browser_name", BROWSER_NAMES, "browser_name"),
        CheckConstraint("response_delay_ms BETWEEN 0 AND 2000", name="response_delay_ms"),
    )

    program_id: Mapped[str] = fk("programs.id", ondelete="CASCADE", primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    browser_name: Mapped[str] = mapped_column(String(20), server_default="chromium")
    width: Mapped[int] = mapped_column(Integer, server_default="1280")
    height: Mapped[int] = mapped_column(Integer, server_default="800")
    response_delay_ms: Mapped[int] = mapped_column(Integer, server_default="0")
    test_context_id: Mapped[str] = mapped_column(String(100), server_default="anonymous")
    record_video: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    reset_adapter: Mapped[str] = mapped_column(String(64), server_default="stateless")
    # Operator-only reference into the worker secret store (QA_SESSION_<ORG>_<REF>).
    session_secret_ref: Mapped[str | None] = mapped_column(String(100))
    created_by: Mapped[str | None] = fk("users.id", nullable=True, index=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
