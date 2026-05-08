"""Tests for dlw-executor CLI."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.slow
def test_cli_help_exits_0() -> None:
    """`dlw-executor --help` should print usage and exit cleanly."""
    r = subprocess.run(
        [sys.executable, "-m", "dlw.executor.cli", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0
    assert "executor" in r.stdout.lower()


@pytest.mark.slow
def test_cli_missing_required_env_exits_nonzero() -> None:
    """Without DLW_EXECUTOR_ID/BEARER_TOKEN, CLI should fail at config validation."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("DLW_EXECUTOR_")}
    r = subprocess.run(
        [sys.executable, "-m", "dlw.executor.cli"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert "id" in combined.lower() or "bearer_token" in combined.lower()
