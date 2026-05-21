"""Async-aware alembic env using dlw.config + Base.metadata."""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import all models so Base.metadata is fully populated
from dlw.config import get_settings
from dlw.db import Base
from dlw.db.models import (  # noqa: F401  side-effect import
    AIConversation,
    AIMessage,
    AuditLog,
    CasbinRule,
    DownloadTask,
    Executor,
    FileSubTask,
    Project,
    QuotaSnapshot,
    StorageBackend,
    Tenant,
    UsageRecord,
    User,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Override sqlalchemy.url from app settings."""
    return get_settings().db_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    config_section = config.get_section(config.config_ini_section, {})
    config_section["sqlalchemy.url"] = get_url()
    connectable = async_engine_from_config(
        config_section, prefix="sqlalchemy.", future=True
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
