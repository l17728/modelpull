"""Executor configuration via pydantic-settings — env_prefix DLW_EXECUTOR_."""
from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DLW_EXECUTOR_",
        env_file=".env.executor",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Identity
    id: str = Field(min_length=1, max_length=64)
    host_id: str = Field(default="", max_length=64)

    # Connection to controller
    controller_url: str = Field(default="http://localhost:8000")
    bearer_token: str = Field(min_length=1)

    # Loop pacing
    heartbeat_interval_seconds: int = Field(default=10, ge=1, le=300)
    poll_interval_seconds: int = Field(default=2, ge=1, le=60)

    # Mock downloader
    download_dir: str = Field(default="./downloads")

    # Capabilities advertised at /join
    nic_speed_gbps: int = Field(default=1, ge=1, le=400)
    region: str = Field(default="local")

    # Phase 1 W4 — HF Hub
    hf_endpoint: str = Field(default="https://huggingface.co")
    hf_token: str | None = Field(default=None)

    # Phase 1 W4 — S3 / S3-compatible
    s3_region: str = Field(default="us-east-1")
    s3_endpoint_url: str | None = Field(default=None)
    s3_path_style: bool = Field(default=True)

    # Phase 1 W4 — pipeline tuning
    multipart_part_size_bytes: int = Field(default=5 * 1024 * 1024, ge=5 * 1024 * 1024)
    download_timeout_seconds: int = Field(default=300, ge=10, le=3600)

    @model_validator(mode="after")
    def _derive_host_id(self) -> "ExecutorSettings":
        """If host_id not set, derive from id by stripping any -worker-N suffix.

        Reflects invariant 9 convention: id = `host-X-worker-N`, host_id = `host-X`.
        """
        if not self.host_id:
            parts = self.id.rsplit("-worker-", 1)
            self.host_id = parts[0] if len(parts) == 2 else self.id
        return self
