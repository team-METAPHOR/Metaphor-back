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


class TestAccountResponse(BaseModel):
    """Status only. The password (and the full login id) is never returned."""

    __test__ = False
    configured: bool
    # False until the engine can log in with id/password: a saved account is not yet applied
    # to runs (only an operator-provisioned session is). See docs/api/onboarding.md.
    in_use: bool = False
    login_id_masked: str | None
    login_path: str | None
    updated_at: datetime | None
