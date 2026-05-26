"""v2.1 Sprint 8 — Optimizer tests.

Covers the acceptance criteria from the sprint plan:
  - hand-verifiable result on synthetic input
  - 100 × 10 × 5 in < 1s
  - single executor → linear scan
  - single source → identical to single-executor case
  - empty / infeasible inputs degrade safely"""
from __future__ import annotations

import time

from dlw.services.optimizer import Capacity, Chunk, solve


def _cap(ex: str, src: str, ft: str, bps: float) -> Capacity:
    return Capacity(executor_id=ex, source_id=src, file_type=ft,
                    bytes_per_sec=bps)


# ---------------------------------------------------------------------------
# Degenerate paths

def test_empty_chunks_returns_zero_makespan():
    r = solve([], ["ex-A"], ["hf"], [_cap("ex-A", "hf", "other", 100)])
    assert r.makespan_seconds == 0.0
    assert r.assignments == {}


def test_no_executors_returns_infeasible():
    r = solve([Chunk("c1", 100)], [], ["hf"],
              [_cap("ex-A", "hf", "other", 100)])
    assert r.unassigned == ["c1"]
    assert r.makespan_seconds == float("inf")


def test_no_sources_returns_infeasible():
    r = solve([Chunk("c1", 100)], ["ex-A"], [], [])
    assert r.unassigned == ["c1"]


def test_no_capacity_returns_infeasible():
    """All chunks need file_type=safetensors; capacity only covers 'other'."""
    r = solve([Chunk("c1", 100, file_type="safetensors")],
              ["ex-A"], ["hf"],
              [_cap("ex-A", "hf", "other", 100)])
    assert r.unassigned == ["c1"]


# ---------------------------------------------------------------------------
# Hand-verifiable: 2 chunks of equal size, 2 executors of equal capacity
#                  → must end up one chunk per executor

def test_two_chunks_two_executors_balanced():
    chunks = [Chunk("c1", 1000), Chunk("c2", 1000)]
    caps = [_cap("ex-A", "hf", "other", 100),
            _cap("ex-B", "hf", "other", 100)]
    r = solve(chunks, ["ex-A", "ex-B"], ["hf"], caps)
    # Each chunk takes 10s; placed one per executor → makespan = 10s
    assert r.makespan_seconds == 10.0
    placed_executors = {ex for ex, _ in r.assignments.values()}
    assert placed_executors == {"ex-A", "ex-B"}


# ---------------------------------------------------------------------------
# Hand-verifiable: a big chunk + a tiny one, two equal executors
#                  → big goes to one, tiny to the other (LPT)

def test_lpt_orders_by_size_descending():
    chunks = [Chunk("tiny", 100), Chunk("huge", 10000)]
    caps = [_cap("ex-A", "hf", "other", 100),
            _cap("ex-B", "hf", "other", 100)]
    r = solve(chunks, ["ex-A", "ex-B"], ["hf"], caps)
    huge_ex, _ = r.assignments["huge"]
    tiny_ex, _ = r.assignments["tiny"]
    assert huge_ex != tiny_ex
    # huge=100s on its executor; tiny=1s on the other → makespan=100s
    assert r.makespan_seconds == 100.0


# ---------------------------------------------------------------------------
# Single executor → all chunks land on it

def test_single_executor_linear_scan():
    chunks = [Chunk(f"c{i}", 100) for i in range(5)]
    r = solve(chunks, ["sole"], ["hf"],
              [_cap("sole", "hf", "other", 100)])
    # 5 chunks × 1s each = 5s on the only executor
    assert all(ex == "sole" for ex, _ in r.assignments.values())
    assert r.makespan_seconds == 5.0


# Single source → identical to single-executor logic at the source level

def test_single_source_no_choice():
    chunks = [Chunk(f"c{i}", 100) for i in range(4)]
    caps = [_cap(ex, "only-src", "other", 100)
            for ex in ["ex-A", "ex-B"]]
    r = solve(chunks, ["ex-A", "ex-B"], ["only-src"], caps)
    assert all(src == "only-src" for _, src in r.assignments.values())
    # 4 chunks × 1s, 2 executors → 2s makespan (perfect split)
    assert r.makespan_seconds == 2.0


# ---------------------------------------------------------------------------
# Optimizer picks the FASTER (executor, source) when available

def test_optimizer_prefers_higher_capacity():
    chunks = [Chunk("c1", 1000, "safetensors")]
    caps = [
        _cap("slow", "hf", "safetensors", 100),      # 10s
        _cap("fast", "mirror", "safetensors", 1000), # 1s
    ]
    r = solve(chunks, ["slow", "fast"], ["hf", "mirror"], caps)
    assert r.assignments["c1"] == ("fast", "mirror")
    assert r.makespan_seconds == 1.0


# ---------------------------------------------------------------------------
# file_type matters — a (ex, src) with capacity for ONE type but not the
# requested one is treated as zero

def test_file_type_isolation():
    chunks = [Chunk("c1", 100, "json")]
    caps = [
        _cap("ex-A", "hf", "safetensors", 9999),   # wrong type
        _cap("ex-A", "hf", "json", 100),           # correct type
    ]
    r = solve(chunks, ["ex-A"], ["hf"], caps)
    assert r.assignments["c1"] == ("ex-A", "hf")
    # 100 bytes at 100 bps == 1s, NOT 0.01s from the wrong-type row
    assert r.makespan_seconds == 1.0


# ---------------------------------------------------------------------------
# Acceptance criterion: 100 chunks × 10 executors × 5 sources in < 1s

def test_performance_100x10x5_under_one_second():
    import random
    rng = random.Random(42)
    chunks = [Chunk(f"c{i}", rng.randint(1_000_000, 100_000_000))
              for i in range(100)]
    executors = [f"ex-{i}" for i in range(10)]
    sources = [f"src-{i}" for i in range(5)]
    caps = [
        _cap(ex, src, "other", rng.uniform(10_000_000, 200_000_000))
        for ex in executors for src in sources
    ]
    t0 = time.perf_counter()
    r = solve(chunks, executors, sources, caps)
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"100x10x5 took {elapsed:.3f}s (budget 1s)"
    assert len(r.assignments) == 100
    assert r.unassigned == []


# ---------------------------------------------------------------------------
# Polish should not WORSEN the makespan (it might no-op)

def test_polish_never_worsens():
    chunks = [Chunk(f"c{i}", 100 * (i + 1)) for i in range(8)]
    caps = [
        _cap(ex, "hf", "other", 100)
        for ex in ["ex-A", "ex-B", "ex-C"]
    ]
    r_polished = solve(chunks, ["ex-A", "ex-B", "ex-C"], ["hf"], caps,
                       polish=True)
    r_raw = solve(chunks, ["ex-A", "ex-B", "ex-C"], ["hf"], caps,
                  polish=False)
    assert r_polished.makespan_seconds <= r_raw.makespan_seconds + 1e-9
