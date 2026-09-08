"""Authoring rate sets through the API (Step 14)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cad_costing_agent.db_models import RateSetRow
from cad_costing_agent.main import app
from cad_costing_agent.rate_sets import RateSet, RateSetVersionCreate
from cad_costing_agent.repository import RateSetRepository

client = TestClient(app)

# A realistic-shaped INR rate set. Still not real pricing — it is marked
# is_illustrative=False only to exercise that flag.
SHOP_RATES = {
    "rate_set_id": "acme-machine-shop",
    "name": "ACME Machine Shop",
    "version": "1.0.0",
    "description": "Authored through the API.",
    "is_illustrative": False,
    "materials": {
        "steel": {"name": "Steel", "cost_per_kg": 72.5, "density_kg_m3": 7850},
        "aluminium": {"name": "Aluminium", "cost_per_kg": 245.0, "density_kg_m3": 2700},
    },
    "operations": {
        "milling": {"machine_rate_per_hour": 1850.0, "labor_rate_per_hour": 420.0},
        "turning": {"machine_rate_per_hour": 1600.0, "labor_rate_per_hour": 420.0},
    },
    "additional_costs": {
        "overhead_rate_percent": 18.0,
        "scrap_rate_percent": 3.0,
        "tooling_cost_per_batch": 4500.0,
    },
}


def part_request(rate_set_id: str, version: str, quantity: int = 10) -> dict:
    return {
        "part": {
            "part_id": "P-AUTH",
            "part_name": "Bracket",
            "material": "Steel",
            "mass_kg": 2.0,
            "quantity": quantity,
        },
        "operation_requirements": [
            {
                "operation_id": "OP1",
                "operation_name": "Milling",
                "process_type": "Milling",
                "setup_time_minutes": 45.0,
                "cycle_time_minutes": 4.0,
                "quantity": quantity,
            }
        ],
        "rate_set_id": rate_set_id,
        "rate_set_version": version,
    }


# --- creation ---------------------------------------------------------------


def test_create_rate_set_returns_201_and_the_stored_rate_set():
    response = client.post("/rates", json=SHOP_RATES)

    assert response.status_code == 201
    body = response.json()
    assert body["rate_set_id"] == "acme-machine-shop"
    assert body["version"] == "1.0.0"
    assert body["name"] == "ACME Machine Shop"
    assert body["is_illustrative"] is False
    assert body["materials"]["steel"]["cost_per_kg"] == 72.5
    assert body["operations"]["milling"]["machine_rate_per_hour"] == 1850.0
    assert body["additional_costs"]["overhead_rate_percent"] == 18.0


def test_a_created_rate_set_is_immediately_retrievable_and_listed():
    client.post("/rates", json=SHOP_RATES)

    assert client.get("/rates/acme-machine-shop").json()["version"] == "1.0.0"

    identities = {(r["rate_set_id"], r["version"]) for r in client.get("/rates").json()}
    assert ("acme-machine-shop", "1.0.0") in identities


def test_created_rate_set_is_actually_persisted(db_session):
    client.post("/rates", json=SHOP_RATES)

    rows = db_session.execute(
        select(RateSetRow).where(RateSetRow.rate_set_id == "acme-machine-shop")
    ).scalars().all()

    assert len(rows) == 1
    assert rows[0].version == "1.0.0"
    assert rows[0].currency == "INR"


def test_currency_defaults_to_inr_when_omitted():
    assert "currency" not in SHOP_RATES

    body = client.post("/rates", json=SHOP_RATES).json()

    assert body["currency"] == "INR"


def test_an_explicit_currency_is_honoured():
    body = client.post("/rates", json={**SHOP_RATES, "currency": "EUR"}).json()

    assert body["currency"] == "EUR"


def test_is_illustrative_defaults_to_true_when_omitted():
    """Existing behaviour preserved: rates are presumed illustrative unless stated."""
    payload = {k: v for k, v in SHOP_RATES.items() if k != "is_illustrative"}

    body = client.post("/rates", json=payload).json()

    assert body["is_illustrative"] is True


def test_a_minimal_rate_set_is_accepted():
    body = client.post(
        "/rates", json={"rate_set_id": "bare", "name": "Bare", "version": "1.0.0"}
    ).json()

    assert body["materials"] == {}
    assert body["operations"] == {}
    assert body["additional_costs"]["overhead_rate_percent"] == 0.0
    assert body["currency"] == "INR"


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "X", "version": "1.0.0"},  # missing rate_set_id
        {"rate_set_id": "x", "version": "1.0.0"},  # missing name
        {"rate_set_id": "x", "name": "X"},  # missing version
        {"rate_set_id": "Has Spaces", "name": "X", "version": "1.0.0"},
        {"rate_set_id": "UPPER", "name": "X", "version": "1.0.0"},
        {"rate_set_id": "x", "name": "X", "version": "1.0"},
        {"rate_set_id": "x", "name": "X", "version": "v1.0.0"},
        {"rate_set_id": "x", "name": "", "version": "1.0.0"},
        {"rate_set_id": "x", "name": "X", "version": "1.0.0", "currency": "inr"},
        {"rate_set_id": "x", "name": "X", "version": "1.0.0", "currency": "RUPEE"},
    ],
)
def test_invalid_rate_sets_are_rejected(payload):
    assert client.post("/rates", json=payload).status_code == 422


@pytest.mark.parametrize(
    "materials",
    [
        {"steel": {"name": "Steel", "cost_per_kg": -1.0, "density_kg_m3": 7850}},
        {"steel": {"name": "Steel", "cost_per_kg": 0.0, "density_kg_m3": 7850}},
        {"steel": {"name": "Steel", "cost_per_kg": 10.0, "density_kg_m3": 0.0}},
        {"steel": {"name": "", "cost_per_kg": 10.0, "density_kg_m3": 7850}},
    ],
)
def test_invalid_material_rates_are_rejected(materials):
    payload = {**SHOP_RATES, "materials": materials}

    assert client.post("/rates", json=payload).status_code == 422


@pytest.mark.parametrize(
    "operations",
    [
        {"milling": {"machine_rate_per_hour": -1.0, "labor_rate_per_hour": 420.0}},
        {"milling": {"machine_rate_per_hour": 0.0, "labor_rate_per_hour": 420.0}},
        {"milling": {"machine_rate_per_hour": 1850.0, "labor_rate_per_hour": -1.0}},
    ],
)
def test_invalid_operation_rates_are_rejected(operations):
    payload = {**SHOP_RATES, "operations": operations}

    assert client.post("/rates", json=payload).status_code == 422


@pytest.mark.parametrize(
    "additional",
    [
        {"overhead_rate_percent": -1.0},
        {"overhead_rate_percent": 5000.0},
        {"scrap_rate_percent": 100.0},
        {"scrap_rate_percent": -1.0},
        {"tooling_cost_per_batch": -1.0},
    ],
)
def test_invalid_additional_cost_rates_are_rejected(additional):
    payload = {**SHOP_RATES, "additional_costs": additional}

    assert client.post("/rates", json=payload).status_code == 422


def test_a_rejected_rate_set_is_not_persisted(db_session):
    client.post("/rates", json={**SHOP_RATES, "version": "nope"})

    assert (
        db_session.execute(
            select(RateSetRow).where(RateSetRow.rate_set_id == "acme-machine-shop")
        ).scalars().all()
        == []
    )


# --- immutability -----------------------------------------------------------


def test_reposting_an_existing_version_is_rejected_as_a_conflict():
    assert client.post("/rates", json=SHOP_RATES).status_code == 201

    response = client.post("/rates", json=SHOP_RATES)

    assert response.status_code == 409
    assert "immutable" in response.json()["detail"]


def test_a_conflicting_post_cannot_overwrite_the_stored_rates():
    client.post("/rates", json=SHOP_RATES)

    tampered = {
        **SHOP_RATES,
        "name": "Tampered",
        "materials": {"steel": {"name": "Steel", "cost_per_kg": 9999.0, "density_kg_m3": 7850}},
    }
    assert client.post("/rates", json=tampered).status_code == 409

    stored = client.get("/rates/acme-machine-shop", params={"version": "1.0.0"}).json()
    assert stored["name"] == "ACME Machine Shop"
    assert stored["materials"]["steel"]["cost_per_kg"] == 72.5


def test_seeded_versions_cannot_be_overwritten_either():
    existing = client.get("/rates/poc-machine-shop", params={"version": "1.0.0"}).json()

    response = client.post("/rates", json={**existing, "name": "Hijacked"})

    assert response.status_code == 409
    assert (
        client.get("/rates/poc-machine-shop", params={"version": "1.0.0"}).json()["name"]
        == "POC Machine Shop"
    )


def test_there_is_no_update_or_delete_route_for_rate_sets():
    """Immutability is structural: no edit path exists at all."""
    assert client.put("/rates/acme-machine-shop", json=SHOP_RATES).status_code == 405
    assert client.patch("/rates/acme-machine-shop", json=SHOP_RATES).status_code == 405
    assert client.delete("/rates/acme-machine-shop").status_code == 405


# --- multiple versions ------------------------------------------------------


def test_multiple_versions_of_one_family_coexist():
    client.post("/rates", json=SHOP_RATES)
    client.post("/rates", json={**SHOP_RATES, "version": "2.0.0"})

    versions = {
        r["version"] for r in client.get("/rates").json() if r["rate_set_id"] == "acme-machine-shop"
    }
    assert versions == {"1.0.0", "2.0.0"}


def test_latest_version_resolution_uses_the_newest_authored_version():
    client.post("/rates", json=SHOP_RATES)
    client.post("/rates", json={**SHOP_RATES, "version": "10.0.0"})
    client.post("/rates", json={**SHOP_RATES, "version": "2.0.0"})

    # Numeric comparison, so 10.0.0 outranks 2.0.0.
    assert client.get("/rates/acme-machine-shop").json()["version"] == "10.0.0"


# --- the versions convenience endpoint --------------------------------------


def test_versions_endpoint_takes_the_id_from_the_path():
    client.post("/rates", json=SHOP_RATES)
    payload = {k: v for k, v in SHOP_RATES.items() if k != "rate_set_id"}

    response = client.post(
        "/rates/acme-machine-shop/versions", json={**payload, "version": "2.0.0"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["rate_set_id"] == "acme-machine-shop"
    assert body["version"] == "2.0.0"


def test_versions_endpoint_enforces_the_same_duplicate_rule():
    client.post("/rates", json=SHOP_RATES)
    payload = {k: v for k, v in SHOP_RATES.items() if k != "rate_set_id"}

    response = client.post("/rates/acme-machine-shop/versions", json=payload)

    assert response.status_code == 409


def test_versions_endpoint_rejects_an_invalid_path_id():
    payload = {k: v for k, v in SHOP_RATES.items() if k != "rate_set_id"}

    assert client.post("/rates/Has Spaces/versions", json=payload).status_code == 422


def test_versions_endpoint_defaults_currency_to_inr():
    payload = {
        k: v for k, v in SHOP_RATES.items() if k not in {"rate_set_id", "is_illustrative"}
    }

    body = client.post("/rates/new-family/versions", json=payload).json()

    assert body["currency"] == "INR"
    assert body["is_illustrative"] is True


def test_version_create_model_mirrors_rate_set_minus_the_id():
    """Guards the two models against drifting apart."""
    assert set(RateSetVersionCreate.model_fields) == set(RateSet.model_fields) - {"rate_set_id"}


# --- estimates keep using the version they were made with -------------------


def test_a_new_version_does_not_change_an_estimate_made_with_the_old_one():
    client.post("/rates", json=SHOP_RATES)

    first = client.post("/cost/part", json=part_request("acme-machine-shop", "1.0.0")).json()
    first_id = first["estimate_id"]
    stored_before = client.get(f"/estimates/{first_id}").json()

    # Publish a much more expensive version 2.
    client.post(
        "/rates",
        json={
            **SHOP_RATES,
            "version": "2.0.0",
            "materials": {
                "steel": {"name": "Steel", "cost_per_kg": 500.0, "density_kg_m3": 7850}
            },
            "operations": {
                "milling": {"machine_rate_per_hour": 5000.0, "labor_rate_per_hour": 1200.0}
            },
        },
    )

    second = client.post("/cost/part", json=part_request("acme-machine-shop", "2.0.0")).json()

    # The new estimate uses the new rates...
    assert second["total_part_cost"] > first["total_part_cost"]
    assert second["rate_set"]["version"] == "2.0.0"
    assert second["material"]["material_cost_per_kg"] == 500.0

    # ...and the old one is untouched, snapshots included.
    assert client.get(f"/estimates/{first_id}").json() == stored_before
    assert stored_before["rate_set_version"] == "1.0.0"
    assert stored_before["rate_set_snapshot"]["materials"]["steel"]["cost_per_kg"] == 72.5


def test_an_authored_rate_set_produces_a_hand_checkable_estimate():
    """Steel 72.5/kg, 2 kg, qty 10; milling 1850/hr machine + 420/hr labor,
    45 min setup, 4 min cycle; overhead 18%, scrap 3%, tooling 4500."""
    client.post("/rates", json=SHOP_RATES)

    body = client.post("/cost/part", json=part_request("acme-machine-shop", "1.0.0")).json()

    material = 2.0 * 72.5 * 10  # 1450.00
    run_hours = 4.0 * 10 / 60
    operations = 1850.0 * run_hours + 1850.0 * (45.0 / 60) + 420.0 * run_hours  # 2925.83...
    direct = material + operations
    total = direct + direct * 0.18 + direct * 0.03 + 4500.0

    assert body["currency"] == "INR"
    assert body["total_material_cost"] == pytest.approx(material)
    assert body["total_operation_cost"] == pytest.approx(operations, abs=0.01)
    assert body["total_part_cost"] == pytest.approx(total, abs=0.01)
    assert body["per_unit_cost"] == pytest.approx(total / 10, abs=0.01)


# --- repository-level -------------------------------------------------------


def test_repository_add_then_get_round_trips(db_session):
    repository = RateSetRepository(db_session)
    rate_set = RateSet(**SHOP_RATES)

    repository.add(rate_set)
    db_session.commit()

    assert repository.get("acme-machine-shop", "1.0.0").model_dump() == rate_set.model_dump()


def test_repository_add_rejects_a_duplicate(db_session):
    repository = RateSetRepository(db_session)
    repository.add(RateSet(**SHOP_RATES))
    db_session.commit()

    with pytest.raises(ValueError, match="immutable"):
        repository.add(RateSet(**SHOP_RATES))


def test_repository_exposes_no_update_or_delete():
    for forbidden in ("update", "delete", "remove", "save"):
        assert not hasattr(RateSetRepository, forbidden)
