from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_ALLOWED_ORIGINS = 20


class WidgetSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None means "not sent"; an explicit null is rejected by the service.
    widget_enabled: bool | None = None
    public_reporting_enabled: bool | None = None
    auto_detect_enabled: bool | None = None
    live_screenshot_enabled: bool | None = None
    allowed_origins: list[str] | None = Field(default=None, max_length=MAX_ALLOWED_ORIGINS)


class AllowedOrigin(BaseModel):
    origin: str
    kind: Literal["approved", "mapped"]


class WidgetSettingsResponse(BaseModel):
    program_id: str
    public_project_key: str | None
    allowed_origins: list[AllowedOrigin]
    widget_enabled: bool
    public_reporting_enabled: bool
    auto_detect_enabled: bool
    live_screenshot_enabled: bool
    updated_at: datetime | None


class ProjectKeyResponse(BaseModel):
    public_project_key: str
