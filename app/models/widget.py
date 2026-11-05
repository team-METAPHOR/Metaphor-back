from datetime import datetime

from sqlalchemy import Boolean, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.columns import created_at, fk, updated_at
from app.models.program import text_array


class ProgramWidgetSettings(Base):
    """Web SDK settings. The project key is a public identifier, not a credential."""

    __tablename__ = "program_widget_settings"

    program_id: Mapped[str] = fk("programs.id", ondelete="CASCADE", primary_key=True)
    public_project_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Exact origins (scheme://host[:port]) allowed for CORS and report intake.
    allowed_origins: Mapped[list[str]] = text_array()
    widget_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    public_reporting_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    auto_detect_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Screenshots from origins other than the approved base_url (live sites).
    live_screenshot_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Recent user actions (clicks, navigations, ...) sent by the SDK with a report.
    breadcrumbs_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
