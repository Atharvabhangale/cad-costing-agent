"""Currency and 2-decimal presentation policy (Step 13)."""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from cad_costing_agent.additional_costs import AdditionalCostInputs
from cad_costing_agent.db import init_db, run_migrations
from cad_costing_agent.db_models import EstimateRow, RateSetRow
from cad_costing_agent.main import app
from cad_costing_agent.money import DEFAULT_CURRENCY, round_money, round_percent
from cad_costing_agent.operation import ManufacturingOperation
from cad_costing_agent.part import Part
from cad_costing_agent.part_cost import PartCostEstimateRequest, calculate_part_cost
from cad_costing_agent.rate_sets import (
    SEED_RATE_SETS,
    MaterialRate,
    OperationRate,
    OperationRequirement,
    RateSet,
)
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


def seed_rate_set(rate_set_id: str, version: str) -> RateSet:
    return next(
        r for r in SEED_RATE_SETS if r.rate_set_id == rate_set_id and r.version == version
    )


# --- rounding policy --------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1366.6666666666667, 1366.67),
        (0.3768, 0.38),
        (96.80000000000001, 96.8),
        (1367.0434666666667, 1367.04),
        (0.0, 0.0),
        (0.004, 0.0),
        (0.005, 0.01),  # half-up, not banker's rounding
        (0.125, 0.13),  # round() would give 0.12
        (2.675, 2.68),
    ],
)
def test_round_money_is_two_places_half_up(value, expected):
    assert round_money(value) == expected


def test_round_percent_matches_the_same_policy():
    assert round_percent(31.043466666) == 31.04
    assert round_percent(None) is None


def test_models_keep_full_precision_internally():
    """Rounding is presentation only, so intermediates are never rounded."""
    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operation_requirements=[],
            operations=[
                ManufacturingOperation(
                    operation_id="OP1",
                    operation_name="Drilling",
                    process_type="Drilling",
                    machine_rate_per_hour=1800.0,
                    labor_rate_per_hour=500.0,
                    setup_time_minutes=20.0,
                    cycle_time_minutes=2.0,
                    quantity=4,
                )
            ],
        )
    )

    exact_labor = 500.0 * (2.0 * 4 / 60)
    assert exact_labor != round_money(exact_labor)
    # The attribute is exact...
    assert result.operations[0].labor_cost == pytest.approx(exact_labor)
    # ...and only the serialized form is rounded.
    assert result.model_dump()["operations"][0]["labor_cost"] == round_money(exact_labor)


def test_totals_are_rounded_from_the_exact_value_not_from_rounded_parts():
    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operation_requirements=[OperationRequirement(**WORKED_REQUIREMENT)],
            rate_set_id="poc-machine-shop",
            rate_set_version="1.0.0",
        ),
        rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
    )

    exact_total = (
        result.total_material_cost + result.total_operation_cost + result.total_additional_cost
    )
    assert result.model_dump()["total_part_cost"] == round_money(exact_total)


def test_rates_are_not_rounded():
    """Rounding applies to computed amounts, not to echoed input rates."""
    body = client.post("/cost/part", json=WORKED_REQUEST).json()

    assert body["material"]["material_cost_per_kg"] == 0.80
    assert body["operations"][0]["machine_rate_per_hour"] == 1200.0
    assert body["operations"][0]["labor_rate_per_hour"] == 600.0


def test_every_monetary_field_in_an_estimate_response_is_two_places():
    body = client.post(
        "/cost/part",
        json={
            **WORKED_REQUEST,
            "part": {**WORKED_PART, "quantity": 3},
            "operation_requirements": [{**WORKED_REQUIREMENT, "quantity": 3}],
        },
    ).json()

    monetary = [
        body["total_material_cost"],
        body["total_operation_cost"],
        body["direct_cost_base"],
        body["total_additional_cost"],
        body["total_part_cost"],
        body["per_unit_cost"],
        body["material"]["material_cost"],
        body["material"]["total_material_cost"],
        body["additional_costs"]["overhead_cost"],
        body["additional_costs"]["scrap_cost"],
        body["additional_costs"]["tooling_cost"],
        body["additional_costs"]["total_additional_cost"],
        *(
            value
            for operation in body["operations"]
            for value in (
                operation["machine_cost"],
                operation["setup_cost"],
                operation["labor_cost"],
                operation["total_operation_cost"],
            )
        ),
    ]

    assert monetary
    for amount in monetary:
        assert round(amount, 2) == amount, f"{amount} is not presented at 2 decimal places"


# --- currency ---------------------------------------------------------------


def test_default_currency_is_inr():
    assert DEFAULT_CURRENCY == "INR"


def test_every_seeded_rate_set_is_inr():
    assert all(rate_set.currency == "INR" for rate_set in SEED_RATE_SETS)


def test_rate_set_defaults_to_inr_when_unspecified():
    assert RateSet(rate_set_id="x", name="X", version="1.0.0").currency == "INR"


@pytest.mark.parametrize("currency", ["", "in", "inr", "INRR", "1NR", "IN$"])
def test_invalid_currency_codes_rejected(currency):
    with pytest.raises(ValidationError):
        RateSet(rate_set_id="x", name="X", version="1.0.0", currency=currency)


def test_rates_endpoint_reports_currency():
    for row in client.get("/rates").json():
        assert row["currency"] == "INR"

    assert client.get("/rates/poc-machine-shop").json()["currency"] == "INR"


def test_currency_propagates_from_the_rate_set_into_the_estimate():
    body = client.post("/cost/part", json=WORKED_REQUEST).json()

    assert body["currency"] == "INR"
    assert body["rate_set"]["currency"] == "INR"


def test_estimate_without_a_rate_set_still_reports_the_default_currency():
    body = client.post("/cost/part", json={"part": WORKED_PART}).json()

    assert body["rate_set"] is None
    assert body["currency"] == "INR"


def test_a_non_inr_rate_set_propagates_its_own_currency(db_session):
    repository = RateSetRepository(db_session)
    repository.add(
        RateSet(
            rate_set_id="euro-shop",
            name="Euro Shop",
            version="1.0.0",
            currency="EUR",
            materials={"steel": MaterialRate(name="Steel", cost_per_kg=1.0, density_kg_m3=7850)},
            operations={
                "milling": OperationRate(machine_rate_per_hour=100.0, labor_rate_per_hour=50.0)
            },
        )
    )
    db_session.commit()

    body = client.post(
        "/cost/part", json={"part": WORKED_PART, "rate_set_id": "euro-shop"}
    ).json()

    assert body["currency"] == "EUR"
    assert body["rate_set"]["currency"] == "EUR"


def test_currency_is_persisted_and_returned_with_the_estimate():
    estimate_id = client.post("/cost/part", json=WORKED_REQUEST).json()["estimate_id"]

    assert client.get(f"/estimates/{estimate_id}").json()["currency"] == "INR"
    assert client.get("/estimates").json()["items"][0]["currency"] == "INR"


def test_comparison_reports_currency_on_both_sides():
    first = client.post("/cost/part", json=WORKED_REQUEST).json()["estimate_id"]
    second = client.post("/cost/part", json=WORKED_REQUEST).json()["estimate_id"]

    body = client.get(
        "/estimates/compare", params={"baseline": first, "comparison": second}
    ).json()

    assert body["baseline"]["currency"] == "INR"
    assert body["comparison"]["currency"] == "INR"


def test_a_stored_estimate_keeps_its_currency_when_a_later_version_changes_it(db_session):
    """Historical estimates are denominated once, at creation."""
    estimate_id = client.post("/cost/part", json=WORKED_REQUEST).json()["estimate_id"]
    before = client.get(f"/estimates/{estimate_id}").json()

    repository = RateSetRepository(db_session)
    original = repository.get("poc-machine-shop", "1.0.0")
    repository.add(original.model_copy(update={"version": "9.0.0", "currency": "EUR"}))
    db_session.commit()
    assert repository.get("poc-machine-shop").currency == "EUR"

    assert client.get(f"/estimates/{estimate_id}").json() == before
    assert client.get(f"/estimates/{estimate_id}").json()["currency"] == "INR"


# --- per-unit cost ----------------------------------------------------------


def test_per_unit_cost_is_the_batch_total_divided_by_quantity():
    body = client.post("/cost/part", json=WORKED_REQUEST).json()

    assert body["total_part_cost"] == 1363.20
    assert body["per_unit_cost"] == round_money(1363.20 / 4)
    assert body["per_unit_cost"] == 340.80
    assert body["per_unit_formula"]


def test_per_unit_cost_for_a_single_part_equals_the_batch_total():
    body = client.post(
        "/cost/part",
        json={
            **WORKED_REQUEST,
            "part": {**WORKED_PART, "quantity": 1},
            "operation_requirements": [{**WORKED_REQUIREMENT, "quantity": 1}],
        },
    ).json()

    assert body["per_unit_cost"] == body["total_part_cost"]


def test_per_unit_cost_does_not_apply_quantity_a_second_time():
    """Quantity is undone exactly once; per-unit must not scale with the batch."""
    def per_unit(quantity: int) -> float:
        return client.post(
            "/cost/part",
            json={
                "part": {**WORKED_PART, "quantity": quantity},
                "operations": [
                    {
                        "operation_id": "OP1",
                        "operation_name": "Milling",
                        "process_type": "Milling",
                        "machine_rate_per_hour": 1200.0,
                        "labor_rate_per_hour": 600.0,
                        "setup_time_minutes": 0.0,  # no batch-level cost to amortise
                        "cycle_time_minutes": 3.0,
                        "quantity": quantity,
                    }
                ],
            },
        ).json()["per_unit_cost"]

    # With no setup cost, per-unit cost is quantity-independent.
    assert per_unit(1) == pytest.approx(per_unit(10), abs=0.01)
    assert per_unit(1) == pytest.approx(per_unit(100), abs=0.01)


def test_per_unit_cost_amortises_one_time_batch_costs():
    """Setup and tooling are per batch, so per-unit cost falls as quantity rises."""
    def per_unit(quantity: int) -> float:
        return client.post(
            "/cost/part",
            json={
                "part": {**WORKED_PART, "quantity": quantity},
                "operation_requirements": [{**WORKED_REQUIREMENT, "quantity": quantity}],
                "rate_set_id": "poc-machine-shop",
                "rate_set_version": "1.0.0",
            },
        ).json()["per_unit_cost"]

    assert per_unit(1) > per_unit(10) > per_unit(100)


def test_per_unit_cost_is_present_on_the_cad_driven_path():
    import trimesh

    stl = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")
    body = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", stl, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "4",
            "rate_set_id": "poc-machine-shop",
            "rate_set_version": "1.0.0",
        },
    ).json()

    part_cost = body["part_cost"]
    assert part_cost["currency"] == "INR"
    assert part_cost["per_unit_cost"] == round_money(part_cost["total_part_cost"] / 4)


def test_per_unit_cost_of_a_zero_cost_estimate_is_zero(db_session):
    """A free rate set is still a valid estimate, not a division problem."""
    repository = RateSetRepository(db_session)
    repository.add(
        RateSet(
            rate_set_id="free-shop",
            name="Free Shop",
            version="1.0.0",
            materials={"steel": MaterialRate(name="Steel", cost_per_kg=1e-9, density_kg_m3=7850)},
        )
    )
    db_session.commit()

    body = client.post(
        "/cost/part",
        json={
            "part": {**WORKED_PART, "mass_kg": 1e-9, "quantity": 5},
            "rate_set_id": "free-shop",
        },
    ).json()

    assert body["total_part_cost"] == 0.0
    assert body["per_unit_cost"] == 0.0


# --- migration of a populated database -------------------------------------


def _pre_currency_engine():
    """A database at revision 0001 — before currency existed — with data in it."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE rate_sets DROP COLUMN currency"))
        connection.execute(text("ALTER TABLE estimates DROP COLUMN currency"))
        # Revision 0001 predates every later column and table, not just currency.
        # Anything a later revision adds has to come back off here too, or
        # replaying the migrations onto this database tries to create something
        # it already has.
        connection.execute(text("ALTER TABLE estimates DROP COLUMN plan_snapshot"))
        connection.execute(text("ALTER TABLE estimates DROP COLUMN time_profile_snapshot"))
        connection.execute(text("DROP TABLE IF EXISTS time_profiles"))
        connection.execute(text("DROP TABLE IF EXISTS quotations"))
        connection.execute(text("DROP TABLE IF EXISTS cost_actuals"))
        connection.execute(text("UPDATE alembic_version SET version_num = '0001'"))
    return engine


def test_upgrading_a_populated_pre_currency_database_backfills_inr():
    engine = _pre_currency_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    # Populate it the way revision 0001 would have.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO rate_sets (rate_set_id, version, name, description,"
                " is_illustrative, rates, created_at) VALUES"
                " ('legacy-set','1.0.0','Legacy','', 1, '{\"materials\": {},"
                ' "operations": {}, "additional_costs": {"overhead_rate_percent": 0.0,'
                ' "scrap_rate_percent": 0.0, "tooling_cost_per_batch": 0.0}}\','
                " '2026-01-01 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO estimates (estimate_id, created_at, source, part_id, part_name,"
                " material, quantity, total_material_cost, total_operation_cost,"
                " total_additional_cost, total_part_cost, request_snapshot, result_snapshot)"
                " VALUES ('legacyestimate','2026-01-01 00:00:00','cost/part','LEGACY','Flange',"
                " 'Steel', 4, 8.0, 960.0, 395.2, 1363.2, '{}', '{}')"
            )
        )

    assert "currency" not in {c["name"] for c in inspect(engine).get_columns("estimates")}

    # The upgrade path adds the columns and backfills every existing row.
    assert run_migrations(engine) == "upgraded"

    with session_factory() as session:
        rate_set_row = session.execute(select(RateSetRow)).scalars().one()
        estimate_row = session.execute(select(EstimateRow)).scalars().one()

    assert rate_set_row.rate_set_id == "legacy-set"
    assert rate_set_row.currency == "INR"
    assert estimate_row.estimate_id == "legacyestimate"
    assert estimate_row.currency == "INR"
    # Every pre-existing value survives untouched.
    assert estimate_row.total_part_cost == pytest.approx(1363.2)
    assert estimate_row.part_id == "LEGACY"


def test_a_backfilled_database_serves_its_history_normally():
    engine = _pre_currency_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO estimates (estimate_id, created_at, source, part_id, part_name,"
                " material, quantity, total_material_cost, total_operation_cost,"
                " total_additional_cost, total_part_cost, request_snapshot, result_snapshot)"
                " VALUES ('legacyestimate','2026-01-01 00:00:00','cost/part','LEGACY','Flange',"
                " 'Steel', 4, 8.0, 960.0, 395.2, 1363.2, '{}', '{}')"
            )
        )
    init_db(engine)

    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        stored = EstimateRepository(session).get("legacyestimate")
        page = EstimateRepository(session).list(limit=10)

    assert stored.currency == "INR"
    assert stored.total_part_cost == pytest.approx(1363.2)
    assert page.items[0].currency == "INR"


def test_seeded_rate_sets_are_stored_as_inr(db_session):
    for row in db_session.execute(select(RateSetRow)).scalars().all():
        assert row.currency == "INR"


def test_additional_cost_inputs_are_unchanged_by_the_money_policy():
    """The costing inputs themselves are untouched — only presentation changed."""
    inputs = AdditionalCostInputs(overhead_rate_percent=12.345)

    assert inputs.overhead_rate_percent == 12.345
