"""Tests for dlw.services.recovery (Phase 2 W1)."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.recovery import (
    RecoveryStats,
    reclaim_stale_executors,
    run_recovery_routine,
    verify_remote_state,
)


_BUCKET = "recovery-bucket"


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    """Tenant + project + user + storage with proper JSON config."""
    storage_config = json.dumps({
        "bucket": _BUCKET, "region": "us-east-1",
        "endpoint_url": None, "key_prefix": "phase2/",
    }).encode("utf-8")
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(
        id=1, tenant_id=1, name="d", backend_type="s3",
        config_encrypted=storage_config, region="us-east-1",
    ))
    await db_session.flush()


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


async def _make_task_with_subtask(db_session, file_size=4096):
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/recovery", revision="a" * 40, storage_id=1,
        path_template="t/{tenant}", priority=1, status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="weight.bin",
        file_size=file_size, expected_sha256=None, status="assigned",
        executor_id="recovery-host-worker-1", executor_epoch=1,
        assignment_token=uuid.uuid4(),
        multipart_upload_id="some-mpu-id",
    )
    db_session.add(sub)
    await db_session.flush()
    return task, sub


@pytest.mark.slow
async def test_verify_remote_state_missing_returns_missing(
    db_session, env, aws_env,
) -> None:
    """If S3 object doesn't exist, verify_remote_state returns 'missing'."""
    task, sub = await _make_task_with_subtask(db_session)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        # Object NOT uploaded
        result = await verify_remote_state(db_session, sub)
    assert result == "missing"


@pytest.mark.slow
async def test_verify_remote_state_size_match_returns_verified(
    db_session, env, aws_env,
) -> None:
    task, sub = await _make_task_with_subtask(db_session, file_size=4096)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        key = f"phase2/o/recovery/{'a' * 40}/weight.bin"
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"x" * 4096)
        result = await verify_remote_state(db_session, sub)
    assert result == "verified"


@pytest.mark.slow
async def test_verify_remote_state_size_mismatch_returns_size_mismatch(
    db_session, env, aws_env,
) -> None:
    task, sub = await _make_task_with_subtask(db_session, file_size=4096)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        key = f"phase2/o/recovery/{'a' * 40}/weight.bin"
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"y" * 100)   # wrong size!
        result = await verify_remote_state(db_session, sub)
    assert result == "size_mismatch"


@pytest.mark.slow
async def test_run_recovery_routine_resets_long_assigned(
    db_session, env,
) -> None:
    """Subtask assigned > 2× heartbeat ago + no multipart → reset to pending."""
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="a" * 40, storage_id=1,
        path_template="t", priority=1, status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    long_ago = datetime.now(UTC) - timedelta(seconds=300)
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="x.bin",
        file_size=100, status="assigned",
        executor_id="stale-host-worker-1", executor_epoch=1,
        assignment_token=uuid.uuid4(), assigned_at=long_ago,
        multipart_upload_id=None,
    )
    db_session.add(sub)
    await db_session.flush()

    stats = await run_recovery_routine(db_session)
    await db_session.flush()
    await db_session.refresh(sub)
    assert sub.status == "pending"
    assert sub.executor_id is None
    assert sub.executor_epoch is None
    assert stats.no_multipart_reset == 1


@pytest.mark.slow
async def test_run_recovery_routine_three_way_verified_marks_succeeded(
    db_session, env, aws_env,
) -> None:
    """In-flight subtask whose S3 object is intact → marked succeeded."""
    task, sub = await _make_task_with_subtask(db_session, file_size=2048)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        key = f"phase2/o/recovery/{'a' * 40}/weight.bin"
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"z" * 2048)

        stats = await run_recovery_routine(db_session)
    await db_session.flush()
    await db_session.refresh(sub)
    assert sub.status == "succeeded"
    assert stats.verified_recovered == 1


@pytest.mark.slow
async def test_run_recovery_routine_aborts_orphan_multiparts(
    db_session, env, aws_env,
) -> None:
    """Terminal subtask with multipart_upload_id still set → abort + clear field."""
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="a" * 40, storage_id=1,
        path_template="t", priority=1, status="succeeded",
    )
    db_session.add(task)
    await db_session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="x.bin",
        file_size=100, status="succeeded",
        multipart_upload_id="orphan-mpu-id",
    )
    db_session.add(sub)
    await db_session.flush()

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        stats = await run_recovery_routine(db_session)

    await db_session.flush()
    await db_session.refresh(sub)
    assert sub.multipart_upload_id is None
    assert stats.orphan_aborted == 1
