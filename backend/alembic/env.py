"""Alembic env configured for async engine + app.gateway.identity metadata."""

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.gateway.identity.models import Base  # noqa: F401 populates metadata
from app.gateway.identity.settings import get_identity_settings

config = context.config
if config.config_file_name:
    # disable_existing_loggers=False prevents fileConfig from disabling all
    # loggers defined outside alembic.ini — important during pytest runs where
    # test fixtures rely on caplog being able to capture warnings from
    # unrelated loggers (e.g. deerflow.tools.tools).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

config.set_main_option("sqlalchemy.url", get_identity_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        version_table_schema="identity",
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # Ensure the identity schema exists before alembic tries to create the
    # version table inside it. Idempotent.
    connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS identity")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        version_table_schema="identity",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=None,
    )
    async with connectable.begin() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
