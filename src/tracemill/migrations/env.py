"""Alembic environment configuration for tracemill system storage.

This is used programmatically — not via the `alembic` CLI — so there is no
alembic.ini file. Instead, SystemStore calls run_migrations() directly.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from tracemill.migrations.models import metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live connection)."""
    url = context.config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "qmark"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (with a live connection)."""
    connectable = context.config.attributes.get("connection")

    if connectable is not None:
        # Connection already provided by the caller (SystemStore)
        context.configure(connection=connectable, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
    else:
        # Fallback: create engine from config
        configuration = context.config.get_section(context.config.config_ini_section, {})
        connectable = engine_from_config(
            configuration,
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=metadata)
            with context.begin_transaction():
                context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
