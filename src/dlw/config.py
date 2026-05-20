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

    # UI-SP5 — SSE tick rate for /tasks/{id}/stream (clamped at runtime).
    task_stream_interval_seconds: float = Field(default=1.0)

    # UI-SP5b — SSE tick rate for /executors/stream (clamped at runtime).
    executors_stream_interval_seconds: float = Field(default=5.0)

    # Phase 2 W3c — controller leader election
    active_lock_id: int = Field(
        default=0x444C5743_414B5631,
        ge=1,
        le=9_223_372_036_854_775_807,   # PG bigint max (2**63 - 1)
    )  # 'DLWC AKV1'
    leader_poll_interval_seconds: float = Field(default=5.0, ge=0.5, le=60.0)

    # Phase 3 SP1 — multi-tenancy
    auth_dev_mode: bool = Field(default=False)
    system_jwt_secret: str = Field(default="dev-system-jwt-change-me")
    system_admin_token: str = Field(default="")
    oidc_issuer: str = Field(default="")
    oidc_client_id: str = Field(default="")
    oidc_client_secret: str = Field(default="")
    oidc_redirect_url: str = Field(
        default="http://localhost:8000/api/v1/auth/callback"
    )
    auth_tenant_rules_json: str = Field(default="[]")

    # Phase 3 SP2 — multi-source
    sources_yaml_path: str = Field(default="config/sources.yaml")
    resolver_rules_path: str = Field(default="config/resolver-rules.yaml")
    probe_size_mb: int = Field(default=32, ge=1, le=256)
    probe_timeout_s: float = Field(default=8.0, ge=1.0, le=60.0)
    probe_history_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    combo_overhead_per_source_pct: float = Field(default=2.0, ge=0.0, le=50.0)
    chunk_level_min_file_mb: int = Field(default=100, ge=1)
    speed_ewma_alpha: float = Field(default=0.3, ge=0.0, le=1.0)
    blacklist_5xx_count: int = Field(default=3, ge=1)
    blacklist_minutes: int = Field(default=5, ge=1)
    blacklist_max_minutes: int = Field(default=30, ge=1)
    sha_mismatch_blacklist_hours: int = Field(default=24, ge=1)
    rebalance_interval_seconds: float = Field(default=60.0, ge=5.0, le=600.0)
    degradation_trigger_threshold: float = Field(default=0.3, ge=0.0, le=1.0)

    # Phase 3 SP3 — incremental / dedup GC
    gc_interval_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)
    gc_grace_seconds: int = Field(default=3600, ge=0)

    @property
    def db_url(self) -> str:
        auth = f"{self.db_user}:{self.db_password}" if self.db_password else self.db_user
        return f"postgresql+asyncpg://{auth}@{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
