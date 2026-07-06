"""Programmatic Alembic migration runner for traceforge system storage.

Called by SystemStore on initialization to bring the database to HEAD.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent


def _make_config(connection: Connection) -> Config:
    """Build an Alembic Config that uses the provided connection."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    return cfg


def run_migrations(connection: Connection) -> None:
    """Run all pending migrations against the given connection.

    This is called once per SystemStore initialization. It is idempotent —
    if the database is already at HEAD, this is a no-op.
    """
    cfg = _make_config(connection)
    command.upgrade(cfg, "head")
    logger.debug("Migrations applied successfully")


def current_revision(connection: Connection) -> str | None:
    """Return the current alembic revision (or None if unversioned)."""
    from alembic.runtime.migration import MigrationContext

    ctx = MigrationContext.configure(connection)
    return ctx.get_current_revision()
