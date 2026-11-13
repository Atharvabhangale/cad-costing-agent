"""Migration tests.

The important one is `test_upgrading_a_pre_alembic_database_preserves_data`:
adopting Alembic on a database created before it existed must not lose the rate
sets or estimates already stored there.
"""



import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cad_costing_agent.db import (
    ALEMBIC_VERSION_TABLE,
    MIGRATIONS_PATH,
    init_db,
    run_migrations,
)
from cad_costing_agent.db_models import Base, EstimateRow, RateSetRow
from cad_costing_agent.dfm import SEED_DFM_PROFILES
from cad_costing_agent.rate_sets import SEED_RATE_SETS
from cad_costing_agent.time_profiles import SEED_TIME_PROFILES

# `init_db` seeds both catalogs — rate sets (money) and time profiles
# (duration) — so its return value counts versions across both.
SEEDED_VERSIONS = len(SEED_RATE_SETS) + len(SEED_TIME_PROFILES) + len(SEED_DFM_PROFILES)
from cad_costing_agent.repository import EstimateRepository, RateSetRepository

HEAD_REVISION = "0011"


def _engine(path=None):
    if path is None:
        return create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    return create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})


def _stamped_revision(engine) -> str | None:
    with engine.connect() as connection:
        if not inspect(connection).has_table(ALEMBIC_VERSION_TABLE):
            return None
        return connection.execute(text("select version_num from alembic_version")).scalar_one()


def _schema_fingerprint(engine) -> dict:
    """Structure of the user tables, ignoring Alembic's bookkeeping table."""
    inspector = inspect(engine)
    fingerprint = {}
    for table in sorted(inspector.get_table_names()):
        if table == ALEMBIC_VERSION_TABLE:
            continue
        fingerprint[table] = {
            "columns": {
                column["name"]: (str(column["type"]), column["nullable"])
                for column in inspector.get_columns(table)
            },
            "primary_key": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
            "indexes": sorted(
                (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                for index in inspector.get_indexes(table)
            ),
            "unique_constraints": sorted(
                (constraint["name"], tuple(constraint["column_names"]))
                for constraint in inspector.get_unique_constraints(table)
            ),
        }
    return fingerprint


def _build_pre_alembic_database(engine) -> tuple[list[str], int]:
    """Recreate a Step-11 database: real schema, real data, no alembic_version."""
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        RateSetRepository(session).seed(SEED_RATE_SETS)
        estimate_ids = [
            EstimateRepository(session).save(
                source="cost/part",
                part_id=f"LEGACY-{index}",
                part_name="Flange",
                material="Steel",
                quantity=4,
                rate_set=None,
                currency="INR",
                total_material_cost=8.0,
                total_operation_cost=960.0,
                total_additional_cost=395.2,
                total_part_cost=1363.2,
                request_snapshot={"legacy": True, "index": index},
                result_snapshot={"total_part_cost": 1363.2},
            )
            for index in range(3)
        ]
        session.commit()
    return estimate_ids, len(SEED_RATE_SETS)


# --- fresh database ---------------------------------------------------------


def test_upgrade_head_on_a_fresh_database_creates_the_schema():
    engine = _engine()

    assert run_migrations(engine) == "created"

    tables = set(inspect(engine).get_table_names())
    assert {
        "rate_sets",
        "estimates",
        "time_profiles",
        "quotations",
        "cost_actuals",
        ALEMBIC_VERSION_TABLE,
    } <= tables
    assert _stamped_revision(engine) == HEAD_REVISION


def test_fresh_database_gets_the_estimates_id_primary_key_and_unique_estimate_id():
    engine = _engine()
    run_migrations(engine)

    inspector = inspect(engine)
    assert inspector.get_pk_constraint("estimates")["constrained_columns"] == ["id"]

    estimate_id_indexes = [
        index
        for index in inspector.get_indexes("estimates")
        if index["column_names"] == ["estimate_id"]
    ]
    assert estimate_id_indexes, "estimate_id must be indexed"
    assert all(index["unique"] for index in estimate_id_indexes)


def test_migrated_schema_matches_the_orm_models():
    """Guards against the migration drifting away from db_models.py."""
    migrated = _engine()
    run_migrations(migrated)

    from_models = _engine()
    Base.metadata.create_all(from_models)

    assert _schema_fingerprint(migrated) == _schema_fingerprint(from_models)


def test_init_db_migrates_then_seeds():
    engine = _engine()

    assert init_db(engine) == SEEDED_VERSIONS
    assert _stamped_revision(engine) == HEAD_REVISION

    with sessionmaker(bind=engine)() as session:
        assert len(session.execute(select(RateSetRow)).scalars().all()) == len(SEED_RATE_SETS)


def test_running_migrations_again_is_idempotent():
    engine = _engine()
    run_migrations(engine)

    assert run_migrations(engine) == "upgraded"
    assert _stamped_revision(engine) == HEAD_REVISION


def test_init_db_is_idempotent_across_repeated_startups():
    engine = _engine()

    assert init_db(engine) == SEEDED_VERSIONS
    assert init_db(engine) == 0
    assert init_db(engine) == 0

    with sessionmaker(bind=engine)() as session:
        assert len(session.execute(select(RateSetRow)).scalars().all()) == len(SEED_RATE_SETS)


# --- existing pre-Alembic database -----------------------------------------


def test_upgrading_a_pre_alembic_database_preserves_data():
    """A Step-11 database is adopted, not rebuilt: every row survives."""
    engine = _engine()
    estimate_ids, rate_set_count = _build_pre_alembic_database(engine)
    assert _stamped_revision(engine) is None

    assert run_migrations(engine) == "stamped"

    assert _stamped_revision(engine) == HEAD_REVISION
    with sessionmaker(bind=engine)() as session:
        rate_sets = session.execute(select(RateSetRow)).scalars().all()
        estimates = session.execute(select(EstimateRow)).scalars().all()

    assert len(rate_sets) == rate_set_count
    assert {estimate.estimate_id for estimate in estimates} == set(estimate_ids)
    # Snapshots survive intact, not just row counts.
    assert all(estimate.request_snapshot["legacy"] is True for estimate in estimates)
    assert all(estimate.total_part_cost == pytest.approx(1363.2) for estimate in estimates)


def test_init_db_on_a_pre_alembic_database_preserves_data_and_reseeds_nothing():
    engine = _engine()
    estimate_ids, rate_set_count = _build_pre_alembic_database(engine)

    # The rate sets were already seeded, so none is re-inserted. The time-profile
    # catalog did not exist before Step 21, so adopting this database back-fills
    # the baseline profile — the catalog has to exist for a time to be estimated.
    assert init_db(engine) == len(SEED_TIME_PROFILES) + len(SEED_DFM_PROFILES)

    with sessionmaker(bind=engine)() as session:
        assert len(session.execute(select(RateSetRow)).scalars().all()) == rate_set_count
        assert len(session.execute(select(EstimateRow)).scalars().all()) == len(estimate_ids)


def test_a_stamped_database_still_serves_and_accepts_estimates():
    """After adoption the app keeps working against the same data."""
    engine = _engine()
    estimate_ids, _ = _build_pre_alembic_database(engine)
    run_migrations(engine)

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        repository = EstimateRepository(session)

        # Old rows are still readable.
        assert repository.get(estimate_ids[0]).part_id == "LEGACY-0"

        # And new ones can still be written alongside them.
        new_id = repository.save(
            source="cost/part",
            part_id="AFTER-MIGRATION",
            part_name="Flange",
            material="Steel",
            quantity=1,
            rate_set=None,
            currency="INR",
            total_material_cost=1.0,
            total_operation_cost=2.0,
            total_additional_cost=3.0,
            total_part_cost=6.0,
            request_snapshot={},
            result_snapshot={},
        )
        session.commit()

        page = repository.list(limit=50)

    assert page.total == len(estimate_ids) + 1
    assert page.items[0].estimate_id == new_id
    assert repository.get_summary(new_id).part_id == "AFTER-MIGRATION"


def test_stamping_runs_no_ddl_against_an_existing_database():
    """Adoption must not alter the existing schema in any way."""
    engine = _engine()
    _build_pre_alembic_database(engine)
    before = _schema_fingerprint(engine)

    run_migrations(engine)

    assert _schema_fingerprint(engine) == before


# --- file-backed database, and persistence across connections --------------


def test_migration_works_on_a_file_backed_database(tmp_path):
    database_path = tmp_path / "file.db"
    engine = _engine(database_path)

    assert run_migrations(engine) == "created"
    engine.dispose()

    reopened = _engine(database_path)
    assert _stamped_revision(reopened) == HEAD_REVISION
    assert {"rate_sets", "estimates"} <= set(inspect(reopened).get_table_names())
    reopened.dispose()


def test_pre_alembic_file_database_upgrades_in_place(tmp_path):
    database_path = tmp_path / "legacy.db"
    engine = _engine(database_path)
    estimate_ids, rate_set_count = _build_pre_alembic_database(engine)
    engine.dispose()

    upgrading = _engine(database_path)
    assert run_migrations(upgrading) == "stamped"
    upgrading.dispose()

    reopened = _engine(database_path)
    with sessionmaker(bind=reopened)() as session:
        assert len(session.execute(select(RateSetRow)).scalars().all()) == rate_set_count
        assert len(session.execute(select(EstimateRow)).scalars().all()) == len(estimate_ids)
    reopened.dispose()


# --- safety -----------------------------------------------------------------


@pytest.mark.parametrize(
    "revision_file", sorted((MIGRATIONS_PATH / "versions").glob("*.py")), ids=lambda p: p.name
)
def test_upgrade_paths_contain_no_destructive_operations(revision_file):
    """No migration's upgrade() may drop or delete. That belongs in downgrade().

    Parameterised over every revision file, so a future migration that would
    silently destroy stored estimates fails here.
    """
    source = revision_file.read_text(encoding="utf-8")
    upgrade_body = source.split("def upgrade()")[1].split("def downgrade()")[0]

    for destructive in ("drop_table", "drop_column", "drop_index", "delete(", "truncate"):
        assert destructive not in upgrade_body, f"{revision_file.name} upgrade() has {destructive}"


def test_exactly_one_head_revision():
    """Multiple heads would make `upgrade head` ambiguous."""
    from alembic.script import ScriptDirectory
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_PATH))

    assert len(ScriptDirectory.from_config(config).get_heads()) == 1
