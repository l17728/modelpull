"""DirectOffsetDownloader — Phase 2 W2b1 chunk-level pipeline.

Parallel HTTP Range pulls → .parts/<subtask_id>/<idx>.bin → sequential
S3 multipart upload with streaming SHA256.
"""
from __future__ import annotations

import asyncio
import dataclasses
import errno
import hashlib
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dlw.executor._io import (
    _HTTP_CHUNK_BYTES,
    _TRANSIENT_RETRY,
    compose_key,
    make_s3_client,
    upload_part,
)
from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.parts_dir import cleanup_parts_dir, parts_dir_for
from dlw.executor.types import Assignment, DownloadResult

logger = logging.getLogger(__name__)


class DiskFullError(Exception):
    """ENOSPC during chunk write. Runner translates to paused_disk_full report."""


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    offset: int
    length: int


def plan_chunks(file_size: int, chunk_size: int) -> list[ChunkPlan]:
    if file_size <= 0:
        return []
    n = math.ceil(file_size / chunk_size)
    out: list[ChunkPlan] = []
    offset = 0
    for i in range(n):
        length = min(chunk_size, file_size - offset)
        out.append(ChunkPlan(index=i, offset=offset, length=length))
        offset += length
    return out


def _plans_from_chunks(chunks, file_size: int | None) -> list[ChunkPlan] | None:
    """One ChunkPlan per subtask_chunks row, ordered by chunk_index, RENUMBERED
    to dense positional indices 0..n-1 (pass2 uses ChunkPlan.index for
    last-chunk detection / S3 PartNumber / <idx>.bin name). None if the rows
    don't contiguously tile [0, file_size-1] (defensive → caller falls back)."""
    ordered = sorted(chunks, key=lambda c: c.chunk_index)
    plans: list[ChunkPlan] = []
    expect = 0
    for pos, c in enumerate(ordered):
        if c.byte_start != expect or c.byte_end < c.byte_start:
            return None
        plans.append(ChunkPlan(index=pos, offset=c.byte_start,
                               length=c.byte_end - c.byte_start + 1))
        expect = c.byte_end + 1
    if not plans:
        return None
    if file_size is not None and expect != file_size:
        return None
    return plans


class DirectOffsetDownloader:
    def __init__(self, *, settings: ExecutorSettings,
                 client: ControllerClient) -> None:
        self._s = settings
        self._controller = client
        assert settings.chunk_size_bytes >= 5 * 1024 * 1024, \
            f"chunk_size_bytes ({settings.chunk_size_bytes}) < 5 MiB"

    @staticmethod
    def _open_writer(path: Path):
        """Test seam — overridable to inject failures (e.g. ENOSPC)."""
        return path.open("wb")

    async def _resolve_size(self, a: Assignment) -> Assignment:
        """W3b: the proxy is GET-only, so probe size with a bytes=0-0 range
        request and read Content-Range (`bytes 0-0/<total>`). Fall back to
        Content-Length if HF answered a full 200 instead of a 206."""
        async with self._controller.stream_source(
            subtask_id=a.subtask_id,
            assignment_token=a.assignment_token,
            range_header="bytes=0-0",
        ) as resp:
            resp.raise_for_status()
            content_range = resp.headers.get("Content-Range")
            content_length = resp.headers.get("Content-Length")
        if content_range and "/" in content_range:
            total = content_range.rsplit("/", 1)[1].strip()
            if total.isdigit():
                return dataclasses.replace(a, file_size=int(total))
        # Content-Length fallback: only correct when HF answered a full 200
        # (then it is the total size). A well-behaved 206 always carries
        # Content-Range, handled above.
        if content_length is None:
            raise RuntimeError(
                "file_size unresolvable: no Content-Range or Content-Length"
            )
        return dataclasses.replace(a, file_size=int(content_length))

    async def download(self, *, assignment: Assignment) -> DownloadResult:
        if assignment.chunks:
            plans = _plans_from_chunks(assignment.chunks, assignment.file_size)
            if plans is not None and assignment.file_size is None:
                assignment = dataclasses.replace(
                    assignment, file_size=plans[-1].offset + plans[-1].length)
            if plans is None:
                logger.warning(
                    "subtask %s chunk rows don't tile file; falling back to "
                    "local split", assignment.subtask_id)
                if assignment.file_size is None:
                    assignment = await self._resolve_size(assignment)
                plans = plan_chunks(assignment.file_size, self._s.chunk_size_bytes)
        else:
            if assignment.file_size is None:
                assignment = await self._resolve_size(assignment)
            plans = plan_chunks(assignment.file_size, self._s.chunk_size_bytes)
        dest_dir = parts_dir_for(self._s.parts_dir_path, assignment.subtask_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            await self._pass1_parallel(assignment, plans, dest_dir)
        except DiskFullError:
            # Deliberately leak parts; sweeper + startup GC reap.
            raise
        except Exception:
            cleanup_parts_dir(self._s.parts_dir_path, assignment.subtask_id)
            raise
        # Pass 2 manages its own cleanup (success + abort paths).
        return await self._pass2_upload(assignment, plans, dest_dir)

    async def _pass1_parallel(
        self, a: Assignment, plans: list[ChunkPlan], dest_dir: Path,
    ) -> None:
        sem = asyncio.Semaphore(self._s.chunk_concurrency)

        async def one(plan: ChunkPlan) -> None:
            async with sem:
                await self._download_one_chunk(a, plan, dest_dir)

        await asyncio.gather(*(one(p) for p in plans))

    @_TRANSIENT_RETRY
    async def _download_one_chunk(
        self, a: Assignment, plan: ChunkPlan, dest_dir: Path,
    ) -> None:
        range_header = f"bytes={plan.offset}-{plan.offset + plan.length - 1}"
        async with self._controller.stream_source(
            subtask_id=a.subtask_id,
            assignment_token=a.assignment_token,
            range_header=range_header,
        ) as resp:
            resp.raise_for_status()
            target = dest_dir / f"{plan.index}.bin"
            try:
                with self._open_writer(target) as f:
                    async for buf in resp.aiter_bytes(_HTTP_CHUNK_BYTES):
                        try:
                            f.write(buf)
                        except OSError as ex:
                            if ex.errno == errno.ENOSPC:
                                raise DiskFullError(
                                    f"ENOSPC writing chunk {plan.index}"
                                ) from ex
                            raise
            except DiskFullError:
                raise

    async def _pass2_upload(
        self, a: Assignment, plans: list[ChunkPlan], dest_dir: Path,
    ) -> DownloadResult:
        s3 = make_s3_client(self._s, a.storage_config)
        bucket = a.storage_config.bucket
        key = compose_key(a)
        upload_id = await asyncio.to_thread(
            lambda: s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
        )
        sha = hashlib.sha256()
        parts: list[dict[str, Any]] = []
        try:
            for plan in plans:
                src = dest_dir / f"{plan.index}.bin"
                body = src.read_bytes()
                if len(body) != plan.length and plan.index != len(plans) - 1:
                    raise RuntimeError(
                        f"chunk {plan.index} short: got {len(body)} expected {plan.length}"
                    )
                sha.update(body)
                etag = await asyncio.to_thread(
                    upload_part, s3, bucket, key, upload_id,
                    plan.index + 1, body,
                )
                parts.append({"PartNumber": plan.index + 1, "ETag": etag})
            await asyncio.to_thread(lambda: s3.complete_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            ))
            cleanup_parts_dir(self._s.parts_dir_path, a.subtask_id)
            return DownloadResult(
                bytes_written=a.file_size,
                actual_sha256=sha.hexdigest(),
                s3_key=key,
            )
        except BaseException:
            try:
                await asyncio.to_thread(lambda: s3.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id,
                ))
            except Exception as e:
                logger.warning("multipart abort failed: %s", e)
            cleanup_parts_dir(self._s.parts_dir_path, a.subtask_id)
            raise
