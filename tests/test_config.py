"""Tests for the controller Settings class (dlw.config)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dlw.config import Settings


def test_settings_has_hf_proxy_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLW_HF_PROXY_TIMEOUT_SECONDS", raising=False)
    s = Settings()
    assert s.hf_proxy_timeout_seconds == 300


def test_settings_hf_proxy_timeout_rejects_below_min() -> None:
    with pytest.raises(ValidationError):
        Settings(hf_proxy_timeout_seconds=5)


def test_settings_hf_proxy_timeout_rejects_above_max() -> None:
    with pytest.raises(ValidationError):
        Settings(hf_proxy_timeout_seconds=4000)


def test_settings_hf_proxy_timeout_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLW_HF_PROXY_TIMEOUT_SECONDS", "600")
    s = Settings()
    assert s.hf_proxy_timeout_seconds == 600


def test_settings_has_active_lock_id_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLW_ACTIVE_LOCK_ID", raising=False)
    s = Settings()
    assert s.active_lock_id == 0x444C5743_414B5631


def test_settings_active_lock_id_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLW_ACTIVE_LOCK_ID", "12345")
    s = Settings()
    assert s.active_lock_id == 12345


def test_settings_active_lock_id_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        Settings(active_lock_id=0)


def test_settings_active_lock_id_rejects_above_pg_bigint_max() -> None:
    with pytest.raises(ValidationError):
        Settings(active_lock_id=9_223_372_036_854_775_808)


def test_settings_has_leader_poll_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLW_LEADER_POLL_INTERVAL_SECONDS", raising=False)
    s = Settings()
    assert s.leader_poll_interval_seconds == 5.0


def test_settings_leader_poll_interval_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLW_LEADER_POLL_INTERVAL_SECONDS", "10.0")
    s = Settings()
    assert s.leader_poll_interval_seconds == 10.0


def test_settings_leader_poll_interval_rejects_below_min() -> None:
    with pytest.raises(ValidationError):
        Settings(leader_poll_interval_seconds=0.1)


def test_settings_leader_poll_interval_rejects_above_max() -> None:
    with pytest.raises(ValidationError):
        Settings(leader_poll_interval_seconds=99.0)


def test_sp1_auth_settings_defaults(monkeypatch):
    from dlw.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.auth_dev_mode is False
    assert s.system_jwt_secret == "dev-system-jwt-change-me"
    assert s.system_admin_token == ""
    assert s.oidc_issuer == ""
    assert s.oidc_redirect_url.endswith("/api/v1/auth/callback")
    assert s.auth_tenant_rules_json == "[]"
    assert not hasattr(s, "bearer_token")
    get_settings.cache_clear()


def test_sp1_auth_settings_env_override(monkeypatch):
    from dlw.config import get_settings
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", "s3cr3t")
    monkeypatch.setenv("DLW_SYSTEM_ADMIN_TOKEN", "svc-tok")
    get_settings.cache_clear()
    s = get_settings()
    assert s.auth_dev_mode is True
    assert s.system_jwt_secret == "s3cr3t"
    assert s.system_admin_token == "svc-tok"
    get_settings.cache_clear()


def test_sp2_source_settings_defaults():
    from dlw.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.sources_yaml_path == "config/sources.yaml"
    assert s.resolver_rules_path == "config/resolver-rules.yaml"
    assert s.probe_size_mb == 32
    assert s.probe_timeout_s == 8.0
    assert s.chunk_level_min_file_mb == 100
    assert s.speed_ewma_alpha == 0.3
    assert s.sha_mismatch_blacklist_hours == 24
    assert s.rebalance_interval_seconds == 60.0
    get_settings.cache_clear()


def test_sp3_gc_settings_defaults():
    from dlw.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.gc_interval_seconds == 60.0
    assert s.gc_grace_seconds == 3600
    get_settings.cache_clear()


def test_sp3_inherit_is_valid_subtask_status():
    from tools.lint_invariants import VALID_SUBTASK_STATUS
    assert "inherit" in VALID_SUBTASK_STATUS


def test_sp3_taskcreate_upgrade_from_revision_optional():
    from dlw.schemas.task import TaskCreate
    t = TaskCreate(repo_id="o/r", revision="a" * 40, storage_id=1)
    assert t.upgrade_from_revision is None
    t2 = TaskCreate(repo_id="o/r", revision="a" * 40, storage_id=1,
                    upgrade_from_revision="b" * 40)
    assert t2.upgrade_from_revision == "b" * 40


def test_sp3_storageconfig_backend_type_default():
    from dlw.schemas.storage import StorageConfig
    c = StorageConfig(bucket="b")
    assert c.backend_type == "s3" and c.base_path is None
    c2 = StorageConfig(bucket="b", backend_type="local", base_path="/srv")
    assert c2.backend_type == "local" and c2.base_path == "/srv"


@pytest.mark.slow
async def test_sp3_create_task_threads_upgrade_from_revision(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from dlw.db.base import Base
    from dlw.schemas.task import TaskCreate
    from dlw.services.task_service import create_task
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    import dlw.db.models  # noqa: F401
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    fac = async_sessionmaker(engine, expire_on_commit=False)
    async with fac() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d"),
            User(id=1, tenant_id=1, oidc_subject="u", email="e",
                 role="tenant_operator"),
            StorageBackend(id=1, tenant_id=1, name="s", backend_type="s3",
                           config_encrypted=b"")])
        await s.flush()
        task = await create_task(
            s, TaskCreate(repo_id="o/r", revision="n" * 40, storage_id=1,
                          upgrade_from_revision="o" * 40),
            owner_user_id=1, tenant_id=1, project_id=1,
            hf_endpoint="https://hf", hf_token=None)
        assert task.upgrade_from_revision == "o" * 40
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
