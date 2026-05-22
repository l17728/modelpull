"""Shared dataclass types for the executor package.

Extracted here to break the circular import between _io.py and downloader.py.
Both files import from this module; downloader.py re-exports for backward compat.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from dlw.schemas.storage import StorageConfig


@dataclass(frozen=True)
class ChunkAssignment:
    chunk_index: int
    byte_start: int
    byte_end: int
    source_id: str


@dataclass(frozen=True)
class Assignment:
    """Slim payload passed from runner to downloader."""
    subtask_id: uuid.UUID
    task_id: uuid.UUID
    assignment_token: uuid.UUID
    repo_id: str
    revision: str
    filename: str
    file_size: int | None
    expected_sha256: str | None
    storage_config: StorageConfig
    chunks: tuple[ChunkAssignment, ...] = ()


@dataclass(frozen=True)
class DownloadResult:
    bytes_written: int
    actual_sha256: str
    s3_key: str
