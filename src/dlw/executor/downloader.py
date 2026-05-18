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
from typing import Any

from dlw.executor._io import (
    _HTTP_CHUNK_BYTES,
    _TRANSIENT_RETRY,
    make_s3_client,
)
from dlw.executor._io import (
    compose_key as _compose_key_io,
)
from dlw.executor._io import (
    upload_part as _upload_part_io,
)
from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.types import Assignment, DownloadResult  # re-exported for callers
from dlw.schemas.storage import StorageConfig

__all__ = ["Assignment", "DownloadResult", "HfS3StreamDownloader"]

logger = logging.getLogger(__name__)


class HfS3StreamDownloader:
    """HF GET stream → S3 multipart upload, sha256 tee'd on the same bytes."""

    def __init__(self, *, settings: ExecutorSettings,
                 client: ControllerClient) -> None:
        self._s = settings
        self._controller = client

    def _compose_key(self, a: Assignment) -> str:
        return _compose_key_io(a)

    def _make_s3_client(self, cfg: StorageConfig) -> Any:
        return make_s3_client(self._s, cfg)

    async def download(self, *, assignment: Assignment) -> DownloadResult:
        """Public entry — retries transient errors (5xx, network, timeout) × 3."""
        @_TRANSIENT_RETRY
        async def _retry_wrapper() -> DownloadResult:
            return await self._download_once(assignment=assignment)
        return await _retry_wrapper()

    async def _download_once(self, *, assignment: Assignment) -> DownloadResult:
        s3 = self._make_s3_client(assignment.storage_config)
        bucket = assignment.storage_config.bucket
        key = self._compose_key(assignment)
        part_size = self._s.multipart_part_size_bytes

        upload_id: str | None = None
        sha = hashlib.sha256()
        bytes_total = 0
        parts: list[dict[str, Any]] = []
        buf = bytearray()
        part_no = 1

        try:
            async with self._controller.stream_source(
                subtask_id=assignment.subtask_id,
                assignment_token=assignment.assignment_token,
            ) as resp:
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
        return _upload_part_io(s3, bucket, key, upload_id, part_no, body)
