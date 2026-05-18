"""Multi-source models (Phase 3 SP2; doc 06 §1.4/§1.7)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class SubtaskChunk(Base):
    __tablename__ = "subtask_chunks"
    __table_args__ = (UniqueConstraint("subtask_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subtask_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("file_subtasks.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    sha256_partial: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bytes_done: Mapped[int] = mapped_column(BigInteger, default=0,
                                            nullable=False)


class SourceSpeedSample(Base):
    __tablename__ = "source_speed_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    executor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    bytes_per_sec: Mapped[float] = mapped_column(Float, nullable=False)
    sample_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_active_probe: Mapped[bool] = mapped_column(default=False,
                                                  nullable=False)


class SourceBlacklist(Base):
    __tablename__ = "source_blacklist"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    repo_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
