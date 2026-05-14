"""Tests for HfS3StreamDownloader — skeleton + helpers (W4 Task 9).

Pipeline tests (HF→S3 stream) come in W4 Task 10/11.
"""
from __future__ import annotations

import asyncio
import hashlib
import os

import boto3 as _boto3
import httpx
import pytest
from moto import mock_aws

from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import (
    Assignment,
    DownloadResult,
    HfS3StreamDownloader,
    StorageConfig,
)
from tests.conftest import make_fake_controller_client


def _settings() -> ExecutorSettings:
    return ExecutorSettings(
        id="host-test-worker-1",
        bearer_token="t",
    )


def _assignment(*, repo_id="o/r", revision="a" * 40, filename="config.json",
                key_prefix="phase1/", bucket="b") -> Assignment:
    import uuid as _uuid
    return Assignment(
        subtask_id=_uuid.uuid4(),
        task_id=_uuid.uuid4(),
        assignment_token=_uuid.uuid4(),
        repo_id=repo_id, revision=revision, filename=filename,
        file_size=4096, expected_sha256=None,
        storage_config=StorageConfig(bucket=bucket, key_prefix=key_prefix),
    )


def _noop_client():
    return make_fake_controller_client(
        lambda request: httpx.Response(200, content=b"")
    )


def test_compose_key_includes_prefix_repo_revision_filename() -> None:
    d = HfS3StreamDownloader(settings=_settings(), client=_noop_client())
    a = _assignment(filename="model.safetensors", key_prefix="phase1/")
    key = d._compose_key(a)
    assert key == "phase1/o/r/" + ("a" * 40) + "/model.safetensors"


def test_compose_key_handles_empty_prefix() -> None:
    d = HfS3StreamDownloader(settings=_settings(), client=_noop_client())
    a = _assignment(key_prefix="")
    key = d._compose_key(a)
    assert key.startswith("o/r/")


def test_compose_key_strips_prefix_trailing_slash() -> None:
    d = HfS3StreamDownloader(settings=_settings(), client=_noop_client())
    a = _assignment(key_prefix="phase1////")
    key = d._compose_key(a)
    # No double slashes, single separator
    assert "//" not in key
    assert key.startswith("phase1/o/r/")


# fixture: spin up moto[s3] in-process + create a bucket
@pytest.fixture
def s3_bucket(monkeypatch: pytest.MonkeyPatch):
    """In-process moto[s3] + a fresh bucket. Yields the bucket name."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = _boto3.client("s3", region_name="us-east-1")
        bucket = "test-bucket"
        client.create_bucket(Bucket=bucket)
        yield bucket


def _hf_handler(body_bytes: bytes):
    """Returns an httpx handler that streams body_bytes for any request."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body_bytes)
    return handler


@pytest.mark.slow
async def test_downloader_streams_hf_to_s3_full_pipeline(
    s3_bucket: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full HF→S3 pipeline: 6MB body → 2 parts (5MB + 1MB) → complete_multipart."""
    body = os.urandom(6 * 1024 * 1024)
    expected_sha = hashlib.sha256(body).hexdigest()

    settings = ExecutorSettings(
        id="host-w4-worker-1", bearer_token="t",
        s3_endpoint_url=None,             # moto via env
    )
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(_hf_handler(body)))

    a = _assignment(
        filename="model.safetensors", key_prefix="phase1/",
        bucket=s3_bucket,
    )
    result = await d.download(assignment=a)

    assert result.bytes_written == len(body)
    assert result.actual_sha256 == expected_sha
    assert result.s3_key == f"phase1/o/r/{'a' * 40}/model.safetensors"

    # Verify the object exists in moto and its bytes match
    s3 = _boto3.client("s3", region_name="us-east-1")
    obj = s3.get_object(Bucket=s3_bucket, Key=result.s3_key)
    assert obj["Body"].read() == body


@pytest.mark.slow
async def test_downloader_small_file_single_part(
    s3_bucket: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub-5MB body → 1 part (allowed for the LAST part only)."""
    body = b"x" * (3 * 1024 * 1024)       # 3MB
    expected_sha = hashlib.sha256(body).hexdigest()

    settings = ExecutorSettings(id="host-w4-worker-2", bearer_token="t")
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(_hf_handler(body)))
    a = _assignment(filename="config.json", bucket=s3_bucket)

    result = await d.download(assignment=a)
    assert result.bytes_written == len(body)
    assert result.actual_sha256 == expected_sha


@pytest.mark.slow
async def test_downloader_exact_5mb_yields_one_part(
    s3_bucket: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly 5MB body → 1 part (boundary case).

    W5-H: a 5MB body fills buf to part_size, the `while` loop flushes ONE
    part, then the stream ends with empty buf so the last-part `if buf` is
    skipped. Total = 1 part.
    """
    body = b"y" * (5 * 1024 * 1024)
    expected_sha = hashlib.sha256(body).hexdigest()

    settings = ExecutorSettings(id="host-w4-worker-3", bearer_token="t")
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(_hf_handler(body)))
    a = _assignment(filename="exact5.bin", bucket=s3_bucket)

    result = await d.download(assignment=a)
    assert result.bytes_written == len(body)
    assert result.actual_sha256 == expected_sha


@pytest.mark.slow
async def test_downloader_404_fails_fast_no_multipart(
    s3_bucket: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HF 404 raises before create_multipart_upload — no orphaned MPU.

    W5-E: use 4xx (not 5xx) so tenacity retry doesn't add 3s wait per CI run.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    settings = ExecutorSettings(id="host-w4-worker-x", bearer_token="t")
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(handler))
    a = _assignment(filename="x.bin", bucket=s3_bucket)

    with pytest.raises(httpx.HTTPStatusError):
        await d.download(assignment=a)

    s3 = _boto3.client("s3", region_name="us-east-1")
    listing = s3.list_objects_v2(Bucket=s3_bucket)
    assert listing.get("KeyCount", 0) == 0
    mpu = s3.list_multipart_uploads(Bucket=s3_bucket)
    assert "Uploads" not in mpu or len(mpu["Uploads"]) == 0


@pytest.mark.slow
async def test_downloader_aborts_multipart_on_mid_stream_drop(
    s3_bucket: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W5-B: real abort path — first chunk arrives, then stream raises.

    By the time the protocol error fires, create_multipart_upload has run
    and at least one upload_part may be in flight. The abort_multipart_upload
    in the BaseException handler must reclaim the in-progress upload.
    """
    # Stream that yields one 5MB chunk then raises — forces parts list non-empty
    # before the protocol error so the abort path is genuinely exercised.
    chunk_a = b"a" * (5 * 1024 * 1024)

    async def streaming_body():
        yield chunk_a
        raise httpx.RemoteProtocolError("connection dropped mid-stream")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=streaming_body())

    settings = ExecutorSettings(id="host-w4-worker-mid", bearer_token="t")
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(handler))
    a = _assignment(filename="dropped.bin", bucket=s3_bucket)

    # ProtocolError is transient → tenacity retries × 3 → all fail → reraise.
    # Each attempt creates + aborts its own multipart upload.
    with pytest.raises(httpx.RemoteProtocolError):
        await d.download(assignment=a)

    s3 = _boto3.client("s3", region_name="us-east-1")
    # No completed object
    assert s3.list_objects_v2(Bucket=s3_bucket).get("KeyCount", 0) == 0
    # No in-progress multipart upload — abort fired on every attempt
    mpu = s3.list_multipart_uploads(Bucket=s3_bucket)
    assert "Uploads" not in mpu or len(mpu["Uploads"]) == 0


@pytest.mark.slow
async def test_downloader_handles_zero_byte_file(
    s3_bucket: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W5-D: 0-byte HF body should NOT call complete_multipart with empty parts.

    Empty parts list to S3 returns MalformedXML. The downloader detects no
    parts produced and falls back to put_object with empty body, then aborts
    the unused multipart upload.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    settings = ExecutorSettings(id="host-w4-worker-zero", bearer_token="t")
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(handler))
    a = _assignment(filename="empty.bin", bucket=s3_bucket)

    result = await d.download(assignment=a)
    assert result.bytes_written == 0
    assert result.actual_sha256 == hashlib.sha256(b"").hexdigest()

    # Object exists with 0 bytes
    s3 = _boto3.client("s3", region_name="us-east-1")
    obj = s3.get_object(Bucket=s3_bucket, Key=result.s3_key)
    assert obj["Body"].read() == b""
    # No leftover multipart upload
    mpu = s3.list_multipart_uploads(Bucket=s3_bucket)
    assert "Uploads" not in mpu or len(mpu["Uploads"]) == 0


@pytest.mark.slow
async def test_downloader_retries_transient_5xx(
    s3_bucket: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 then 200 → should retry and succeed on second attempt."""
    call_count = 0
    body = b"z" * 1024     # tiny body so we don't slow the test
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, content=b"slow")
        return httpx.Response(200, content=body)

    settings = ExecutorSettings(id="host-w4-worker-r", bearer_token="t")
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(handler))
    a = _assignment(filename="retry.bin", bucket=s3_bucket)

    result = await d.download(assignment=a)
    assert result.bytes_written == len(body)
    assert call_count == 2
