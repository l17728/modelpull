"""v2.1 Sprint 7 — Per-chunk throughput sample.

One row per completed chunk; fed to the Sprint 8 LP solver. Schema mirrors
the alembic migration e4f5a6b7c8d9."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class ChunkThroughputSample(Base):
    __tablename__ = "chunk_throughput_sample"
    __table_args__ = (
        CheckConstraint("duration_ms > 0", name="ck_cts_duration_positive"),
        CheckConstraint("bytes_transferred >= 0",
                         name="ck_cts_bytes_nonneg"),
        Index("ix_cts_recorded_at", "recorded_at"),
        Index("ix_cts_es_lookup", "executor_id", "source_id",
              "file_type", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    executor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    file_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="other", server_default="other")
    bytes_transferred: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=True)
