"""Self-test: the lint correctly flags a known-bad fixture."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
import lint_no_direct_status_write as linter   # noqa: E402


def test_lint_flags_direct_status_assignment() -> None:
    fixture = (
        Path(__file__).resolve().parent
        / "fixtures" / "bad_executor_status_write.py"
    )
    hits = linter.scan_file(fixture)
    assert any("status" in text for _, text in hits), \
        f"expected lint to flag fixture, got {hits=}"
