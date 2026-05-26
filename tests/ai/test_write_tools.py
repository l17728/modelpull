"""Write tool registry — tenant isolation + execute (UI-SP4b)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.ai.write_tools import WRITE_TOOLS
from dlw.auth.principal import Principal
from dlw.db.base import Base

TASK_T1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
TASK_T2 = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _principal(tenant_id: int, user_id: int = 1) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id,
                     role="tenant_admin", project_ids=())


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.task import DownloadTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1",
                     quota_bytes_month=10**12, quota_concurrent=5,
                     quota_storage_gb=100))
        s.add(Tenant(id=2, slug="t2", display_name="T2"))
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d1"),
            Project(id=2, tenant_id=2, name="d2"),
            User(id=1, tenant_id=1, oidc_subject="u1", email="u1@t",
                 role="tenant_admin"),
            User(id=2, tenant_id=2, oidc_subject="u2", email="u2@t",
                 role="tenant_admin"),
            StorageBackend(id=1, tenant_id=1, name="s1",
                           backend_type="s3", config_encrypted=b""),
            StorageBackend(id=2, tenant_id=2, name="s2",
                           backend_type="s3", config_encrypted=b""),
        ])
        await s.flush()
        s.add_all([
            DownloadTask(id=TASK_T1, tenant_id=1, project_id=1,
                         owner_user_id=1, storage_id=1, repo_id="org/m1",
                         revision="0" * 40,
                         path_template="hf/{model}/{revision}/{file}",
                         status="running"),
            DownloadTask(id=TASK_T2, tenant_id=2, project_id=2,
                         owner_user_id=2, storage_id=2, repo_id="org/m2",
                         revision="1" * 40,
                         path_template="hf/{model}/{revision}/{file}",
                         status="running"),
        ])
        # Explicit-id inserts above don't advance the sequence, so next
        # insert via the AI tool would collide with id=1/2. Bump sequences
        # past the seeded rows so subsequent inserts use safe ids.
        from sqlalchemy import text
        for seq, last in [
            ("users_id_seq", 2), ("storage_backends_id_seq", 2),
            ("projects_id_seq", 2), ("tenants_id_seq", 2),
        ]:
            await s.execute(text(f"SELECT setval('{seq}', {last}, true)"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_cancel_cross_tenant_is_error(session):
    out = await WRITE_TOOLS["dlw_cancel_task"].run(
        session, _principal(1), task_id=str(TASK_T2))
    assert out == {"error": "task not found"}
    # tenant-2 task untouched
    from dlw.db.models.task import DownloadTask
    t2 = await session.get(DownloadTask, TASK_T2)
    assert t2.status == "running"


async def test_cancel_owned_flips_to_cancelling(session):
    out = await WRITE_TOOLS["dlw_cancel_task"].run(
        session, _principal(1), task_id=str(TASK_T1))
    assert out["status"] == "cancelling"
    await session.commit()
    from dlw.db.models.task import DownloadTask
    t1 = await session.get(DownloadTask, TASK_T1)
    assert t1.status == "cancelling"


async def test_create_happy_with_mocked_hf(session, monkeypatch):
    from dlw.services.hf_metadata import RepoFile

    async def fake(*a, **k):
        return [RepoFile(path="model.safetensors", size=1024, sha256="a" * 64)]

    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)
    out = await WRITE_TOOLS["dlw_create_task"].run(
        session, _principal(1), repo_id="org/new", revision="b" * 40,
        storage_id=1)
    assert "task_id" in out
    assert out["repo_id"] == "org/new"
    await session.commit()


async def test_create_quota_exceeded(session, monkeypatch):
    from dlw.services.quota import QuotaExceeded

    async def boom(*a, **k):
        raise QuotaExceeded("concurrent")

    monkeypatch.setattr("dlw.ai.write_tools.check_quota_for_new_task", boom)
    out = await WRITE_TOOLS["dlw_create_task"].run(
        session, _principal(1), repo_id="org/x", revision="c" * 40,
        storage_id=1)
    assert out["error"] == "quota_exceeded"


# ---------------------------------------------------------------------------
# Smoothing: default revision + auto-pick storage
# ---------------------------------------------------------------------------

async def test_create_default_revision_is_main(session, monkeypatch):
    from dlw.services.hf_metadata import RepoFile
    captured = {}

    async def fake(repo_id, revision, *, hf_endpoint, hf_token):
        captured["revision"] = revision
        return [RepoFile(path="x.bin", size=10, sha256="d" * 64)]

    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)
    await WRITE_TOOLS["dlw_create_task"].run(
        session, _principal(1), repo_id="org/defrev", storage_id=1)
    await session.commit()
    assert captured["revision"] == "main"


async def test_create_auto_picks_default_storage(session, monkeypatch):
    """When storage_id is omitted and an is_default=true storage exists,
    the tool picks it instead of erroring."""
    from dlw.db.models.storage import StorageBackend
    from dlw.services.hf_metadata import RepoFile

    s_default = StorageBackend(
        id=99, tenant_id=1, name="default-s3", backend_type="s3",
        config_encrypted=b"", is_default=True)
    session.add(s_default)
    await session.flush()

    async def fake(*a, **k):
        return [RepoFile(path="x.bin", size=10, sha256="e" * 64)]

    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)
    out = await WRITE_TOOLS["dlw_create_task"].run(
        session, _principal(1), repo_id="org/autostorage")
    assert "task_id" in out
    await session.rollback()


# ---------------------------------------------------------------------------
# dlw_delete_task
# ---------------------------------------------------------------------------

async def test_delete_in_progress_returns_not_terminal(session):
    out = await WRITE_TOOLS["dlw_delete_task"].run(
        session, _principal(1), task_id=str(TASK_T1))
    assert out["error"] == "task_not_terminal"


async def test_delete_cross_tenant_is_error(session):
    out = await WRITE_TOOLS["dlw_delete_task"].run(
        session, _principal(1), task_id=str(TASK_T2))
    assert out == {"error": "task not found"}


async def test_delete_terminal_task_removes_row(session):
    """Mark a fresh task succeeded then delete it via the tool."""
    import uuid as _u
    from dlw.db.models.task import DownloadTask
    new_tid = _u.uuid4()
    session.add(DownloadTask(
        id=new_tid, tenant_id=1, project_id=1, owner_user_id=1, storage_id=1,
        repo_id="org/term", revision="0" * 40,
        path_template="hf/{model}/{revision}/{file}", status="succeeded"))
    await session.commit()
    out = await WRITE_TOOLS["dlw_delete_task"].run(
        session, _principal(1), task_id=str(new_tid))
    assert out["deleted"] is True
    await session.commit()
    assert await session.get(DownloadTask, new_tid) is None


# ---------------------------------------------------------------------------
# dlw_retry_task
# ---------------------------------------------------------------------------

async def test_retry_creates_new_task_with_same_params(session, monkeypatch):
    from dlw.services.hf_metadata import RepoFile
    captured = {}

    async def fake(repo_id, revision, *, hf_endpoint, hf_token):
        captured["repo_id"] = repo_id
        captured["revision"] = revision
        return [RepoFile(path="x.bin", size=10, sha256="f" * 64)]

    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)
    out = await WRITE_TOOLS["dlw_retry_task"].run(
        session, _principal(1), task_id=str(TASK_T1))
    assert "task_id" in out
    # New task gets a fresh UUID, not the original
    assert out["task_id"] != str(TASK_T1)
    assert captured["repo_id"] == "org/m1"
    await session.rollback()


async def test_retry_cross_tenant_is_error(session):
    out = await WRITE_TOOLS["dlw_retry_task"].run(
        session, _principal(1), task_id=str(TASK_T2))
    assert out == {"error": "task not found"}


# ---------------------------------------------------------------------------
# dlw_create_local_user
# ---------------------------------------------------------------------------

async def test_create_local_user_admin_only(session):
    out = await WRITE_TOOLS["dlw_create_local_user"].run(
        session, _principal(1), username="x", password="pw12345678",
        tenant_id=1, role="tenant_viewer")
    # principal is tenant_admin, NOT system_admin → rejected
    assert "error" in out


async def test_create_local_user_system_admin_succeeds(session):
    admin = Principal(user_id=1, tenant_id=1, role="system_admin",
                      project_ids=())
    out = await WRITE_TOOLS["dlw_create_local_user"].run(
        session, admin, username="new_local_u1", password="pw12345678",
        tenant_id=1, role="tenant_viewer")
    assert "user_id" in out
    assert out["must_change_password"] is True
    await session.rollback()


# ---------------------------------------------------------------------------
# dlw_reset_local_password
# ---------------------------------------------------------------------------

async def test_reset_password_non_admin_rejected(session):
    out = await WRITE_TOOLS["dlw_reset_local_password"].run(
        session, _principal(1), user_id=1, new_password="pw12345678")
    assert "error" in out


async def test_reset_password_no_credential_returns_error(session):
    admin = Principal(user_id=1, tenant_id=1, role="system_admin",
                      project_ids=())
    out = await WRITE_TOOLS["dlw_reset_local_password"].run(
        session, admin, user_id=9999, new_password="pw12345678")
    assert "error" in out


# ---------------------------------------------------------------------------
# dlw_upgrade_task
# ---------------------------------------------------------------------------

async def test_upgrade_requires_different_revision(session):
    out = await WRITE_TOOLS["dlw_upgrade_task"].run(
        session, _principal(1), task_id=str(TASK_T1), new_revision="0" * 40)
    assert "must differ" in out["error"]


async def test_upgrade_creates_new_task_at_new_revision(session, monkeypatch):
    from dlw.services.hf_metadata import RepoFile

    async def fake(repo_id, revision, *, hf_endpoint, hf_token):
        return [RepoFile(path="m.bin", size=10, sha256="9" * 64)]

    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)
    out = await WRITE_TOOLS["dlw_upgrade_task"].run(
        session, _principal(1), task_id=str(TASK_T1), new_revision="v2.0")
    assert "task_id" in out
    assert out["task_id"] != str(TASK_T1)
    await session.rollback()


async def test_upgrade_cross_tenant_not_found(session):
    out = await WRITE_TOOLS["dlw_upgrade_task"].run(
        session, _principal(1), task_id=str(TASK_T2), new_revision="v2")
    assert out == {"error": "task not found"}


# ---------------------------------------------------------------------------
# dlw_patch_task
# ---------------------------------------------------------------------------

async def test_patch_priority_via_tool(session):
    out = await WRITE_TOOLS["dlw_patch_task"].run(
        session, _principal(1), task_id=str(TASK_T1), priority=7)
    assert out["priority"] == 7
    await session.rollback()


async def test_patch_strategy_via_tool(session):
    out = await WRITE_TOOLS["dlw_patch_task"].run(
        session, _principal(1), task_id=str(TASK_T1),
        source_strategy="pin_modelscope")
    assert out["source_strategy"] == "pin_modelscope"
    await session.rollback()


async def test_patch_empty_returns_error(session):
    out = await WRITE_TOOLS["dlw_patch_task"].run(
        session, _principal(1), task_id=str(TASK_T1))
    assert "empty" in out["error"]


async def test_patch_invalid_strategy_returns_error(session):
    out = await WRITE_TOOLS["dlw_patch_task"].run(
        session, _principal(1), task_id=str(TASK_T1),
        source_strategy="evil")
    assert out["error"] == "invalid_patch"


async def test_patch_cross_tenant_not_found(session):
    out = await WRITE_TOOLS["dlw_patch_task"].run(
        session, _principal(1), task_id=str(TASK_T2), priority=2)
    assert out == {"error": "task not found"}


# ---------------------------------------------------------------------------
# dlw_set_tenant_quota
# ---------------------------------------------------------------------------

async def test_set_tenant_quota_non_admin_rejected(session):
    out = await WRITE_TOOLS["dlw_set_tenant_quota"].run(
        session, _principal(1), tenant_id=1, quota_concurrent=99)
    assert "error" in out


async def test_set_tenant_quota_system_admin_updates(session):
    admin = Principal(user_id=1, tenant_id=1, role="system_admin",
                      project_ids=())
    out = await WRITE_TOOLS["dlw_set_tenant_quota"].run(
        session, admin, tenant_id=1, quota_concurrent=42)
    assert out["quota_concurrent"] == 42
    await session.rollback()


async def test_set_tenant_quota_nonexistent_tenant(session):
    admin = Principal(user_id=1, tenant_id=1, role="system_admin",
                      project_ids=())
    out = await WRITE_TOOLS["dlw_set_tenant_quota"].run(
        session, admin, tenant_id=9999, quota_bytes_month=1)
    assert out["error"] == "tenant not found"


async def test_set_tenant_quota_negative_value(session):
    admin = Principal(user_id=1, tenant_id=1, role="system_admin",
                      project_ids=())
    out = await WRITE_TOOLS["dlw_set_tenant_quota"].run(
        session, admin, tenant_id=1, quota_concurrent=-5)
    assert out["error"] == "invalid_quota"


# ---------------------------------------------------------------------------
# v2.1 SP6 — dlw_create_replication
# ---------------------------------------------------------------------------

async def test_create_replication_non_admin_rejected(session):
    out = await WRITE_TOOLS["dlw_create_replication"].run(
        session, _principal(1),
        source_object_id=1, target_storage_id=2)
    assert out == {"error": "system_admin role required"}


async def test_create_replication_happy_path(session):
    """system_admin queues a replication job; service-layer validation
    happens naturally — invalid source / target propagates as the same
    error code shape as REST."""
    import hashlib
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.storage_object import StorageObject
    # Seed: tenant 1 owns storage 1 (already in bootstrap); add storage 3
    # also for tenant 1 so cross-storage replication is possible.
    s3 = StorageBackend(tenant_id=1, name="s3-tenant1-extra",
                        backend_type="s3", config_encrypted=b"")
    session.add(s3)
    await session.flush()
    payload = b"replicate-me"
    src_obj = StorageObject(
        tenant_id=1, storage_id=1, storage_key="k/replica",
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload), refcount=1)
    session.add(src_obj)
    await session.flush()

    admin = Principal(user_id=1, tenant_id=1, role="system_admin",
                      project_ids=())
    out = await WRITE_TOOLS["dlw_create_replication"].run(
        session, admin,
        source_object_id=src_obj.id, target_storage_id=s3.id)
    assert "job_id" in out, out
    assert out["status"] == "pending"
    assert out["source_object_id"] == src_obj.id
    assert out["target_storage_id"] == s3.id
    await session.rollback()


async def test_create_replication_unknown_target(session):
    admin = Principal(user_id=1, tenant_id=1, role="system_admin",
                      project_ids=())
    out = await WRITE_TOOLS["dlw_create_replication"].run(
        session, admin, source_object_id=1, target_storage_id=99999)
    # source_object_id=1 likely doesn't exist either; either error is fine
    assert "error" in out
