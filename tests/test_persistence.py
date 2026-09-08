import json

import pytest
import trimesh
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cad_costing_agent.db import init_db
from cad_costing_agent.time_profiles import SEED_TIME_PROFILES
from cad_costing_agent.db_models import EstimateRow, RateSetRow
from cad_costing_agent.main import app
from cad_costing_agent.rate_sets import SEED_RATE_SETS, MaterialRate, OperationRate, RateSet
from cad_costing_agent.repository import EstimateRepository, RateSetRepository

client = TestClient(app)

WORKED_PART = {
    "part_id": "P100",
    "part_name": "Flange",
    "material": "Steel",
    "mass_kg": 2.5,
    "quantity": 4,
}
WORKED_REQUIREMENT = {
    "operation_id": "OP1",
    "operation_name": "Milling",
    "process_type": "Milling",
    "setup_time_minutes": 30.0,
    "cycle_time_minutes": 3.0,
    "quantity": 4,
}
WORKED_REQUEST = {
    "part": WORKED_PART,
    "operation_requirements": [WORKED_REQUIREMENT],
    "rate_set_id": "poc-machine-shop",
    "rate_set_version": "1.0.0",
}


def _cube_stl_bytes() -> bytes:
    return trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")


def _fresh_engine():
    return create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


# --- initialization and seeding --------------------------------------------


def test_init_db_creates_the_expected_tables():
    engine = _fresh_engine()
    init_db(engine)

    tables = set(inspect(engine).get_table_names())
    assert {"rate_sets", "estimates"} <= tables


def test_init_db_seeds_every_illustrative_rate_set():
    engine = _fresh_engine()
    inserted = init_db(engine)

    # Both catalogs are seeded: rate sets for money, time profiles for duration.
    assert inserted == len(SEED_RATE_SETS) + len(SEED_TIME_PROFILES)

    with sessionmaker(bind=engine)() as session:
        stored = session.execute(select(RateSetRow)).scalars().all()
        identities = {(row.rate_set_id, row.version) for row in stored}

    assert identities == {
        (rate_set.rate_set_id, rate_set.version) for rate_set in SEED_RATE_SETS
    }


def test_seeding_is_idempotent():
    engine = _fresh_engine()

    first = init_db(engine)
    second = init_db(engine)
    third = init_db(engine)

    assert first == len(SEED_RATE_SETS) + len(SEED_TIME_PROFILES)
    assert second == 0
    assert third == 0

    with sessionmaker(bind=engine)() as session:
        assert len(session.execute(select(RateSetRow)).scalars().all()) == len(SEED_RATE_SETS)


def test_seeded_values_match_the_seed_data_exactly(db_session):
    repository = RateSetRepository(db_session)

    for seeded in SEED_RATE_SETS:
        stored = repository.get(seeded.rate_set_id, seeded.version)
        assert stored.model_dump() == seeded.model_dump()


# --- rate set repository ----------------------------------------------------


def test_repository_lists_every_stored_version(db_session):
    summaries = RateSetRepository(db_session).list()
    identities = {(summary.rate_set_id, summary.version) for summary in summaries}

    assert ("poc-default", "1.0.0") in identities
    assert ("poc-machine-shop", "1.0.0") in identities
    assert ("poc-machine-shop", "2.0.0") in identities


def test_repository_resolves_an_exact_version(db_session):
    rate_set = RateSetRepository(db_session).get("poc-machine-shop", "1.0.0")

    assert rate_set.version == "1.0.0"
    assert rate_set.materials["steel"].cost_per_kg == 0.80
    assert rate_set.operations["milling"].machine_rate_per_hour == 1200.0
    assert rate_set.additional_costs.overhead_rate_percent == 10.0


def test_repository_resolves_the_latest_version_when_none_given(db_session):
    assert RateSetRepository(db_session).get("poc-machine-shop").version == "2.0.0"


def test_latest_version_is_compared_numerically_not_lexically(db_session):
    """10.0.0 must outrank 2.0.0, which a plain string sort would get wrong."""
    repository = RateSetRepository(db_session)
    repository.add(
        RateSet(
            rate_set_id="poc-machine-shop",
            name="POC Machine Shop",
            version="10.0.0",
            materials={"steel": MaterialRate(name="Steel", cost_per_kg=1.0, density_kg_m3=7850)},
            operations={
                "milling": OperationRate(machine_rate_per_hour=1.0, labor_rate_per_hour=1.0)
            },
        )
    )
    db_session.commit()

    assert repository.get("poc-machine-shop").version == "10.0.0"


def test_repository_rejects_an_unknown_rate_set(db_session):
    with pytest.raises(ValueError, match="Unknown rate set"):
        RateSetRepository(db_session).get("does-not-exist")


def test_repository_rejects_an_unknown_version(db_session):
    with pytest.raises(ValueError, match="no version"):
        RateSetRepository(db_session).get("poc-machine-shop", "9.9.9")


def test_duplicate_version_rejected(db_session):
    repository = RateSetRepository(db_session)
    duplicate = RateSetRepository(db_session).get("poc-machine-shop", "1.0.0")

    with pytest.raises(ValueError, match="already exists"):
        repository.add(duplicate)


def test_rate_set_versions_are_immutable(db_session):
    """There is no update path: new rates require a new version."""
    repository = RateSetRepository(db_session)

    assert not hasattr(repository, "update")

    original = repository.get("poc-machine-shop", "1.0.0")
    changed = original.model_copy(
        update={"materials": {"steel": MaterialRate(name="Steel", cost_per_kg=99.0, density_kg_m3=7850)}}
    )
    with pytest.raises(ValueError, match="immutable"):
        repository.add(changed)

    # The stored version is untouched.
    assert repository.get("poc-machine-shop", "1.0.0").materials["steel"].cost_per_kg == 0.80


def test_a_new_version_can_be_published_alongside_the_old_one(db_session):
    repository = RateSetRepository(db_session)
    original = repository.get("poc-machine-shop", "1.0.0")
    repository.add(original.model_copy(update={"version": "3.0.0"}))
    db_session.commit()

    assert repository.get("poc-machine-shop", "1.0.0").version == "1.0.0"
    assert repository.get("poc-machine-shop", "3.0.0").version == "3.0.0"


# --- estimate persistence ---------------------------------------------------


def test_cost_part_persists_an_estimate_and_returns_its_id():
    response = client.post("/cost/part", json=WORKED_REQUEST)

    assert response.status_code == 200
    body = response.json()
    assert body["estimate_id"]
    assert body["total_part_cost"] == pytest.approx(1363.20)

    stored = client.get(f"/estimates/{body['estimate_id']}")
    assert stored.status_code == 200
    assert stored.json()["total_part_cost"] == pytest.approx(1363.20)


def test_estimate_ids_are_unique():
    ids = {client.post("/cost/part", json=WORKED_REQUEST).json()["estimate_id"] for _ in range(5)}

    assert len(ids) == 5


def test_persisted_estimate_carries_rate_set_provenance():
    estimate_id = client.post("/cost/part", json=WORKED_REQUEST).json()["estimate_id"]

    stored = client.get(f"/estimates/{estimate_id}").json()

    assert stored["rate_set_id"] == "poc-machine-shop"
    assert stored["rate_set_version"] == "1.0.0"
    assert stored["result_snapshot"]["rate_set"]["name"] == "POC Machine Shop"


def test_persisted_estimate_carries_request_and_rate_snapshots():
    estimate_id = client.post("/cost/part", json=WORKED_REQUEST).json()["estimate_id"]

    stored = client.get(f"/estimates/{estimate_id}").json()

    # What was asked for.
    assert stored["request_snapshot"]["part"]["part_id"] == "P100"
    assert stored["request_snapshot"]["operation_requirements"][0]["process_type"] == "Milling"
    # The exact rates that were resolved.
    assert stored["rate_set_snapshot"]["materials"]["steel"]["cost_per_kg"] == 0.80
    assert stored["rate_set_snapshot"]["operations"]["milling"]["machine_rate_per_hour"] == 1200.0
    # The full computed breakdown.
    assert stored["result_snapshot"]["direct_cost_base"] == pytest.approx(968.00)


def test_estimate_records_identity_and_totals():
    estimate_id = client.post("/cost/part", json=WORKED_REQUEST).json()["estimate_id"]

    stored = client.get(f"/estimates/{estimate_id}").json()

    assert stored["source"] == "cost/part"
    assert stored["part_id"] == "P100"
    assert stored["part_name"] == "Flange"
    assert stored["material"] == "Steel"
    assert stored["quantity"] == 4
    assert stored["created_at"]
    assert stored["total_material_cost"] == pytest.approx(8.00)
    assert stored["total_operation_cost"] == pytest.approx(960.00)
    assert stored["total_additional_cost"] == pytest.approx(395.20)


def test_unknown_estimate_returns_404():
    response = client.get("/estimates/does-not-exist")

    assert response.status_code == 404
    assert "Unknown estimate" in response.json()["detail"]


def test_cost_part_without_a_rate_set_is_still_persisted():
    response = client.post("/cost/part", json={"part": WORKED_PART})

    assert response.status_code == 200
    estimate_id = response.json()["estimate_id"]

    stored = client.get(f"/estimates/{estimate_id}").json()
    assert stored["rate_set_id"] is None
    assert stored["rate_set_version"] is None
    assert stored["rate_set_snapshot"] is None


def test_cost_cad_part_persists_an_estimate():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", _cube_stl_bytes(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "4",
            "operation_requirements": json.dumps([WORKED_REQUIREMENT]),
            "rate_set_id": "poc-machine-shop",
            "rate_set_version": "1.0.0",
        },
    )

    assert response.status_code == 200
    body = response.json()
    estimate_id = body["estimate_id"]
    assert estimate_id
    # Also exposed on the nested part cost, from the same value.
    assert body["part_cost"]["estimate_id"] == estimate_id

    stored = client.get(f"/estimates/{estimate_id}").json()
    assert stored["source"] == "cost/cad-part"
    assert stored["part_id"] == "P001"
    assert stored["rate_set_version"] == "1.0.0"
    # The CAD snapshot records the geometry and derived mass, not the file bytes.
    assert stored["request_snapshot"]["source_file"] == "cube.stl"
    assert stored["request_snapshot"]["geometry"]["volume"] == pytest.approx(6000.0)
    assert stored["request_snapshot"]["mass"]["mass_kg"] == pytest.approx(0.0471)


def test_a_failed_estimate_is_not_persisted(db_session):
    response = client.post(
        "/cost/part", json={"part": WORKED_PART, "rate_set_id": "does-not-exist"}
    )

    assert response.status_code == 400
    assert db_session.execute(select(EstimateRow)).scalars().all() == []


# --- history is not rewritten by later rate changes ------------------------


def test_historical_estimate_is_unchanged_by_a_newer_rate_set_version(db_session):
    estimate_id = client.post("/cost/part", json=WORKED_REQUEST).json()["estimate_id"]
    before = client.get(f"/estimates/{estimate_id}").json()

    # Publish a new, more expensive version of the same rate set.
    repository = RateSetRepository(db_session)
    original = repository.get("poc-machine-shop", "1.0.0")
    repository.add(
        original.model_copy(
            update={
                "version": "3.0.0",
                "materials": {
                    "steel": MaterialRate(name="Steel", cost_per_kg=99.0, density_kg_m3=7850)
                },
            }
        )
    )
    db_session.commit()

    assert repository.get("poc-machine-shop").version == "3.0.0"

    after = client.get(f"/estimates/{estimate_id}").json()
    assert after == before
    assert after["total_part_cost"] == pytest.approx(1363.20)
    assert after["rate_set_version"] == "1.0.0"
    assert after["rate_set_snapshot"]["materials"]["steel"]["cost_per_kg"] == 0.80


# --- isolation --------------------------------------------------------------


def test_estimates_written_by_earlier_tests_do_not_leak(db_session):
    """Each test gets a fresh database, so no estimate rows carry over.

    Several tests above write estimates; if the fixture were not isolating
    them, this table would not be empty.
    """
    assert db_session.execute(select(EstimateRow)).scalars().all() == []


def test_each_test_starts_from_freshly_seeded_rate_sets(db_session):
    stored = db_session.execute(select(RateSetRow)).scalars().all()

    assert len(stored) == len(SEED_RATE_SETS)
