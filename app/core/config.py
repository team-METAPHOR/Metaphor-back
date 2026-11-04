from functools import lru_cache
from pathlib import Path

from pydantic import Field
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
    lease_seconds: int = 30
    # Engine QA limit (180s) plus cleanup headroom.
    worker_hard_deadline_seconds: int = 210


@lru_cache
def get_settings() -> Settings:
    return Settings()
