"""Application config via pydantic-settings; env-driven, no hardcoded secrets."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DLW_",
        extra="ignore",
    )

    db_host: str = Field(default="localhost")
    db_port: int = Field(default=5433)
    db_user: str = Field(default="postgres")
    db_password: str = Field(default="")
    db_name: str = Field(default="dlw")

    # Single shared secret for Week 2 (multi-user OIDC PKCE in Phase 3)
    bearer_token: str = Field(default="dev-token-change-me")

    # HF Hub metadata client (controller-side enumeration)
    hf_endpoint: str = Field(default="https://huggingface.co")
    hf_token: str | None = Field(default=None)

    log_level: str = Field(default="INFO")

    # Phase 2 W3a — mTLS + JWT + HMAC
    ca_dir: str = Field(default="./.ca")
    enrollment_token: str = Field(default="")
    controller_hostname: str = Field(default="dlw-controller")
    tls_trusted_proxy: bool = Field(default=False)

    # Phase 2 W3b — HF reverse-proxy
    hf_proxy_timeout_seconds: int = Field(default=300, ge=10, le=3600)

    # Phase 2 W3c — controller leader election
    active_lock_id: int = Field(
        default=0x444C5743_414B5631,
        ge=1,
        le=9_223_372_036_854_775_807,   # PG bigint max (2**63 - 1)
    )  # 'DLWC AKV1'
    leader_poll_interval_seconds: float = Field(default=5.0, ge=0.5, le=60.0)

    @property
    def db_url(self) -> str:
        auth = f"{self.db_user}:{self.db_password}" if self.db_password else self.db_user
        return f"postgresql+asyncpg://{auth}@{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
