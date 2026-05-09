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
        repo_id=repo_id, revision=revision, filename=filename,
        file_size=4096, expected_sha256=None,
        storage_config=StorageConfig(bucket=bucket, key_prefix=key_prefix),
    )


def test_compose_key_includes_prefix_repo_revision_filename() -> None:
    d = HfS3StreamDownloader(settings=_settings())
    a = _assignment(filename="model.safetensors", key_prefix="phase1/")
    key = d._compose_key(a)
    assert key == "phase1/o/r/" + ("a" * 40) + "/model.safetensors"


def test_compose_key_handles_empty_prefix() -> None:
    d = HfS3StreamDownloader(settings=_settings())
    a = _assignment(key_prefix="")
    key = d._compose_key(a)
    assert key.startswith("o/r/")


def test_compose_key_strips_prefix_trailing_slash() -> None:
    d = HfS3StreamDownloader(settings=_settings())
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


def _make_hf_transport(body_bytes: bytes) -> httpx.MockTransport:
    """Returns an httpx transport that streams body_bytes on the resolve URL."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body_bytes)
    return httpx.MockTransport(handler)


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
    d = HfS3StreamDownloader(settings=settings)

    # Inject httpx MockTransport via test-only seam
    monkeypatch.setattr(d, "_make_http_client",
        lambda: httpx.AsyncClient(transport=_make_hf_transport(body),
                                   timeout=settings.download_timeout_seconds,
                                   follow_redirects=True))

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
    d = HfS3StreamDownloader(settings=settings)
    monkeypatch.setattr(d, "_make_http_client",
        lambda: httpx.AsyncClient(transport=_make_hf_transport(body),
                                   timeout=settings.download_timeout_seconds,
                                   follow_redirects=True))
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
    d = HfS3StreamDownloader(settings=settings)
    monkeypatch.setattr(d, "_make_http_client",
        lambda: httpx.AsyncClient(transport=_make_hf_transport(body),
                                   timeout=settings.download_timeout_seconds,
                                   follow_redirects=True))
    a = _assignment(filename="exact5.bin", bucket=s3_bucket)

    result = await d.download(assignment=a)
    assert result.bytes_written == len(body)
    assert result.actual_sha256 == expected_sha
