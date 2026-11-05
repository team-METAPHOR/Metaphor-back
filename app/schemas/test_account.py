from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr

# A path on the program's origin: no scheme/host, query, fragment, backslash or whitespace.
LOGIN_PATH_PATTERN = r"^/(?:[^/?#\\\s][^?#\\\s]*)?$"


class TestAccountPut(BaseModel):
    __test__ = False  # not a pytest class
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    login_id: SecretStr = Field(min_length=1, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=256)
    login_path: str | None = Field(default=None, max_length=2048, pattern=LOGIN_PATH_PATTERN)
    submit_path: str | None = Field(default=None, max_length=2048, pattern=LOGIN_PATH_PATTERN)
    success_path: str | None = Field(default=None, max_length=2048, pattern=LOGIN_PATH_PATTERN)
    success_selector: str | None = Field(default=None, min_length=1, max_length=256)


class TestAccountResponse(BaseModel):
    """Status only. The password (and the full login id) is never returned."""

    __test__ = False
    configured: bool
    # Runs sign in with it: login path + success signal saved, and the current scope has a
    # non-anonymous test_context_id and no operator session (models.test_account.login_applies).
    in_use: bool = False
    login_id_masked: str | None
    login_path: str | None
    submit_path: str | None = None
    success_path: str | None = None
    success_selector: str | None = None
    updated_at: datetime | None
