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
from tenacity import (
    retry, retry_if_exception, stop_after_attempt, wait_exponential,
)

from dlw.executor.config import ExecutorSettings
from dlw.schemas.storage import StorageConfig

logger = logging.getLogger(__name__)

_HTTP_CHUNK_BYTES = 64 * 1024


def _is_transient_http(exc: BaseException) -> bool:
    """5xx HTTP + network/timeout/protocol = transient (retry-worthy).

    4xx errors (404 / 401 / 403) are NOT transient — config / repo issues
    won't fix themselves, so fail fast. ProtocolError/RemoteProtocolError
    covers mid-stream HF drops (W5-C).
    """
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

    def _make_http_client(self) -> httpx.AsyncClient:
        """Test seam — overridden in unit tests via monkeypatch."""
        return httpx.AsyncClient(
            timeout=self._s.download_timeout_seconds,
            follow_redirects=True,
        )

    async def download(self, *, assignment: Assignment) -> DownloadResult:
        """Public entry — retries transient errors (5xx, network, timeout) × 3."""
        @_TRANSIENT_RETRY
        async def _retry_wrapper() -> DownloadResult:
            return await self._download_once(assignment=assignment)
        return await _retry_wrapper()

    async def _download_once(self, *, assignment: Assignment) -> DownloadResult:
        url = (f"{self._s.hf_endpoint.rstrip('/')}/{assignment.repo_id}"
               f"/resolve/{assignment.revision}/{assignment.filename}")
        s3 = self._make_s3_client(assignment.storage_config)
        bucket = assignment.storage_config.bucket
        key = self._compose_key(assignment)
        part_size = self._s.multipart_part_size_bytes

        headers: dict[str, str] = {}
        if self._s.hf_token:
            headers["Authorization"] = f"Bearer {self._s.hf_token}"

        upload_id: str | None = None
        sha = hashlib.sha256()
        bytes_total = 0
        parts: list[dict[str, Any]] = []
        buf = bytearray()
        part_no = 1

        try:
            async with self._make_http_client() as hc:
                async with hc.stream("GET", url, headers=headers) as resp:
                    resp.raise_for_status()

                    upload_id = await asyncio.to_thread(
                        lambda: s3.create_multipart_upload(
                            Bucket=bucket, Key=key
                        )["UploadId"]
                    )

                    async for chunk in resp.aiter_bytes(chunk_size=_HTTP_CHUNK_BYTES):
                        sha.update(chunk)
                        bytes_total += len(chunk)
                        buf.extend(chunk)
                        while len(buf) >= part_size:
                            body = bytes(buf[:part_size])
                            del buf[:part_size]
                            etag = await asyncio.to_thread(
                                self._upload_part,
                                s3, bucket, key, upload_id, part_no, body,
                            )
                            parts.append({"PartNumber": part_no, "ETag": etag})
                            part_no += 1

                    # last (possibly < part_size; allowed for last only)
                    if buf:
                        etag = await asyncio.to_thread(
                            self._upload_part,
                            s3, bucket, key, upload_id, part_no, bytes(buf),
                        )
                        parts.append({"PartNumber": part_no, "ETag": etag})

            # W5-D: 0-byte file → empty parts list would error S3 MalformedXML.
            # Abort the (unused) multipart and use put_object instead.
            if not parts:
                if upload_id is not None:
                    await asyncio.to_thread(lambda: s3.abort_multipart_upload(
                        Bucket=bucket, Key=key, UploadId=upload_id,
                    ))
                await asyncio.to_thread(lambda: s3.put_object(
                    Bucket=bucket, Key=key, Body=b"",
                ))
                return DownloadResult(
                    bytes_written=bytes_total,
                    actual_sha256=sha.hexdigest(),
                    s3_key=key,
                )

            await asyncio.to_thread(
                lambda: s3.complete_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            )
            return DownloadResult(
                bytes_written=bytes_total,
                actual_sha256=sha.hexdigest(),
                s3_key=key,
            )
        except BaseException:
            if upload_id is not None:
                try:
                    await asyncio.to_thread(
                        lambda: s3.abort_multipart_upload(
                            Bucket=bucket, Key=key, UploadId=upload_id,
                        )
                    )
                except Exception as e:
                    logger.warning(
                        "multipart abort failed (will be GC'd later): %s", e
                    )
            raise

    @staticmethod
    def _upload_part(
        s3: Any, bucket: str, key: str, upload_id: str,
        part_no: int, body: bytes,
    ) -> str:
        return s3.upload_part(
            Bucket=bucket, Key=key, UploadId=upload_id,
            PartNumber=part_no, Body=body,
        )["ETag"]
