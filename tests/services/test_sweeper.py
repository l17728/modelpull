"""Tests for sweep_executor_timeouts (Phase 2 W2a §3.4)."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.recovery import sweep_executor_timeouts


_BUCKET = "sweeper-bucket"


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


@pytest.mark.slow
async def test_sweep_transitions_stale_to_suspect_and_reclaims(
    db_session: AsyncSession,
    env,
) -> None:
    """A healthy executor with stale heartbeat + counter == 2 → 3rd timeout
    advances it to suspect and reclaims its 'assigned' subtask."""
    stale_time = datetime.now(UTC) - timedelta(seconds=300)
    ex = Executor(
        id="ex-stale-1", host_id="host-S", cert_fingerprint="x",
        status="healthy", epoch=1, last_heartbeat_at=stale_time,
        consecutive_heartbeat_failures=2,
    )
    db_session.add(ex)
    await db_session.flush()

    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="x.bin",
        file_size=100, status="assigned",
        executor_id="ex-stale-1", executor_epoch=1,
        assignment_token=uuid.uuid4(),
    )
    db_session.add(sub)
    await db_session.flush()

    counters = await sweep_executor_timeouts(db_session)
    await db_session.flush()

    assert counters == {"transitioned": 1, "reclaimed": 1}
    refreshed_ex = await db_session.get(Executor, "ex-stale-1")
    assert refreshed_ex.status == "suspect"
    refreshed_sub = await db_session.get(FileSubTask, sub.id)
    assert refreshed_sub.status == "pending"
    assert refreshed_sub.executor_id is None
