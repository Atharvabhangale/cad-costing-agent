"""Database wiring: configurable URL, lazily-created engine, injected sessions.

Sessions are handed to endpoints via FastAPI's dependency system (`get_session`)
rather than being reached for through a module-level global, so a test can swap
the whole database out with `app.dependency_overrides` and nothing else has to
change. The engine — a connection pool, not a connection — is cached lazily and
can be reset with `reset_engine()` when the configured URL changes.

The default is a local SQLite file. Schema changes are managed by Alembic (see
`run_migrations`), so a database can be upgraded in place without losing data.
This is still POC-grade persistence: no connection tuning, no backups, and no
concurrency story beyond SQLite's own.
"""

import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL_ENV_VAR = "CAD_COSTING_DATABASE_URL"
DEFAULT_DATABASE_URL = "sqlite:///./cad_costing_agent.db"

# Migration scripts ship inside the package, so they are found regardless of
# the process's working directory.
MIGRATIONS_PATH = Path(__file__).resolve().parent / "migrations"
ALEMBIC_VERSION_TABLE = "alembic_version"
MANAGED_TABLES = frozenset({"rate_sets", "estimates"})

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def database_url() -> str:
    """The configured database URL, defaulting to a local SQLite file."""
    return os.environ.get(DATABASE_URL_ENV_VAR, DEFAULT_DATABASE_URL)


def create_db_engine(url: str | None = None) -> Engine:
    resolved_url = url or database_url()
    # SQLite defaults to rejecting cross-thread use, which a threaded ASGI
    # server would otherwise trip over.
    connect_args = {"check_same_thread": False} if resolved_url.startswith("sqlite") else {}
    return create_engine(resolved_url, connect_args=connect_args)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding one session per request."""
    with get_session_factory()() as session:
        yield session


def reset_engine() -> None:
    """Discard the cached engine and session factory."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def _alembic_config(connection=None):
    """Build an Alembic config pointing at the packaged migration scripts.

    Built in code rather than read from `alembic.ini` so it does not depend on
    the process's working directory, and so the URL comes from exactly the same
    place the application uses.
    """
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    config.set_main_option("sqlalchemy.url", database_url())
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def run_migrations(engine: Engine | None = None) -> str:
    """Bring the database schema up to head. Never destroys data.

    Three cases, distinguished so that adopting Alembic on an existing database
    cannot lose anything:

    - **Already under Alembic** (`alembic_version` present): upgrade to head,
      applying whatever revisions are outstanding.
    - **Pre-Alembic database** from Step 10/11: the tables exist but there is no
      version table. Its schema already matches revision 0001, so it is
      *stamped* at head — recording the version without running any DDL. Running
      the migration instead would fail on the existing tables; dropping and
      recreating would throw away stored rate sets and estimates.
    - **Empty database**: upgrade to head, which creates the tables.

    Returns which of those paths was taken: "upgraded", "stamped", or "created".
    """
    from alembic import command
    from sqlalchemy import inspect

    resolved_engine = engine or get_engine()
    with resolved_engine.begin() as connection:
        config = _alembic_config(connection)
        tables = set(inspect(connection).get_table_names())

        if ALEMBIC_VERSION_TABLE in tables:
            command.upgrade(config, "head")
            return "upgraded"

        if tables & MANAGED_TABLES:
            command.stamp(config, "head")
            return "stamped"

        command.upgrade(config, "head")
        return "created"


def init_db(engine: Engine | None = None) -> int:
    """Migrate the database to head and seed the illustrative catalogs.

    Idempotent: safe to call on every startup. Returns how many versions were
    newly inserted across both catalogs (zero on an already-seeded database).

    Rate sets and time profiles are seeded together but stay separate catalogs —
    one decides money, the other duration.
    """
    from cad_costing_agent.rate_sets import SEED_RATE_SETS
    from cad_costing_agent.repository import RateSetRepository, TimeProfileRepository
    from cad_costing_agent.time_profiles import SEED_TIME_PROFILES

    resolved_engine = engine or get_engine()
    run_migrations(resolved_engine)

    with sessionmaker(bind=resolved_engine, expire_on_commit=False)() as session:
        inserted = RateSetRepository(session).seed(SEED_RATE_SETS)
        inserted += TimeProfileRepository(session).seed(SEED_TIME_PROFILES)
        session.commit()
    return inserted
