"""HfS3StreamDownloader — Phase 1 W4 streaming pipeline.

Replaces MockDownloader. Streams bytes HF→S3 with O(5MB) memory and zero
disk landing. sha256 is computed on the same byte stream that gets uploaded
to S3 (single source of truth).

Public surface (kept compatible with runner.py wiring):
  - Assignment       — slim payload from runner
  - DownloadResult   — return shape (now includes s3_key)
  - HfS3StreamDownloader.download(assignment) -> DownloadResult
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import boto3
import httpx
from botocore.config import Config

from dlw.executor.config import ExecutorSettings
from dlw.schemas.storage import StorageConfig

logger = logging.getLogger(__name__)

_HTTP_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class Assignment:
    """Slim payload passed from runner to downloader."""
    subtask_id: uuid.UUID
    task_id: uuid.UUID
    repo_id: str
    revision: str
    filename: str
    file_size: int | None
    expected_sha256: str | None
    storage_config: StorageConfig


@dataclass(frozen=True)
class DownloadResult:
    bytes_written: int
    actual_sha256: str
    s3_key: str


class HfS3StreamDownloader:
    """HF GET stream → S3 multipart upload, sha256 tee'd on the same bytes."""

    def __init__(self, *, settings: ExecutorSettings) -> None:
        self._s = settings

    def _compose_key(self, a: Assignment) -> str:
        prefix = a.storage_config.key_prefix.strip("/")
        parts = [p for p in (prefix, a.repo_id, a.revision, a.filename) if p]
        return "/".join(parts)

    def _make_s3_client(self, cfg: StorageConfig) -> Any:
        addressing = "path" if self._s.s3_path_style else "virtual"
        boto_cfg = Config(
            region_name=cfg.region,
            s3={"addressing_style": addressing},
        )
        return boto3.client(
            "s3",
            region_name=cfg.region,
            endpoint_url=cfg.endpoint_url or self._s.s3_endpoint_url,
            config=boto_cfg,
        )

    async def download(self, *, assignment: Assignment) -> DownloadResult:
        """Pipeline: see Task 10/11 for the full body."""
        raise NotImplementedError("Task 10 wires the streaming body")
