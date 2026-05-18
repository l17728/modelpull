"""Source/(source,repo,file) blacklist (Phase 3 SP2; doc §1.7).
Caller commits (service-layer convention)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.source import SourceBlacklist


async def blacklist_file(
    session: AsyncSession, *, source_id: str, repo_id: str,
    filename: str, hours: int, reason: str,
) -> None:
    session.add(SourceBlacklist(
        source_id=source_id, repo_id=repo_id, filename=filename,
        until=datetime.now(UTC) + timedelta(hours=hours), reason=reason))


async def is_blacklisted(
    session: AsyncSession, source_id: str, repo_id: str, filename: str
) -> bool:
    row = await session.scalar(
        select(SourceBlacklist.id).where(
            SourceBlacklist.source_id == source_id,
            SourceBlacklist.repo_id == repo_id,
            SourceBlacklist.filename == filename,
            SourceBlacklist.until > datetime.now(UTC)).limit(1))
    return row is not None


async def active_blacklisted_sources(session: AsyncSession) -> list[str]:
    rows = await session.execute(select(SourceBlacklist.source_id).where(
        SourceBlacklist.until > datetime.now(UTC)).distinct())
    return [r[0] for r in rows]
