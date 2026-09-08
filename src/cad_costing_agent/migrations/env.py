"""Alembic environment.

Two things here differ from the stock template, both deliberately:

1. The database URL is **not** read from `alembic.ini`. It comes from the
   application's own configuration (`cad_costing_agent.db.database_url`), so
   `alembic upgrade head` and the running app always target the same database,
   and `CAD_COSTING_DATABASE_URL` remains the single place it is configured.

2. If the caller puts a live connection in `config.attributes["connection"]`,
   migrations run on that connection instead of opening a new one. This is
   essential for the in-memory SQLite databases used in tests: a second
   connection to `sqlite://` is a different, empty database, so migrations
   would otherwise run somewhere the tests never see.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from cad_costing_agent.db import database_url
from cad_costing_agent.db_models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things in place; batch mode rewrites the
        # table instead, which future migrations will need.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    existing_connection = config.attributes.get("connection")
    if existing_connection is not None:
        do_run_migrations(existing_connection)
        return

    connectable = create_engine(database_url(), poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            do_run_migrations(connection)
            connection.commit()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
