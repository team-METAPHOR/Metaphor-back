from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bounds only; content is validated with the engine's TestCondition/BrowserOptions.
ConditionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Browser = Literal["chromium", "firefox", "webkit"]
Delay = Annotated[int, Field(ge=0, le=2000)]


class _Strict(BaseModel):
    # session_secret_ref is operator-only and deliberately absent (see schemas/program.py).
    model_config = ConfigDict(extra="forbid")


class TestConditionCreate(_Strict):
    condition_id: ConditionId
    label: str = Field(min_length=1, max_length=200)
    browser_name: Browser = "chromium"
    width: int = 1280
    height: int = 800
    response_delay_ms: Delay = 0
    test_context_id: str = Field(default="anonymous", max_length=100)
    record_video: bool = False
    reset_adapter: str = Field(default="stateless", max_length=64)


class TestConditionPatch(_Strict):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    browser_name: Browser | None = None
    width: int | None = None
    height: int | None = None
    response_delay_ms: Delay | None = None
    test_context_id: str | None = Field(default=None, max_length=100)
    record_video: bool | None = None
    reset_adapter: str | None = Field(default=None, max_length=64)


class TestConditionOut(BaseModel):
    condition_id: str
    label: str
    browser_name: str
    width: int
    height: int
    response_delay_ms: int
    test_context_id: str
    record_video: bool
    reset_adapter: str
    has_session: bool
    created_at: datetime
    updated_at: datetime


class TestConditionList(BaseModel):
    items: list[TestConditionOut]
