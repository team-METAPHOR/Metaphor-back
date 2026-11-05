from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Metaphor API"
    debug: bool = False
    database_url: str = Field(
        default="postgresql+psycopg://metaphor:metaphor@localhost:5432/metaphor",
        repr=False,
    )
    # Private evidence volume. Engine job stores live under {storage_root}/{org}/{report}/.
    storage_root: Path = Path("var/storage")
    # Exact origins of the Metaphor platform frontend (CSRF check for cookie sessions).
    allowed_origins: list[str] = ["http://localhost:3000"]
    # HMAC key (>= 32 bytes) for login emails and client IPs; see app/core/hashing.py.
    # Required unless DEBUG=true (then empty means the public dev-only key).
    ip_hash_secret: str = Field(default="", repr=False)
    # Reverse proxies (IPs or CIDRs) whose X-Forwarded-For is trusted for the client IP.
    trusted_proxies: list[str] = []
    # RSA (>= 3072) public key PEM that seals program test accounts. Not secret: only the
    # worker's TEST_ACCOUNT_PRIVATE_KEY can open them. Unset → PUT test-account returns 503.
    test_account_public_key: str = Field(default="", repr=False)
    # Public base URL of this API as customer sites reach it (e.g. https://api.metaphor.dev),
    # used to build the Web SDK install snippet. Empty → widget-settings returns null URLs.
    public_api_base: str = ""
    lease_seconds: int = 30
    # Engine QA limit (180s) plus cleanup headroom.
    worker_hard_deadline_seconds: int = 210

    @field_validator("public_api_base")
    @classmethod
    def _strip_api_base(cls, base: str) -> str:
        return base.strip().rstrip("/")

    @field_validator("allowed_origins")
    @classmethod
    def _strip_origins(cls, origins: list[str]) -> list[str]:
        # Browsers send Origin as scheme://host[:port] with no path, so "https://x/" never
        # matches; normalize instead of failing every request with CSRF_REJECTED.
        return [o.strip().rstrip("/") for o in origins if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
