from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["draft", "active", "paused", "closed"]

# Engine validates content (RunPolicy / BrowserOptions); these only bound input size.
SCOPE_FIELDS = (
    "base_url",
    "allowed_path_prefixes",
    "excluded_path_prefixes",
    "width",
    "height",
    "app_version",
    "approved_app_versions",
    "test_context_id",
    "mask_selectors",
    "reset_adapter",
)

# Same limits as the engine's BrowserOptions (core_engine/observation.py): at most 30
# selectors, each 1..256 chars. Anything wider would make every worker job fail.
MAX_MASK_SELECTORS = 30
MaskSelector = Annotated[str, Field(min_length=1, max_length=256)]
# Automatic scenario regenerations per report after an inconclusive result.
AutoFreshRetries = Annotated[int, Field(ge=0, le=2)]


class _Strict(BaseModel):
    # Unknown fields are rejected, not ignored. Operator-only revision fields
    # (session_secret_ref, allow_private_targets, private_cidrs) are deliberately absent:
    # a customer admin naming a worker secret could point another org's credentials at
    # their own base_url.
    model_config = ConfigDict(extra="forbid")


class ProgramCreate(_Strict):
    org_id: str = Field(max_length=32)
    name: str = Field(min_length=1, max_length=200)
    status: Status = "draft"
    external_ai_allowed: bool = False
    auto_fresh_retries: AutoFreshRetries = 1
    base_url: str = Field(max_length=2048)
    allowed_path_prefixes: list[str] = Field(max_length=50)
    excluded_path_prefixes: list[str] = Field(default=[], max_length=50)
    width: int = 1280
    height: int = 800
    app_version: str = "unspecified"
    approved_app_versions: list[str] = Field(default=[], max_length=50)  # checked per item
    test_context_id: str = "anonymous"
    mask_selectors: list[MaskSelector] = Field(default=[], max_length=MAX_MASK_SELECTORS)
    reset_adapter: str = Field(default="stateless", max_length=64)


class ProgramPatch(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Status | None = None
    external_ai_allowed: bool | None = None
    auto_fresh_retries: AutoFreshRetries | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    allowed_path_prefixes: list[str] | None = Field(default=None, max_length=50)
    excluded_path_prefixes: list[str] | None = Field(default=None, max_length=50)
    width: int | None = None
    height: int | None = None
    app_version: str | None = None
    approved_app_versions: list[str] | None = Field(default=None, max_length=50)
    test_context_id: str | None = None
    mask_selectors: list[MaskSelector] | None = Field(default=None, max_length=MAX_MASK_SELECTORS)
    reset_adapter: str | None = Field(default=None, max_length=64)


class ScopeResponse(BaseModel):
    revision: int
    base_url: str
    allowed_path_prefixes: list[str]
    excluded_path_prefixes: list[str]
    width: int
    height: int
    app_version: str
    approved_app_versions: list[str]
    test_context_id: str
    mask_selectors: list[str]
    has_session: bool
    reset_adapter: str
    created_at: datetime


class ProgramResponse(BaseModel):
    """Full view for members of the owning org."""

    id: str
    org_id: str
    name: str
    status: str
    external_ai_allowed: bool
    daily_job_limit: int
    retention_days: int
    auto_fresh_retries: int
    current_revision: int
    scope: ScopeResponse
    created_at: datetime
    updated_at: datetime


class ProgramSummary(BaseModel):
    id: str
    name: str
    status: str
    base_url: str
    role: str  # the caller's role in the owning org


class ProgramList(BaseModel):
    items: list[ProgramSummary]


class OriginVerificationResponse(BaseModel):
    origin: str
    token: str  # public: published by the customer at file_url
    file_url: str
    verified_at: datetime | None
    last_checked_at: datetime | None
