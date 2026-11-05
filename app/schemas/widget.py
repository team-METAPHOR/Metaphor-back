from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    # Install snippet inputs. All null when PUBLIC_API_BASE is unset (or no SDK bundle).
    api_base: str | None
    sdk_url: str | None
    sdk_integrity: str | None


class ProjectKeyResponse(BaseModel):
    public_project_key: str


# --- public widget API -----------------------------------------------------


class _Payload(BaseModel):
    # Whitelist only: no header, cookie or request/response body fields exist, and unknown
    # fields are rejected.
    model_config = ConfigDict(extra="forbid")


class HttpInfo(_Payload):
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    path: str = Field(max_length=2048)
    status: int


class ErrorInfo(_Payload):
    message: str = Field(max_length=1000)
    stack_top: str | None = Field(default=None, max_length=300, pattern=r"^.+:\d+:\d+$")
    http: HttpInfo | None = None


class Viewport(_Payload):
    width: int = Field(ge=1, le=10000)
    height: int = Field(ge=1, le=10000)


class Environment(_Payload):
    user_agent: str = Field(default="", max_length=512)
    language: str = Field(default="", max_length=35)
    timezone: str = Field(default="", max_length=64)
    viewport: Viewport | None = None


class Consent(_Payload):
    environment: bool = False
    screenshot: bool = False
    activity: bool = False


class ActivityNavigation(_Payload):
    kind: Literal["navigation"]
    path: str = Field(max_length=512, min_length=1)
    age_ms: int = Field(strict=True, ge=0, le=120000)


class ActivityClick(_Payload):
    kind: Literal["click"]
    path: str = Field(max_length=512, min_length=1)
    age_ms: int = Field(strict=True, ge=0, le=120000)
    selector: str = Field(max_length=256, min_length=1)
    label: str = Field(default="", max_length=80)
    feedback: list[Annotated[str, Field(max_length=120)]] = Field(
        default_factory=list, max_length=2
    )


class RecentActivity(_Payload):
    version: Literal[1]
    truncated: bool = False
    events: list[Annotated[ActivityNavigation | ActivityClick, Field(discriminator="kind")]] = (
        Field(max_length=20)
    )

    @model_validator(mode="after")
    def bounded(self):
        import json

        if (
            len(json.dumps(self.model_dump(), ensure_ascii=False, separators=(",", ":")).encode())
            > 8192
        ):
            raise ValueError("activity too large")
        if any(a.age_ms < b.age_ms for a, b in zip(self.events, self.events[1:])):
            raise ValueError("activity order invalid")
        return self


class WidgetReportPayload(_Payload):
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=8000)
    page_url: str = Field(max_length=2048)
    error_type: Literal["manual", "js_error", "unhandled_rejection", "http_5xx"] = "manual"
    error: ErrorInfo | None = None
    environment: Environment | None = None
    mask_boxes: list[list[int]] | None = Field(default=None, max_length=20)
    consent: Consent = Consent()
    activity: RecentActivity | None = None


class AutoDetect(BaseModel):
    enabled: bool
    js_error: bool
    unhandled_rejection: bool
    http_5xx: bool


class WidgetLimits(BaseModel):
    max_screenshot_bytes: int
    max_description: int
    max_mask_boxes: int


class WidgetConfig(BaseModel):
    widget_enabled: bool
    reporting_enabled: bool
    screenshot_allowed: bool
    activity_allowed: bool = True
    auto_detect: AutoDetect
    limits: WidgetLimits
    mask_selectors: list[str]


class WidgetReceipt(BaseModel):
    receipt_id: str | None
    status: Literal["received", "duplicate"]
    created_at: datetime | None = None
