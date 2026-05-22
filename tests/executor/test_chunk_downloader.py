"""Tests for DirectOffsetDownloader (Phase 2 W2b1)."""
from __future__ import annotations

import pytest

from dlw.executor.chunk_downloader import ChunkPlan, DiskFullError, plan_chunks
from dlw.executor.types import ChunkAssignment


def test_plan_chunks_splits_evenly() -> None:
    plans = plan_chunks(100, 30)
    assert plans == [
        ChunkPlan(index=0, offset=0,  length=30),
        ChunkPlan(index=1, offset=30, length=30),
        ChunkPlan(index=2, offset=60, length=30),
        ChunkPlan(index=3, offset=90, length=10),
    ]


def test_plan_chunks_exact_multiple() -> None:
    plans = plan_chunks(60, 30)
    assert plans == [
        ChunkPlan(index=0, offset=0,  length=30),
        ChunkPlan(index=1, offset=30, length=30),
    ]


def test_plan_chunks_smaller_than_chunk_size() -> None:
    plans = plan_chunks(5 * 1024 * 1024, 16 * 1024 * 1024)
    assert plans == [ChunkPlan(index=0, offset=0, length=5 * 1024 * 1024)]


def test_plan_chunks_zero_file_size_returns_empty() -> None:
    assert plan_chunks(0, 16 * 1024 * 1024) == []


def test_disk_full_error_is_exception_subclass() -> None:
    """Smoke: ensure the public DiskFullError is importable and an Exception."""
    e = DiskFullError("ENOSPC writing chunk 3")
    assert isinstance(e, Exception)
    assert "ENOSPC" in str(e)


import asyncio
import errno
import hashlib
import uuid
from typing import Any

import boto3
import httpx
from moto import mock_aws

from dlw.executor.chunk_downloader import DirectOffsetDownloader
from dlw.executor.config import ExecutorSettings
from dlw.executor.types import Assignment
from dlw.schemas.storage import StorageConfig
from tests.conftest import make_fake_controller_client

_FILE_SIZE = 200 * 1024 * 1024   # 200 MiB → 4 chunks at 64 MiB
_CHUNK_SIZE = 64 * 1024 * 1024
_SYNTHETIC = bytes((i * 13 + 7) % 256 for i in range(_FILE_SIZE))
_EXPECTED_SHA = hashlib.sha256(_SYNTHETIC).hexdigest()


def _synthetic_hf_handler():
    """httpx handler: HTTP 206 Partial Content reading the Range header."""
    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("Range", "")
        assert rng.startswith("bytes="), f"unexpected Range header: {rng!r}"
        a, b = rng.removeprefix("bytes=").split("-")
        start, end = int(a), int(b)
        body = _SYNTHETIC[start:end + 1]
        return httpx.Response(
            status_code=206,
            content=body,
            headers={"Content-Length": str(len(body))},
        )
    return handler


@pytest.fixture
def chunk_settings(tmp_path) -> ExecutorSettings:
    return ExecutorSettings(
        id="ex-chunk-test",
        bearer_token="t",
        chunk_size_bytes=_CHUNK_SIZE,
        chunk_concurrency=2,
        parts_dir_path=str(tmp_path / "parts"),
        s3_region="us-east-1",
        s3_endpoint_url=None,
    )


async def test_pass1_pass2_happy_path_with_moto(chunk_settings) -> None:
    """Full pipeline: 4 chunks via the fake controller proxy -> moto multipart
    -> sha256 matches."""
    storage_config = StorageConfig(
        bucket="test-bucket", region="us-east-1", endpoint_url=None,
        access_key_id="dummy", secret_access_key="dummy",
        key_prefix="phase1",
    )

    sub_id = uuid.uuid4()
    a = Assignment(
        subtask_id=sub_id,
        task_id=uuid.uuid4(),
        assignment_token=uuid.uuid4(),
        repo_id="owner/repo",
        revision="b" * 40,
        filename="model.bin",
        file_size=_FILE_SIZE,
        expected_sha256=None,
        storage_config=storage_config,
    )

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")

        d = DirectOffsetDownloader(
            settings=chunk_settings,
            client=make_fake_controller_client(_synthetic_hf_handler()),
        )
        result = await d.download(assignment=a)

    assert result.bytes_written == _FILE_SIZE
    assert result.actual_sha256 == _EXPECTED_SHA
    assert result.s3_key.endswith("model.bin")

    from dlw.executor.parts_dir import parts_dir_for
    assert not parts_dir_for(chunk_settings.parts_dir_path, sub_id).exists()


async def test_pass1_enospc_raises_disk_full_and_leaks_parts(
    chunk_settings, monkeypatch,
) -> None:
    """Inject ENOSPC into pass-1 chunk write -> DiskFullError + parts NOT cleaned."""
    from dlw.executor import chunk_downloader as cd_mod

    class _NoSpaceWriter:
        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")
        def __enter__(self): return self
        def __exit__(self, *_): return False

    monkeypatch.setattr(
        cd_mod.DirectOffsetDownloader,
        "_open_writer",
        staticmethod(lambda path: _NoSpaceWriter()),
    )

    storage_config = StorageConfig(
        bucket="test-bucket", region="us-east-1", endpoint_url=None,
        access_key_id="dummy", secret_access_key="dummy",
        key_prefix="phase1",
    )
    sub_id = uuid.uuid4()
    a = Assignment(
        subtask_id=sub_id,
        task_id=uuid.uuid4(),
        assignment_token=uuid.uuid4(),
        repo_id="owner/repo",
        revision="b" * 40,
        filename="model.bin",
        file_size=_FILE_SIZE,
        expected_sha256=None,
        storage_config=storage_config,
    )

    d = DirectOffsetDownloader(
        settings=chunk_settings,
        client=make_fake_controller_client(_synthetic_hf_handler()),
    )
    from dlw.executor.chunk_downloader import DiskFullError
    with pytest.raises(DiskFullError):
        await d.download(assignment=a)

    from dlw.executor.parts_dir import parts_dir_for
    assert parts_dir_for(chunk_settings.parts_dir_path, sub_id).exists()


async def test_resolve_size_via_range_probe(chunk_settings) -> None:
    """When file_size is None, _resolve_size does a bytes=0-0 probe and reads
    Content-Range to recover the total size."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Range") == "bytes=0-0"
        return httpx.Response(
            206, content=b"\x00",
            headers={"Content-Range": "bytes 0-0/123456", "Content-Length": "1"},
        )

    d = DirectOffsetDownloader(
        settings=chunk_settings,
        client=make_fake_controller_client(handler),
    )
    a = Assignment(
        subtask_id=uuid.uuid4(), task_id=uuid.uuid4(),
        assignment_token=uuid.uuid4(),
        repo_id="o/r", revision="b" * 40, filename="big.bin",
        file_size=None, expected_sha256=None,
        storage_config=StorageConfig(
            bucket="b", region="us-east-1", endpoint_url=None,
            key_prefix="p",
        ),
    )
    resolved = await d._resolve_size(a)
    assert resolved.file_size == 123456


async def test_resolve_size_falls_back_to_content_length(chunk_settings) -> None:
    """If HF answers a 200 (no Content-Range), _resolve_size uses Content-Length."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"\x00" * 10,
            headers={"Content-Length": "777"},
        )

    d = DirectOffsetDownloader(
        settings=chunk_settings,
        client=make_fake_controller_client(handler),
    )
    a = Assignment(
        subtask_id=uuid.uuid4(), task_id=uuid.uuid4(),
        assignment_token=uuid.uuid4(),
        repo_id="o/r", revision="b" * 40, filename="big.bin",
        file_size=None, expected_sha256=None,
        storage_config=StorageConfig(
            bucket="b", region="us-east-1", endpoint_url=None,
            key_prefix="p",
        ),
    )
    resolved = await d._resolve_size(a)
    assert resolved.file_size == 777


async def test_resolve_size_raises_when_no_headers(chunk_settings) -> None:
    """If neither Content-Range nor Content-Length is present, raise RuntimeError."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Use stream= to prevent httpx from auto-injecting Content-Length.
        return httpx.Response(200, stream=httpx.ByteStream(b"\x00"))  # no size headers

    d = DirectOffsetDownloader(
        settings=chunk_settings,
        client=make_fake_controller_client(handler),
    )
    a = Assignment(
        subtask_id=uuid.uuid4(), task_id=uuid.uuid4(),
        assignment_token=uuid.uuid4(),
        repo_id="o/r", revision="b" * 40, filename="big.bin",
        file_size=None, expected_sha256=None,
        storage_config=StorageConfig(
            bucket="b", region="us-east-1", endpoint_url=None,
            key_prefix="p",
        ),
    )
    with pytest.raises(RuntimeError, match="unresolvable"):
        await d._resolve_size(a)


# ---------------------------------------------------------------------------
# Task 4: chunk-row-aligned Range headers
# ---------------------------------------------------------------------------

_TINY_BLOB = bytes((i * 7 + 3) % 256 for i in range(32))
_TINY_SHA = hashlib.sha256(_TINY_BLOB).hexdigest()

# For the aligned multi-chunk test: S3 multipart requires every part except the
# last to be >= 5 MiB, so chunk 0 must be a full 5 MiB and chunk 1 the small
# remainder. Cheap repeating pattern (sha computed from the same blob).
_5MIB = 5 * 1024 * 1024
_ALIGN_SIZE = _5MIB + 32
_ALIGN_BLOB = (b"\xab\xcd\xef\x12" * (_ALIGN_SIZE // 4 + 1))[:_ALIGN_SIZE]
_ALIGN_SHA = hashlib.sha256(_ALIGN_BLOB).hexdigest()


def _recording_handler(blob: bytes, recorded_ranges: list[str]):
    """httpx handler that records each Range header and serves bytes from blob."""
    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("Range", "")
        recorded_ranges.append(rng)
        if rng.startswith("bytes="):
            a, b = rng.removeprefix("bytes=").split("-")
            start, end = int(a), int(b)
            body = blob[start:end + 1]
        else:
            body = blob
        return httpx.Response(
            status_code=206,
            content=body,
            headers={"Content-Length": str(len(body))},
        )
    return handler


@pytest.fixture
def tiny_settings(tmp_path) -> ExecutorSettings:
    return ExecutorSettings(
        id="ex-tiny-test",
        bearer_token="t",
        chunk_size_bytes=5 * 1024 * 1024,   # 5 MiB — tiny blob is one chunk if split locally
        chunk_concurrency=2,
        parts_dir_path=str(tmp_path / "parts"),
        s3_region="us-east-1",
        s3_endpoint_url=None,
    )


async def test_chunked_assignment_aligns_ranges_to_rows(tiny_settings) -> None:
    """When Assignment.chunks is set and tiles the file, download must issue
    exactly one Range per chunk row (not the local plan_chunks split)."""
    recorded: list[str] = []
    storage_config = StorageConfig(
        bucket="test-bucket", region="us-east-1", endpoint_url=None,
        access_key_id="dummy", secret_access_key="dummy",
        key_prefix="p",
    )
    sub_id = uuid.uuid4()
    a = Assignment(
        subtask_id=sub_id,
        task_id=uuid.uuid4(),
        assignment_token=uuid.uuid4(),
        repo_id="o/r",
        revision="a" * 40,
        filename="tiny.bin",
        file_size=_ALIGN_SIZE,
        expected_sha256=None,
        storage_config=storage_config,
        chunks=(
            ChunkAssignment(0, 0, _5MIB - 1, "modelscope"),
            ChunkAssignment(1, _5MIB, _ALIGN_SIZE - 1, "hf_mirror"),
        ),
    )

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")

        d = DirectOffsetDownloader(
            settings=tiny_settings,
            client=make_fake_controller_client(_recording_handler(_ALIGN_BLOB, recorded)),
        )
        result = await d.download(assignment=a)

    # Exactly one Range per chunk row (chunk-row boundaries), NOT the local
    # plan_chunks 5-MiB split. Order-independent: the two chunks download
    # concurrently (chunk_concurrency=2).
    assert set(recorded) == {f"bytes=0-{_5MIB - 1}",
                             f"bytes={_5MIB}-{_ALIGN_SIZE - 1}"}, (
        f"expected chunk-row Ranges, got {recorded!r}"
    )
    assert len(recorded) == 2
    assert result.actual_sha256 == _ALIGN_SHA
    assert result.bytes_written == _ALIGN_SIZE


async def test_malformed_chunks_fall_back_to_plan_chunks(tiny_settings) -> None:
    """When chunk rows leave a gap (don't tile [0, file_size-1]), download must
    fall back to the local plan_chunks split and still produce the correct SHA."""
    recorded: list[str] = []
    storage_config = StorageConfig(
        bucket="test-bucket", region="us-east-1", endpoint_url=None,
        access_key_id="dummy", secret_access_key="dummy",
        key_prefix="p",
    )
    sub_id = uuid.uuid4()
    # Gap at bytes 16-19: byte_start of chunk 1 is 20, not 16
    a = Assignment(
        subtask_id=sub_id,
        task_id=uuid.uuid4(),
        assignment_token=uuid.uuid4(),
        repo_id="o/r",
        revision="a" * 40,
        filename="tiny.bin",
        file_size=32,
        expected_sha256=None,
        storage_config=storage_config,
        chunks=(
            ChunkAssignment(0, 0, 15, "a"),
            ChunkAssignment(1, 20, 31, "b"),   # gap 16..19
        ),
    )

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")

        d = DirectOffsetDownloader(
            settings=tiny_settings,
            client=make_fake_controller_client(_recording_handler(_TINY_BLOB, recorded)),
        )
        result = await d.download(assignment=a)

    # Fallback: 5 MiB chunk_size covers the whole 32-byte blob in one request
    assert recorded == ["bytes=0-31"], (
        f"expected single plan_chunks Range on fallback, got {recorded!r}"
    )
    assert result.actual_sha256 == _TINY_SHA
    assert result.bytes_written == 32
