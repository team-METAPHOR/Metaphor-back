from datetime import datetime
from typing import Annotated, Literal

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
    activity: bool = False  # required to send `breadcrumbs`


# --- breadcrumbs: the reporter's recent actions -----------------------------
# Whitelisted per type; unknown fields (e.g. an input `value`) are rejected. The server
# sanitizes again before storing (app/services/breadcrumbs.py).

MAX_BREADCRUMBS = 50
BREADCRUMB_WINDOW_MS = 600_000


def _text(limit: int):
    return Field(default=None, max_length=limit)


class _Crumb(_Payload):
    # Milliseconds relative to submit time (0 = the moment of submitting).
    t: int = Field(ge=-BREADCRUMB_WINDOW_MS, le=0)


class NavigationCrumb(_Crumb):
    type: Literal["navigation"]
    path: str | None = _text(512)


class ClickCrumb(_Crumb):
    type: Literal["click"]
    tag: str | None = _text(32)
    role: str | None = _text(32)
    text: str | None = _text(80)
    testid: str | None = _text(80)
    selector: str | None = _text(200)


class InputCrumb(_Crumb):
    # Never the value: only what kind of field changed and how long the value is.
    type: Literal["input"]
    tag: str | None = _text(32)
    input_type: str | None = _text(32)
    label: str | None = _text(80)
    testid: str | None = _text(80)
    selector: str | None = _text(200)
    checked: bool | None = None
    selected_text: str | None = _text(80)
    length: int | None = Field(default=None, ge=0, le=10_000)


class SubmitCrumb(_Crumb):
    type: Literal["submit"]
    label: str | None = _text(80)
    testid: str | None = _text(80)
    selector: str | None = _text(200)


class KeyCrumb(_Crumb):
    type: Literal["key"]
    key: (
        Literal[
            "Enter",
            "Escape",
            "Tab",
            "ArrowUp",
            "ArrowDown",
            "ArrowLeft",
            "ArrowRight",
            "Backspace",
        ]
        | None
    ) = None


class RequestCrumb(_Crumb):
    # Method, path, status and timing only: no headers, cookies or bodies exist here.
    type: Literal["request"]
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"] | None = None
    path: str | None = _text(512)
    status: int | None = Field(default=None, ge=100, le=599)
    # `status` is omitted when the request failed or was aborted (no HTTP response).
    duration_ms: int | None = Field(default=None, ge=0, le=BREADCRUMB_WINDOW_MS)


class ErrorCrumb(_Crumb):
    type: Literal["error"]
    message: str | None = _text(200)


Breadcrumb = Annotated[
    NavigationCrumb | ClickCrumb | InputCrumb | SubmitCrumb | KeyCrumb | RequestCrumb | ErrorCrumb,
    Field(discriminator="type"),
]


class WidgetReportPayload(_Payload):
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=8000)
    page_url: str = Field(max_length=2048)
    error_type: Literal["manual", "js_error", "unhandled_rejection", "http_5xx"] = "manual"
    error: ErrorInfo | None = None
    environment: Environment | None = None
    mask_boxes: list[list[int]] | None = Field(default=None, max_length=20)
    consent: Consent = Consent()
    breadcrumbs: list[Breadcrumb] | None = Field(default=None, max_length=MAX_BREADCRUMBS)


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
    auto_detect: AutoDetect
    limits: WidgetLimits
    mask_selectors: list[str]


class WidgetReceipt(BaseModel):
    receipt_id: str | None
    status: Literal["received", "duplicate"]
    created_at: datetime | None = None
