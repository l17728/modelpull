"""AI Copilot ORM models (UI-SP4a)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer,
                        String, func)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dlw.db.base import Base


class AIConversation(Base):
    __tablename__ = "ai_conversations"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=False)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    messages: Mapped[list["AIMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan")


class AIMessage(Base):
    __tablename__ = "ai_messages"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_conversations.id", ondelete="CASCADE"),
        nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tokens_input: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    tokens_output: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    conversation: Mapped[AIConversation] = relationship(
        back_populates="messages")


class AIToolCall(Base):
    """UI-SP4b: write-tool confirmation tracking. proposed_input vs
    final_input records the invariant-40 distinction."""
    __tablename__ = "ai_tool_calls"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_messages.id", ondelete="CASCADE"), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    final_input: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requires_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending")
    confirmed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True)
    confirmation_decision: Mapped[str | None] = mapped_column(
        String(16), nullable=True)
    confirmation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class AITokenUsage(Base):
    """UI-SP4d: per-turn LLM token ledger for invariant-18 budget enforcement."""
    __tablename__ = "ai_token_usage"
    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_input: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_output: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
