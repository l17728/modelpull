"""Smoke tests for the v2.1 physical_gc skeleton.

These verify the stable interface (data classes, function signatures,
NotImplemented exception type) so downstream callers can be wired up
ahead of the full implementation. When the real implementation lands,
extend these into actual behavior tests (covered in docs/v2.1-roadmap.md
§ 1.3 verification)."""
from __future__ import annotations

import pytest

from dlw.services import physical_gc
from dlw.services.physical_gc import (
    EvictionCandidate, EvictionResult, ObjectStoreDeleter,
    PhysicalGCNotImplemented,
)


def test_eviction_candidate_is_frozen_dataclass():
    c = EvictionCandidate(
        object_id=1, storage_id=1, storage_key="k", sha256="a" * 64,
        size_bytes=100, reason="tombstone")
    with pytest.raises(AttributeError):
        c.size_bytes = 200  # frozen


def test_eviction_result_carries_candidate_and_status():
    c = EvictionCandidate(
        object_id=1, storage_id=1, storage_key="k", sha256="a" * 64,
        size_bytes=100, reason="lru_pressure")
    r = EvictionResult(candidate=c, deleted=False, error="auth_failed")
    assert r.candidate.object_id == 1
    assert r.deleted is False
    assert r.error == "auth_failed"


async def test_find_tombstone_raises_not_implemented():
    with pytest.raises(PhysicalGCNotImplemented):
        await physical_gc.find_tombstone_candidates(session=None, limit=10)


async def test_find_lru_raises_not_implemented():
    with pytest.raises(PhysicalGCNotImplemented):
        await physical_gc.find_lru_eviction_candidates(
            session=None, tenant_id=1, target_free_bytes=1024)


async def test_evict_one_raises_not_implemented():
    candidate = EvictionCandidate(
        object_id=1, storage_id=1, storage_key="k", sha256="a" * 64,
        size_bytes=100, reason="tombstone")
    with pytest.raises(PhysicalGCNotImplemented):
        await physical_gc.evict_one(
            session=None, candidate=candidate, deleter=None)


async def test_gc_run_once_raises_not_implemented():
    with pytest.raises(PhysicalGCNotImplemented):
        await physical_gc.gc_run_once(session=None, tenant_id=1)


def test_object_store_deleter_is_protocol():
    """ObjectStoreDeleter is a Protocol — any class with .delete(...)
    matches structurally. Sanity check the runtime hint."""
    class FakeDeleter:
        async def delete(self, storage_id: int, key: str) -> bool:
            return True
    d: ObjectStoreDeleter = FakeDeleter()
    assert callable(d.delete)


def test_default_config_constants_exist():
    """The module exports sensible defaults that operator docs reference."""
    assert physical_gc._GC_INTERVAL_SECONDS_DEFAULT == 3600
    assert 0.5 <= physical_gc._LRU_TARGET_FRACTION_DEFAULT <= 1.0
    assert physical_gc._GC_BATCH_SIZE_DEFAULT > 0
