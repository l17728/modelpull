"""StorageBackend model (architecture §4.4)."""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class StorageBackend(Base):
    __tablename__ = "storage_backends"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    backend_type: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # config_encrypted: envelope encryption (KMS); Phase 1 accepts placeholder bytes
    config_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
