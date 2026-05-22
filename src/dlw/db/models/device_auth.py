"""RFC 8628 device-authorization session (FU6)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class DeviceAuthSession(Base):
    __tablename__ = "device_auth_sessions"
    __table_args__ = (
        UniqueConstraint("device_code_hash", name="uq_device_code_hash"),
        UniqueConstraint("user_code", name="uq_device_user_code"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_code: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    project_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
