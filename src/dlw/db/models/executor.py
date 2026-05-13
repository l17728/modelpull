"""Executor model (architecture §4.3)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, SmallInteger, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class Executor(Base):
    __tablename__ = "executors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=True
    )
    host_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_executor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cert_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Phase 2 W1 fence token: monotonic counter bumped atomically on every /join.
    # First /join sets epoch=1; re-join (post-crash or after EPOCH_MISMATCH) bumps it.
    # All non-join executor requests carry X-Executor-Epoch which controller verifies.
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    health_score: Mapped[int] = mapped_column(SmallInteger, default=100, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_heartbeat_failures: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    consecutive_task_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    degraded_failure_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    nic_speed_gbps: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    disk_free_gb: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    disk_total_gb: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    parts_dir_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
