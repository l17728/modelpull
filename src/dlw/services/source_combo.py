"""File->source assignment: size-descending greedy heuristic (NOT bounded-
optimal LPT — doc OR-V21-04) + fastest-K combo with overhead penalty."""
from __future__ import annotations


def assign_files_lpt(
    files: dict[str, int], source_speeds: dict[str, float]
) -> dict[str, str]:
    """files: {filename: size}; source_speeds: {source_id: bytes/sec}.
    Returns {filename: source_id}. Largest-first; each file to the source
    with the earliest projected completion (load+size)/speed."""
    load = {sid: 0.0 for sid in source_speeds}
    out: dict[str, str] = {}
    for fn in sorted(files, key=lambda k: -files[k]):
        size = files[fn]
        best = min(source_speeds,
                   key=lambda sid: (load[sid] + size) / source_speeds[sid])
        out[fn] = best
        load[best] += size
    return out


def _eta(files: dict[str, int], speeds: dict[str, float]) -> float:
    assign = assign_files_lpt(files, speeds)
    load = {sid: 0.0 for sid in speeds}
    for fn, sid in assign.items():
        load[sid] += files[fn]
    return max((load[sid] / speeds[sid] for sid in speeds), default=0.0)


def solve_optimal_combo(
    source_speeds: dict[str, float], files: dict[str, int],
    *, overhead_pct: float
) -> list[str]:
    ranked = sorted(source_speeds, key=lambda s: -source_speeds[s])
    best_eta = float("inf")
    best: list[str] = ranked[:1]
    for k in range(1, len(ranked) + 1):
        combo = ranked[:k]
        sub = {s: source_speeds[s] for s in combo}
        eta = _eta(files, sub) * (1 + 0.01 * overhead_pct * (k - 1))
        if eta < best_eta:
            best_eta, best = eta, combo
        elif k > 1 and eta > best_eta * 1.05:
            break
    return best
