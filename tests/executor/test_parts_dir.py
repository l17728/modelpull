"""Tests for parts_dir helpers (Phase 2 W2b1)."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from dlw.executor.parts_dir import (
    cleanup_parts_dir,
    parts_dir_for,
    startup_gc,
    total_parts_bytes,
)


def test_parts_dir_for_returns_hex_subdir(tmp_path) -> None:
    sub_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    p = parts_dir_for(str(tmp_path), sub_id)
    assert p == tmp_path / "12345678123456781234567812345678"


def test_cleanup_parts_dir_removes_existing_and_ignores_missing(tmp_path) -> None:
    sub_id = uuid.uuid4()
    p = parts_dir_for(str(tmp_path), sub_id)
    p.mkdir(parents=True)
    (p / "0.bin").write_bytes(b"x" * 100)
    cleanup_parts_dir(str(tmp_path), sub_id)
    assert not p.exists()
    # Idempotent on missing dir:
    cleanup_parts_dir(str(tmp_path), sub_id)  # no exception


def test_total_parts_bytes_sums_all_files(tmp_path) -> None:
    (tmp_path / "abc").mkdir()
    (tmp_path / "abc" / "0.bin").write_bytes(b"x" * 100)
    (tmp_path / "abc" / "1.bin").write_bytes(b"y" * 50)
    (tmp_path / "def").mkdir()
    (tmp_path / "def" / "0.bin").write_bytes(b"z" * 25)
    assert total_parts_bytes(str(tmp_path)) == 175


def test_total_parts_bytes_returns_zero_for_missing_root(tmp_path) -> None:
    assert total_parts_bytes(str(tmp_path / "nope")) == 0


def test_startup_gc_removes_dirs_not_in_active_set(tmp_path) -> None:
    keep_id = uuid.uuid4()
    reap_id = uuid.uuid4()
    keep_dir = parts_dir_for(str(tmp_path), keep_id)
    reap_dir = parts_dir_for(str(tmp_path), reap_id)
    keep_dir.mkdir(parents=True)
    reap_dir.mkdir(parents=True)
    (keep_dir / "0.bin").write_bytes(b"x")
    (reap_dir / "0.bin").write_bytes(b"y")

    removed = startup_gc(str(tmp_path), active_subtask_ids={keep_id})

    assert removed == 1
    assert keep_dir.exists()
    assert not reap_dir.exists()


def test_startup_gc_with_empty_active_set_removes_all(tmp_path) -> None:
    for _ in range(3):
        d = parts_dir_for(str(tmp_path), uuid.uuid4())
        d.mkdir(parents=True)
        (d / "0.bin").write_bytes(b"x")

    removed = startup_gc(str(tmp_path), active_subtask_ids=set())
    assert removed == 3
    # tmp_path itself should still exist (we only reap children, not root):
    assert tmp_path.exists()
