"""Actual costs recorded against an estimate, and the variance between them.

The rules under test: only the total is required; a category not supplied is
reported as not supplied and never as zero; the cost basis is never guessed; a
currency is never converted; a record is never edited, only superseded; and an
assembly with no complete total is never compared against the 0.0 its row holds.
"""

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.pool import StaticPool

from cad_costing_agent import cost_actuals
from cad_costing_agent.cost_actuals import (
    ActualCostInput,
    ActualCostRecord,
    EstimateFigures,
    compute_variance,
    figures_from_estimate,
)
from cad_costing_agent.db import _alembic_config, run_migrations
from cad_costing_agent.main import app
from cad_costing_agent.repository import CostActualRepository, EstimateRepository

client = TestClient(app)

PACKAGE = Path(cost_actuals.__file__).resolve().parent
STATIC = PACKAGE / "static"

# Part estimate: 4 units. Material 8.00, operations 960.00, additional 0.00,
# total 968.00 — so 2.00, 240.00, 0.00 and 242.00 per unit.
PART_FIGURES = EstimateFigures(
    estimate_id="e1",
    kind="part",
    currency="INR",
    quantity=4,
    total=968.0,
    material=8.0,
    machining=960.0,
    additional=0.0,
)


def record(**overrides) -> ActualCostRecord:
    values = dict(
        actual_id="a1",
        estimate_id="e1",
        recorded_at="2026-10-19T10:00:00",
        source="Client ERP actual",
        cost_basis="per_unit",
        currency="INR",
        total_cost=200.0,
    )
    values.update(overrides)
    return ActualCostRecord(**values)


def line(variance, category):
    return next(item for item in variance.lines if item.category == category)


# --- the input is never guessed ------------------------------------------------


def test_the_cost_basis_has_no_default():
    with pytest.raises(ValidationError):
        ActualCostInput(source="ERP", currency="INR", total_cost=10.0)


def test_a_per_batch_cost_without_its_batch_quantity_is_refused():
    with pytest.raises(ValidationError, match="batch quantity"):
        ActualCostInput(source="ERP", cost_basis="per_batch", currency="INR", total_cost=10.0)


def test_a_source_is_required_and_blank_is_not_a_source():
    with pytest.raises(ValidationError):
        ActualCostInput(source="   ", cost_basis="per_unit", currency="INR", total_cost=10.0)


@pytest.mark.parametrize("total", [0.0, -5.0, float("inf"), float("nan")])
def test_a_total_must_be_a_positive_finite_amount(total):
    with pytest.raises(ValidationError):
        ActualCostInput(source="ERP", cost_basis="per_unit", currency="INR", total_cost=total)


def test_a_breakdown_larger_than_its_total_is_refused():
    with pytest.raises(ValidationError, match="more than the total"):
        ActualCostInput(
            source="ERP",
            cost_basis="per_unit",
            currency="INR",
            total_cost=100.0,
            material_cost=60.0,
            machining_cost=50.0,
        )


def test_a_breakdown_equal_to_its_total_is_accepted():
    entry = ActualCostInput(
        source="ERP",
        cost_basis="per_unit",
        currency="INR",
        total_cost=100.0,
        material_cost=60.0,
        machining_cost=40.0,
    )
    assert entry.additional_cost is None


# --- the variance -------------------------------------------------------------


def test_variance_is_estimate_minus_actual_relative_to_the_actual():
    variance = compute_variance(PART_FIGURES, record(total_cost=200.0, batch_quantity=4))

    total = line(variance, "total")
    assert variance.status == "available"
    assert total.state == "compared"
    assert total.estimate_per_unit == pytest.approx(242.0)
    assert total.actual_per_unit == pytest.approx(200.0)
    assert total.variance == pytest.approx(42.0)
    assert total.variance_percent == pytest.approx(21.0)


def test_a_per_batch_actual_is_normalised_by_its_own_batch_quantity():
    variance = compute_variance(
        PART_FIGURES,
        record(cost_basis="per_batch", batch_quantity=4, total_cost=1000.0, material_cost=40.0),
    )
    assert line(variance, "total").actual_per_unit == pytest.approx(250.0)
    assert line(variance, "material").actual_per_unit == pytest.approx(10.0)
    assert line(variance, "material").variance == pytest.approx(-8.0)


def test_a_category_not_supplied_is_not_supplied_and_never_zero():
    variance = compute_variance(PART_FIGURES, record(material_cost=5.0))

    machining = line(variance, "machining")
    assert machining.state == "not_supplied"
    assert machining.actual_per_unit is None
    assert machining.variance is None
    assert machining.variance_percent is None
    # The estimate's own figure is still shown beside the gap.
    assert machining.estimate_per_unit == pytest.approx(240.0)


def test_a_supplied_zero_is_compared_but_has_no_percentage():
    variance = compute_variance(PART_FIGURES, record(additional_cost=0.0))

    additional = line(variance, "additional")
    assert additional.state == "compared"
    assert additional.variance == pytest.approx(0.0)
    assert additional.variance_percent is None
    assert "zero" in additional.note


def test_the_part_of_the_total_no_category_covers_is_reported_not_assigned():
    variance = compute_variance(
        PART_FIGURES, record(total_cost=200.0, material_cost=20.0, machining_cost=150.0)
    )
    assert variance.unattributed_actual_per_unit == pytest.approx(30.0)


def test_unattributed_is_absent_when_no_category_was_supplied():
    assert compute_variance(PART_FIGURES, record()).unattributed_actual_per_unit is None


def test_a_currency_mismatch_is_unavailable_and_nothing_is_converted():
    variance = compute_variance(PART_FIGURES, record(currency="USD"))
    assert variance.status == "unavailable"
    assert variance.unavailable_code == "currency_mismatch"
    assert variance.lines == []


def test_a_different_batch_size_is_warned_about_and_not_adjusted():
    variance = compute_variance(PART_FIGURES, record(batch_quantity=50, total_cost=200.0))
    assert any("batch of 4" in warning and "50" in warning for warning in variance.warnings)
    assert line(variance, "total").actual_per_unit == pytest.approx(200.0)


def test_an_unstated_batch_size_is_warned_about():
    variance = compute_variance(PART_FIGURES, record())
    assert any("does not state its batch size" in warning for warning in variance.warnings)


def test_matching_batch_sizes_carry_no_batch_warning():
    assert compute_variance(PART_FIGURES, record(batch_quantity=4)).warnings == []


def test_the_variance_is_deterministic():
    entry = record(material_cost=3.0, machining_cost=100.0, batch_quantity=7)
    first = compute_variance(PART_FIGURES, entry).model_dump()
    second = compute_variance(PART_FIGURES, entry).model_dump()
    assert first == second


# --- assemblies ---------------------------------------------------------------


def assembly_figures(**snapshot):
    result = {
        "currency": "INR",
        "assembly_quantity": 2,
        "is_aggregatable": True,
        "is_complete": True,
        "production_total_cost": 500.0,
        "material_cost": 40.0,
    }
    result.update(snapshot)
    return figures_from_estimate(
        estimate_id="asm",
        source="cost/assembly",
        quantity=2,
        currency="INR",
        total_material_cost=80.0,
        total_operation_cost=0.0,
        total_additional_cost=0.0,
        total_part_cost=result["production_total_cost"] or 0.0,
        result_snapshot=result,
    )


def test_an_assembly_is_compared_on_its_total_only():
    variance = compute_variance(assembly_figures(), record(total_cost=200.0, material_cost=30.0))

    assert line(variance, "total").estimate_per_unit == pytest.approx(250.0)
    assert line(variance, "total").variance == pytest.approx(50.0)
    material = line(variance, "material")
    assert material.state == "not_comparable"
    assert material.variance is None
    assert "manufactured components only" in material.note
    assert line(variance, "machining").state == "not_supplied"


def test_an_incomplete_assembly_is_never_compared_with_its_stored_zero():
    figures = assembly_figures(is_complete=False, production_total_cost=None)
    variance = compute_variance(figures, record())

    assert figures.total is None
    assert variance.status == "unavailable"
    assert variance.unavailable_code == "assembly_incomplete"
    assert variance.lines == []


def test_a_mixed_currency_assembly_has_no_total_to_compare():
    variance = compute_variance(assembly_figures(is_aggregatable=False), record())
    assert variance.unavailable_code == "mixed_currency"


# --- structure ----------------------------------------------------------------


def test_the_variance_module_imports_only_the_money_policy():
    tree = ast.parse((PACKAGE / "cost_actuals.py").read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module.startswith("cad_costing_agent")
    }
    imported |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.startswith("cad_costing_agent")
    }
    assert imported == {"cad_costing_agent.money"}


def test_no_costing_module_reads_actual_costs():
    # Imports, parsed rather than grepped: the table is also called cost_actuals,
    # so a substring search matches the ORM model and the migration.
    def imports_it(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "cad_costing_agent.cost_actuals":
                return True
            if isinstance(node, ast.ImportFrom) and node.module == "cad_costing_agent":
                if any(alias.name == "cost_actuals" for alias in node.names):
                    return True
            if isinstance(node, ast.Import):
                if any(alias.name == "cad_costing_agent.cost_actuals" for alias in node.names):
                    return True
        return False

    readers = {path.name for path in PACKAGE.rglob("*.py") if imports_it(path)}
    assert readers == {"main.py", "repository.py"}


def test_the_actual_cost_repository_cannot_edit_or_delete():
    public = {name for name in vars(CostActualRepository) if not name.startswith("_")}
    assert public == {"record", "get", "for_estimate"}


def test_the_actuals_panel_works_nothing_out():
    source = (STATIC / "actuals.js").read_text(encoding="utf-8")
    code = re.sub(r"^\s*//.*$", "", source, flags=re.MULTILINE)
    # Arithmetic on a response field — a variance, a per-unit figure, a percentage.
    assert not re.search(r"[\w)\]]\s*[-*/]\s*(record|line|entry|variance|data)\.", code)
    assert not re.search(r"(record|line|entry|variance|data)\.\w+\s*[-*/]", code)
    assert "* 100" not in code


def test_a_blank_breakdown_field_is_sent_as_null_not_zero():
    source = (STATIC / "actuals.js").read_text(encoding="utf-8")
    assert 'raw === "" ? null : Number(raw)' in source
    assert 'material_cost: optionalNumber("actual-material")' in source


def test_the_actuals_panel_loads_after_the_globals_it_borrows():
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    assert markup.index('src="app.js"') < markup.index('src="actuals.js"')


# --- the API ------------------------------------------------------------------


PART_REQUEST = {
    "part": {"part_id": "P100", "part_name": "Flange", "material": "Steel", "mass_kg": 2.5,
             "quantity": 4},
    "operation_requirements": [
        {"operation_id": "OP1", "operation_name": "Milling", "process_type": "Milling",
         "setup_time_minutes": 30.0, "cycle_time_minutes": 3.0, "quantity": 4}
    ],
    "rate_set_id": "poc-machine-shop",
    "rate_set_version": "1.0.0",
}


def create_estimate() -> dict:
    response = client.post("/cost/part", json=PART_REQUEST)
    assert response.status_code == 200
    return response.json()


def post_actual(estimate_id, **overrides):
    body = {"source": "Client ERP actual", "cost_basis": "per_unit", "currency": "INR",
            "total_cost": 300.0}
    body.update(overrides)
    return client.post(f"/estimates/{estimate_id}/actuals", json=body)


def test_an_actual_is_recorded_and_compared_per_unit():
    estimate = create_estimate()
    response = post_actual(
        estimate["estimate_id"], cost_basis="per_batch", batch_quantity=4, total_cost=1200.0
    )

    assert response.status_code == 201
    entry = response.json()
    assert entry["is_current"] is True
    total = next(item for item in entry["variance"]["lines"] if item["category"] == "total")
    assert total["estimate_per_unit"] == pytest.approx(estimate["per_unit_cost"], abs=0.01)
    assert total["actual_per_unit"] == 300.0
    assert entry["variance"]["warnings"] == []


def test_recording_an_actual_changes_nothing_in_the_estimate():
    estimate_id = create_estimate()["estimate_id"]
    before = client.get(f"/estimates/{estimate_id}").json()

    post_actual(estimate_id, material_cost=10.0)

    assert client.get(f"/estimates/{estimate_id}").json() == before


def test_blank_categories_come_back_not_supplied():
    estimate_id = create_estimate()["estimate_id"]
    entry = post_actual(estimate_id, material_cost=None, machining_cost=None).json()

    states = {item["category"]: item["state"] for item in entry["variance"]["lines"]}
    assert states == {"total": "compared", "material": "not_supplied",
                      "machining": "not_supplied", "additional": "not_supplied"}
    assert entry["record"]["material_cost"] is None


def test_the_client_part_number_and_drawing_revision_are_kept_as_entered():
    estimate_id = create_estimate()["estimate_id"]
    record = post_actual(
        estimate_id, client_part_number=" FL-100 ", drawing_revision="C", cost_date="2025-03-31"
    ).json()["record"]
    assert record["client_part_number"] == "FL-100"
    assert record["drawing_revision"] == "C"
    assert record["cost_date"] == "2025-03-31"


def test_an_unknown_estimate_is_404():
    assert post_actual("nope").status_code == 404
    assert client.get("/estimates/nope/actuals").status_code == 404


def test_a_currency_mismatch_is_refused_and_nothing_is_stored():
    estimate_id = create_estimate()["estimate_id"]
    response = post_actual(estimate_id, currency="USD")

    assert response.status_code == 422
    assert "never converted" in response.json()["detail"]
    assert client.get(f"/estimates/{estimate_id}/actuals").json()["entries"] == []


def test_a_per_batch_actual_without_a_quantity_is_refused():
    estimate_id = create_estimate()["estimate_id"]
    assert post_actual(estimate_id, cost_basis="per_batch").status_code == 422


def test_a_correction_is_a_new_record_and_the_original_is_kept():
    estimate_id = create_estimate()["estimate_id"]
    original = post_actual(estimate_id, total_cost=300.0).json()["record"]

    corrected = post_actual(
        estimate_id, total_cost=310.0, supersedes_actual_id=original["actual_id"]
    )
    assert corrected.status_code == 201

    entries = client.get(f"/estimates/{estimate_id}/actuals").json()["entries"]
    assert [entry["record"]["total_cost"] for entry in entries] == [300.0, 310.0]
    assert entries[0]["is_current"] is False
    assert entries[0]["superseded_by"] == corrected.json()["record"]["actual_id"]
    assert entries[0]["record"] == original
    assert entries[1]["is_current"] is True


def test_a_record_is_corrected_at_most_once():
    estimate_id = create_estimate()["estimate_id"]
    original = post_actual(estimate_id).json()["record"]["actual_id"]
    assert post_actual(estimate_id, supersedes_actual_id=original).status_code == 201

    again = post_actual(estimate_id, supersedes_actual_id=original)
    assert again.status_code == 409
    assert len(client.get(f"/estimates/{estimate_id}/actuals").json()["entries"]) == 2


def test_a_record_from_another_estimate_cannot_be_corrected_here():
    first = create_estimate()["estimate_id"]
    second = create_estimate()["estimate_id"]
    foreign = post_actual(first).json()["record"]["actual_id"]

    assert post_actual(second, supersedes_actual_id=foreign).status_code == 422
    assert post_actual(second, supersedes_actual_id="missing").status_code == 422


def test_an_incomplete_assembly_estimate_takes_an_actual_but_reports_no_variance(db_session):
    estimate_id = EstimateRepository(db_session).save(
        source="cost/assembly",
        part_id="PUMP",
        part_name="Pump",
        material="Steel",
        quantity=1,
        rate_set=None,
        currency="INR",
        total_material_cost=0.0,
        total_operation_cost=0.0,
        total_additional_cost=0.0,
        total_part_cost=0.0,
        request_snapshot={},
        result_snapshot={"currency": "INR", "assembly_quantity": 1, "is_complete": False,
                         "is_aggregatable": True, "total_cost": None,
                         "production_total_cost": None},
    )
    db_session.commit()

    entry = post_actual(estimate_id).json()
    assert entry["variance"]["status"] == "unavailable"
    assert entry["variance"]["unavailable_code"] == "assembly_incomplete"
    listing = client.get(f"/estimates/{estimate_id}/actuals").json()
    assert listing["estimate_kind"] == "assembly"


# --- the migration ------------------------------------------------------------


def test_upgrading_a_0005_database_keeps_its_estimates_and_adds_the_table():
    from alembic import command

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        command.upgrade(_alembic_config(connection), "0005")
    from sqlalchemy.orm import sessionmaker

    with sessionmaker(bind=engine)() as session:
        estimate_id = EstimateRepository(session).save(
            source="cost/part", part_id="OLD", part_name="Flange", material="Steel",
            quantity=2, rate_set=None, currency="INR", total_material_cost=1.0,
            total_operation_cost=2.0, total_additional_cost=0.0, total_part_cost=3.0,
            request_snapshot={"before": "0006"}, result_snapshot={},
        )
        session.commit()
    assert "cost_actuals" not in inspect(engine).get_table_names()

    assert run_migrations(engine) == "upgraded"

    assert "cost_actuals" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == "0006"
    with sessionmaker(bind=engine)() as session:
        kept = EstimateRepository(session).get(estimate_id)
        assert kept.request_snapshot == {"before": "0006"}
        assert kept.total_part_cost == 3.0
        assert CostActualRepository(session).for_estimate(estimate_id) == []
