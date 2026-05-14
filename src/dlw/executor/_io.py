"""Shared HTTP + S3 helpers for HfS3StreamDownloader and DirectOffsetDownloader.

Pure utilities — no behavior change. Both downloaders import what they need.
"""
from __future__ import annotations

from typing import Any

import boto3
import httpx
from botocore.config import Config
from tenacity import (
    retry, retry_if_exception, stop_after_attempt, wait_exponential,
)

from dlw.executor.config import ExecutorSettings
from dlw.executor.types import Assignment
from dlw.schemas.storage import StorageConfig

_HTTP_CHUNK_BYTES = 64 * 1024


def _is_transient_http(exc: BaseException) -> bool:
    """5xx HTTP + network/timeout/protocol = transient (retry-worthy)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return isinstance(exc, (
        httpx.NetworkError, httpx.TimeoutException, httpx.ProtocolError,
    ))


_TRANSIENT_RETRY = retry(
    retry=retry_if_exception(_is_transient_http),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.0, max=8.0),
    reraise=True,
)


def make_s3_client(settings: ExecutorSettings, cfg: StorageConfig) -> Any:
    addressing = "path" if settings.s3_path_style else "virtual"
    boto_cfg = Config(
        region_name=cfg.region,
        s3={"addressing_style": addressing},
    )
    return boto3.client(
        "s3",
        region_name=cfg.region,
        endpoint_url=cfg.endpoint_url or settings.s3_endpoint_url,
        config=boto_cfg,
    )


def compose_key(a: Assignment) -> str:
    prefix = a.storage_config.key_prefix.strip("/")
    parts = [p for p in (prefix, a.repo_id, a.revision, a.filename) if p]
    return "/".join(parts)


def upload_part(
    s3: Any, bucket: str, key: str, upload_id: str,
    part_no: int, body: bytes,
) -> str:
    return s3.upload_part(
        Bucket=bucket, Key=key, UploadId=upload_id,
        PartNumber=part_no, Body=body,
    )["ETag"]
