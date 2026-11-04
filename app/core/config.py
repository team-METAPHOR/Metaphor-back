from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
