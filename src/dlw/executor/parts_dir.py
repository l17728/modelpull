"""Helpers for the .parts/ staging area used by DirectOffsetDownloader."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path


def parts_dir_for(root: str, subtask_id: uuid.UUID) -> Path:
    """Return ${root}/${subtask_id_hex}; does NOT create."""
    return Path(root) / subtask_id.hex


def cleanup_parts_dir(root: str, subtask_id: uuid.UUID) -> None:
    """rmtree the per-subtask dir if it exists; ignore errors."""
    p = parts_dir_for(root, subtask_id)
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


def total_parts_bytes(root: str) -> int:
    """Sum of file sizes under ${root}/, recursive; 0 if root missing."""
    base = Path(root)
    if not base.exists():
        return 0
    total = 0
    for child in base.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def startup_gc(root: str, active_subtask_ids: set[uuid.UUID]) -> int:
    """Scan ${root}/* directories; rmtree any whose hex name is NOT in
    active_subtask_ids. Returns count of removed dirs.

    Called by runner bootstrap before the poll loop starts. W2b1 callers
    pass active_subtask_ids=set() (remove everything) — the parameter
    exists for a Phase-3 multipart-resume world.
    """
    base = Path(root)
    if not base.exists():
        return 0
    active_hex = {sub_id.hex for sub_id in active_subtask_ids}
    removed = 0
    for child in base.iterdir():
        if not child.is_dir():
            continue
        if child.name in active_hex:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    return removed
