"""Alembic environment: reads DATABASE_URL, targets medchron.db.models metadata."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from medchron.db.models import Base
from medchron.db.store import make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    import os

    from medchron.db.store import DEFAULT_URL

    context.configure(
        url=os.environ.get("DATABASE_URL", DEFAULT_URL),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")  # tests may pass one in
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    with make_engine().connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
