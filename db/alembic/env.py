"""Alembic environment — builds the DB URL from environment variables.

Works both inside the HA addon (env set by s6/init_addon) and locally
(env loaded from .env via python-dotenv).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from dotenv import load_dotenv

load_dotenv()

from alembic import context
from sqlalchemy import create_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _db_url() -> str:
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "health")
    user = os.environ.get("DB_USER", "postgres")
    pw = os.environ.get("DB_PASSWORD", "")
    return f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{name}"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    context.configure(
        url=_db_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    engine = create_engine(_db_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
