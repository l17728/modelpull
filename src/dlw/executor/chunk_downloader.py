"""DirectOffsetDownloader — Phase 2 W2b1 chunk-level pipeline.

Parallel HTTP Range pulls → .parts/<subtask_id>/<idx>.bin → sequential
S3 multipart upload with streaming SHA256.

W2b1 M1 ships the public surface + plan_chunks + DiskFullError. Pass 1
(parallel download) and Pass 2 (sequential upload) are added in M2.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from dlw.executor.config import ExecutorSettings
from dlw.executor.types import Assignment, DownloadResult


class DiskFullError(Exception):
    """ENOSPC during chunk write. Runner translates to paused_disk_full report."""


@dataclass(frozen=True)
class ChunkPlan:
    """Slice of [0, file_size) handled by a single Range request."""
    index: int       # 0..N-1
    offset: int      # inclusive
    length: int      # bytes; last chunk may be < chunk_size


def plan_chunks(file_size: int, chunk_size: int) -> list[ChunkPlan]:
    """Split [0, file_size) into ChunkPlans of length chunk_size (last may be smaller).

    Returns [] when file_size <= 0. S3 multipart constraints (INVARIANT D-22):
    callers must ensure chunk_size >= 5 MiB; this function does not enforce it.
    """
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


class DirectOffsetDownloader:
    """W2b1 M1: skeleton — download() raises NotImplementedError until M2."""

    def __init__(self, *, settings: ExecutorSettings) -> None:
        self._s = settings
        # Fail fast if operator misconfigured chunk_size below S3 min part size.
        assert settings.chunk_size_bytes >= 5 * 1024 * 1024, \
            f"chunk_size_bytes ({settings.chunk_size_bytes}) < 5 MiB"

    async def download(self, *, assignment: Assignment) -> DownloadResult:
        raise NotImplementedError("DirectOffsetDownloader.download lands in W2b1 M2")
