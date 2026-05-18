"""LPT greedy + optimal-combo (Phase 3 SP2; doc §1.6/§1.8, OR-V21-04)."""
from __future__ import annotations

from dlw.services.source_combo import assign_files_lpt, solve_optimal_combo


def test_lpt_balances_by_completion_time():
    files = {"a": 100, "b": 100, "c": 50}
    speeds = {"s1": 10.0, "s2": 5.0}
    assign = assign_files_lpt(files, speeds)
    assert set(assign.values()) <= {"s1", "s2"}
    assert assign["a"] == "s1"


def test_lpt_single_source_degenerate():
    assign = assign_files_lpt({"a": 1, "b": 2}, {"only": 7.0})
    assert assign == {"a": "only", "b": "only"}


def test_combo_excludes_slow_source_by_overhead():
    files = {"f": 1_000_000_000}
    speeds = {"fast": 1_000_000_000.0, "slow": 1.0}
    combo = solve_optimal_combo(speeds, files, overhead_pct=2.0)
    assert combo == ["fast"]


def test_combo_uses_both_when_comparable():
    files = {"a": 100, "b": 100}
    speeds = {"s1": 10.0, "s2": 10.0}
    combo = solve_optimal_combo(speeds, files, overhead_pct=2.0)
    assert set(combo) == {"s1", "s2"}
