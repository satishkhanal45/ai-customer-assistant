"""
Tika configuration -- data layer only.

Follows the existing project convention (see README's INGESTION_ prefixed
settings) of a pydantic-settings model with an env prefix.
"""

from __future__ import annotations

from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class TikaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TIKA_")

    base_url: AnyHttpUrl = "http://tika:9998"  # type: ignore[assignment]
    endpoint: str = "/rmeta/text"
    request_timeout_seconds: float = 60.0
    max_file_size_bytes: int = 50 * 1024 * 1024  # 50 MB, per operational notes

    @property
    def extract_url(self) -> str:
        return f"{str(self.base_url).rstrip('/')}{self.endpoint}"
