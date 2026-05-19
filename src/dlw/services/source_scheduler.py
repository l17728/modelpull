"""Task scheduling-phase source planner (Phase 3 SP2; doc §1.6/§1.8).
Caller commits."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.source import SubtaskChunk
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.source_combo import assign_files_lpt, solve_optimal_combo

_CHUNK_BYTES = 64 * 1024 * 1024   # source-routing chunk granularity


def _strategy_filter(enabled: list[str], strategy: str,
                     blacklist: list[str]) -> tuple[list[str], str | None]:
    """Apply task.source_strategy + task.source_blacklist (spec ruling 6e).
    Returns (allowed_ids, pinned_or_None). pinned!=None means an explicit
    single-source pin that must be honored (pause if unreachable)."""
    allowed = [s for s in enabled if s not in blacklist]
    if strategy == "auto_balance" or not strategy:
        return allowed, None
    if strategy == "fastest_only":
        return allowed, None
    if strategy.startswith("pin_"):
        pin = strategy.removeprefix("pin_")
        return ([pin] if pin in allowed else []), pin
    if strategy.startswith("list:"):
        wanted = [x.strip() for x in strategy.removeprefix("list:").split(",")]
        return [s for s in allowed if s in wanted], None
    return allowed, None


async def plan_task_sources(
    session: AsyncSession, task: DownloadTask, *,
    registry: Any, resolver: Any, speeds: dict[str, float],
    chunk_min_mb: int, overhead_pct: float = 2.0,
) -> None:
    # SP3 (banner 7a): only `pending` subtasks need source planning;
    # `inherit` subs were materialised by diff_and_dedup. A fully-inherited
    # task has zero pending subs — return early so the pinned / no-sha /
    # no-speed pause gates below never fire (the task then flows
    # scheduling -> downloading and its inherit subs complete to succeeded).
    subs = (await session.execute(select(FileSubTask).where(
        FileSubTask.task_id == task.id,
        FileSubTask.status == "pending"))).scalars().all()
    if not subs:
        return
    allowed, pinned = _strategy_filter(
        registry.enabled_ids(), task.source_strategy or "auto_balance",
        list(task.source_blacklist or []))

    manifests: dict[str, Any] = {}
    for sid in allowed:
        drv = registry.get(sid)
        src_repo = resolver.resolve(sid, task.repo_id)
        if src_repo is None:
            continue
        m = await drv.resolve(src_repo, task.revision)
        if m is not None:
            manifests[sid] = (drv, m)

    if pinned is not None and pinned not in manifests:
        task.status = "paused_external"
        task.error_message = "pinned_source_unavailable"
        return

    hf_ok = "huggingface" in manifests
    if not hf_ok and not task.trust_non_hf_sha256:
        task.status = "paused_external"
        task.error_message = "no_sha256_authority"
        return

    candidates = {sid: speeds[sid] for sid in manifests
                  if sid in speeds and speeds[sid] > 0}
    if not candidates:
        task.status = "paused_external"
        task.error_message = "no_source_speed"
        return
    sizes = {x.filename: (x.file_size or 0) for x in subs}
    combo = solve_optimal_combo(candidates, sizes, overhead_pct=overhead_pct)
    combo_speeds = {s: candidates[s] for s in combo}

    assign = assign_files_lpt(sizes, combo_speeds)
    hf_files: set[str] = set()
    if "huggingface" in manifests:
        hf_files = {f.filename for f in manifests["huggingface"][1].files}
    chunk_min = chunk_min_mb * 1024 * 1024
    for sub in subs:
        no_hf_authority = (sub.expected_sha256 is None
                           or sub.filename not in hf_files)
        if no_hf_authority and not task.trust_non_hf_sha256:
            if "huggingface" not in manifests:
                task.status = "paused_external"
                task.error_message = "no_sha256_authority"
                return
            sub.source_id = "huggingface"
            continue
        sid = assign[sub.filename]
        sub.source_id = sid
        covering = [s for s in candidates
                    if any(f.filename == sub.filename
                           for f in manifests[s][1].files)]
        if (sub.file_size or 0) >= chunk_min and len(covering) >= 2:
            sub.is_chunked = True
            cov_speeds = {s: candidates[s] for s in covering}
            await _split_chunks(session, sub, sub.file_size, covering,
                                cov_speeds)


async def run_scheduling_tick(session, registry, resolver, settings) -> None:
    """Pick `pending` tasks; controller-side probe each source; plan; move
    to claimable (spec ruling 6d)."""
    from dlw.db.models.source import SourceSpeedSample
    from dlw.services.source_speed import (
        fuse_ewma,
        pick_probe_size_bytes,
        probe_source_speed,
    )
    pend = (await session.execute(select(DownloadTask).where(
        DownloadTask.status == "pending").limit(20))).scalars().all()
    probe_bytes = pick_probe_size_bytes(probe_size_mb=settings.probe_size_mb)
    for task in pend:
        task.status = "scheduling"
        from dlw.services.incremental import diff_and_dedup
        await diff_and_dedup(session, task)
        speeds: dict[str, float] = {}
        for sid in registry.enabled_ids():
            drv = registry.get(sid)
            src_repo = resolver.resolve(sid, task.repo_id)
            live = 0.0
            if src_repo is not None:
                try:
                    m = await drv.resolve(src_repo, task.revision)
                except Exception:
                    m = None
                if m is not None and m.files:
                    probe_f = min(m.files, key=lambda f: f.size or 1 << 62)
                    live = await probe_source_speed(
                        drv, probe_f, probe_bytes=probe_bytes,
                        timeout_s=settings.probe_timeout_s,
                        hf_token=settings.hf_token)
            hist = await session.scalar(
                select(SourceSpeedSample.bytes_per_sec)
                .where(SourceSpeedSample.source_id == sid)
                .order_by(SourceSpeedSample.measured_at.desc()).limit(1))
            fused = fuse_ewma(live=live, hist=float(hist) if hist else None,
                              hist_weight=settings.probe_history_weight)
            if live > 0:
                session.add(SourceSpeedSample(
                    executor_id="controller", source_id=sid,
                    bytes_per_sec=live, sample_size=probe_bytes,
                    is_active_probe=True))
            speeds[sid] = fused if fused > 0 else 0.0
        await plan_task_sources(
            session, task, registry=registry, resolver=resolver,
            speeds=speeds, chunk_min_mb=settings.chunk_level_min_file_mb,
            overhead_pct=settings.combo_overhead_per_source_pct)
        if task.status == "scheduling":
            task.status = "downloading"


async def run_rebalance_tick(session, settings) -> None:
    """Reassign a degraded (blacklisted) source's PENDING chunks to a
    healthy sibling source on the same subtask (in-flight untouched)."""
    from sqlalchemy import text

    from dlw.services.source_blacklist import active_blacklisted_sources
    bad = await active_blacklisted_sources(session)
    for src in bad:
        await session.execute(text(
            "UPDATE subtask_chunks c SET source_id = ("
            "  SELECT source_id FROM subtask_chunks d "
            "  WHERE d.subtask_id=c.subtask_id AND d.source_id!=:bad "
            "  LIMIT 1) "
            "WHERE c.source_id=:bad AND c.status='pending' "
            "AND EXISTS (SELECT 1 FROM subtask_chunks e "
            "  WHERE e.subtask_id=c.subtask_id AND e.source_id!=:bad)"
        ), {"bad": src})


async def _split_chunks(
    session: AsyncSession, sub: FileSubTask, size: int,
    sources: list[str], speeds: dict[str, float],
) -> None:
    total = sum(speeds[s] for s in sources) or 1.0
    offset = 0
    idx = 0
    for i, sid in enumerate(sources):
        if i == len(sources) - 1:
            length = size - offset
        else:
            portion = int(size * speeds[sid] / total)
            length = max(_CHUNK_BYTES,
                         (portion // _CHUNK_BYTES) * _CHUNK_BYTES)
            length = min(length, size - offset)
        if length <= 0:
            continue
        session.add(SubtaskChunk(
            subtask_id=sub.id, chunk_index=idx, byte_start=offset,
            byte_end=offset + length - 1, source_id=sid, status="pending"))
        offset += length
        idx += 1
