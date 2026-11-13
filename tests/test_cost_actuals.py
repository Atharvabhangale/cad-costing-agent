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
        figure_kind="actual",
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
        ActualCostInput(figure_kind="actual", source="ERP", currency="INR", total_cost=10.0)


def test_a_per_batch_cost_without_its_batch_quantity_is_refused():
    with pytest.raises(ValidationError, match="batch quantity"):
        ActualCostInput(figure_kind="actual", source="ERP", cost_basis="per_batch", currency="INR", total_cost=10.0)


def test_a_source_is_required_and_blank_is_not_a_source():
    with pytest.raises(ValidationError):
        ActualCostInput(figure_kind="actual", source="   ", cost_basis="per_unit", currency="INR", total_cost=10.0)


@pytest.mark.parametrize("total", [0.0, -5.0, float("inf"), float("nan")])
def test_a_total_must_be_a_positive_finite_amount(total):
    with pytest.raises(ValidationError):
        ActualCostInput(figure_kind="actual", source="ERP", cost_basis="per_unit", currency="INR", total_cost=total)


def test_a_breakdown_larger_than_its_total_is_refused():
    with pytest.raises(ValidationError, match="more than the total"):
        ActualCostInput(
            figure_kind="actual",
            source="ERP",
            cost_basis="per_unit",
            currency="INR",
            total_cost=100.0,
            material_cost=60.0,
            machining_cost=50.0,
        )


def test_a_breakdown_equal_to_its_total_is_accepted():
    entry = ActualCostInput(
        figure_kind="actual",
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
    body = {"figure_kind": "actual", "source": "Client ERP actual", "cost_basis": "per_unit", "currency": "INR",
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
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == "0010"
    with sessionmaker(bind=engine)() as session:
        kept = EstimateRepository(session).get(estimate_id)
        assert kept.request_snapshot == {"before": "0006"}
        assert kept.total_part_cost == 3.0
        assert CostActualRepository(session).for_estimate(estimate_id) == []


# --- what kind of figure: an actual or a supplier quote -----------------------


def quote(**overrides) -> ActualCostRecord:
    values = dict(figure_kind="supplier_quote", source="RFQ 2026-11", supplier_name="Acme")
    values.update(overrides)
    return record(**values)


def test_the_figure_kind_has_no_default():
    with pytest.raises(ValidationError, match="figure_kind"):
        ActualCostInput(source="ERP", cost_basis="per_unit", currency="INR", total_cost=10.0)


def test_an_unknown_figure_kind_is_refused():
    with pytest.raises(ValidationError):
        ActualCostInput(
            figure_kind="estimate", source="ERP", cost_basis="per_unit", currency="INR",
            total_cost=10.0,
        )


@pytest.mark.parametrize("field", ["supplier_name", "quote_reference"])
def test_a_supplier_field_on_an_actual_is_refused(field):
    with pytest.raises(ValidationError, match="belongs to a supplier quote"):
        ActualCostInput(
            figure_kind="actual", source="ERP", cost_basis="per_unit", currency="INR",
            total_cost=10.0, **{field: "Q-17"},
        )


def test_a_quote_needs_neither_supplier_field_and_keeps_them_as_entered():
    bare = ActualCostInput(
        figure_kind="supplier_quote", source="RFQ", cost_basis="per_unit", currency="INR",
        total_cost=10.0,
    )
    assert bare.supplier_name is None and bare.quote_reference is None
    named = ActualCostInput(
        figure_kind="supplier_quote", source="RFQ", cost_basis="per_unit", currency="INR",
        total_cost=10.0, supplier_name="  acme Castings pvt  ", quote_reference=" q/17-b ",
    )
    # Trimmed at the ends and otherwise untouched: no case folding, no matching.
    assert named.supplier_name == "acme Castings pvt"
    assert named.quote_reference == "q/17-b"


def test_a_quote_gap_is_quote_minus_should_cost_relative_to_the_should_cost():
    result = compute_variance(PART_FIGURES, quote(total_cost=300.0, batch_quantity=4))

    total = line(result, "total")
    assert result.figure_kind == "supplier_quote"
    assert total.state == "compared"
    assert total.estimate_per_unit == pytest.approx(242.0)
    assert total.quote_per_unit == pytest.approx(300.0)
    assert total.gap == pytest.approx(58.0)
    assert total.gap_percent == pytest.approx(58.0 / 242.0 * 100)
    assert total.statement == "Quote is 58.00 INR above should-cost (23.97% of should-cost)."


def test_a_quote_below_should_cost_says_below_and_one_equal_says_equal():
    below = line(compute_variance(PART_FIGURES, quote(total_cost=200.0)), "total")
    assert below.gap == pytest.approx(-42.0)
    assert below.statement == "Quote is 42.00 INR below should-cost (17.36% of should-cost)."
    equal = line(compute_variance(PART_FIGURES, quote(total_cost=242.0)), "total")
    assert equal.statement == "Quote equals should-cost."


def test_a_quote_never_fills_the_actual_fields_nor_an_actual_the_quote_fields():
    quoted = compute_variance(
        PART_FIGURES, quote(total_cost=300.0, material_cost=5.0, machining_cost=250.0)
    )
    for item in quoted.lines:
        assert item.actual_per_unit is None
        assert item.variance is None
        assert item.variance_percent is None
    assert quoted.unattributed_actual_per_unit is None
    assert quoted.unattributed_quote_per_unit == pytest.approx(45.0)

    actual = compute_variance(
        PART_FIGURES, record(total_cost=300.0, material_cost=5.0, machining_cost=250.0)
    )
    for item in actual.lines:
        assert item.quote_per_unit is None
        assert item.gap is None
        assert item.gap_percent is None
    assert actual.unattributed_quote_per_unit is None
    assert actual.unattributed_actual_per_unit == pytest.approx(45.0)


def test_an_actual_keeps_its_figures_and_its_wording():
    result = compute_variance(PART_FIGURES, record(total_cost=200.0, batch_quantity=4))
    total = line(result, "total")
    assert result.figure_kind == "actual"
    assert result.basis == cost_actuals.VARIANCE_BASIS
    assert total.variance == pytest.approx(42.0)
    assert total.variance_percent == pytest.approx(21.0)
    assert total.statement == "Estimate is 42.00 INR above the actual (21.00% of the actual)."


def test_the_wording_differs_by_kind():
    quoted = compute_variance(PART_FIGURES, quote(batch_quantity=50))
    actual = compute_variance(PART_FIGURES, record(batch_quantity=50))

    assert quoted.basis == cost_actuals.QUOTE_GAP_BASIS != actual.basis
    assert "the supplier quote for 50" in quoted.warnings[0]
    assert "this gap" in quoted.warnings[0]
    assert "the actual for 50" in actual.warnings[0]
    assert "this variance" in actual.warnings[0]
    assert line(quoted, "material").note.startswith("Not itemised in the supplier quote")
    assert line(actual, "material").note.startswith("Not supplied in the actual cost record")


def test_an_un_itemised_quote_category_is_not_supplied_and_never_zero():
    result = compute_variance(PART_FIGURES, quote(total_cost=300.0, material_cost=5.0))

    machining = line(result, "machining")
    assert machining.state == "not_supplied"
    assert machining.quote_per_unit is None
    assert machining.gap is None
    assert machining.gap_percent is None
    assert machining.statement == ""
    assert machining.estimate_per_unit == pytest.approx(240.0)
    assert line(result, "material").gap == pytest.approx(3.0)


def test_a_quote_against_a_zero_should_cost_figure_has_a_gap_but_no_percentage():
    additional = line(compute_variance(PART_FIGURES, quote(additional_cost=12.0)), "additional")
    assert additional.state == "compared"
    assert additional.gap == pytest.approx(12.0)
    assert additional.gap_percent is None
    assert "should-cost figure is zero" in additional.note
    assert additional.statement == "Quote is 12.00 INR above should-cost."


def test_a_quote_for_an_assembly_is_compared_on_its_total_only():
    result = compute_variance(assembly_figures(), quote(total_cost=300.0, material_cost=30.0))
    assert line(result, "total").gap == pytest.approx(50.0)
    material = line(result, "material")
    assert material.state == "not_comparable"
    assert material.quote_per_unit == pytest.approx(30.0)
    assert material.gap is None
    assert line(result, "machining").state == "not_supplied"


def test_no_wording_passes_a_verdict_on_the_supplier():
    # Every sentence a quote can produce, and the constants, read for a judgement.
    spoken = [cost_actuals.QUOTE_GAP_BASIS, cost_actuals.QUOTE_ORDER_NOTE]
    for entry in (
        quote(total_cost=900.0, material_cost=50.0, batch_quantity=50),
        quote(total_cost=100.0, additional_cost=1.0),
        quote(total_cost=242.0),
    ):
        result = compute_variance(PART_FIGURES, entry)
        spoken += result.warnings
        spoken += [item.statement for item in result.lines]
        spoken += [item.note for item in result.lines]
    text_ = " ".join(spoken).lower()
    for word in ("overcharg", "overpric", "too high", "too low", "cheapest", "best", "worst",
                 "unfair", "excessive", "should pay", "recommend"):
        assert word not in text_, word


# --- several quotes, side by side ---------------------------------------------


def test_quotes_are_listed_in_the_order_entered_and_never_by_price():
    records = [
        quote(actual_id="q1", supplier_name="Dear", total_cost=400.0),
        record(actual_id="a1", total_cost=200.0),
        quote(actual_id="q2", supplier_name="Cheap", total_cost=100.0),
        quote(actual_id="q3", supplier_name="Middle", total_cost=250.0, material_cost=4.0),
    ]
    comparison = cost_actuals.compare_supplier_quotes(PART_FIGURES, records)

    assert [column.supplier_name for column in comparison.quotes] == ["Dear", "Cheap", "Middle"]
    assert comparison.order == "as_entered"
    assert comparison.ranked is False
    assert comparison.preferred is None
    total = next(row for row in comparison.rows if row.category == "total")
    assert total.estimate_per_unit == pytest.approx(242.0)
    assert [cell.actual_id for cell in total.cells] == ["q1", "q2", "q3"]
    assert [cell.gap for cell in total.cells] == pytest.approx([158.0, -142.0, 8.0])
    material = next(row for row in comparison.rows if row.category == "material")
    assert [cell.state for cell in material.cells] == ["not_supplied", "not_supplied", "compared"]
    assert material.cells[0].quote_per_unit is None
    assert material.cells[2].gap == pytest.approx(2.0)


def test_the_side_by_side_table_leaves_out_actuals_and_superseded_quotes():
    records = [
        quote(actual_id="q1", total_cost=400.0),
        record(actual_id="a1", total_cost=200.0),
        quote(actual_id="q2", total_cost=390.0, supersedes_actual_id="q1"),
    ]
    comparison = cost_actuals.compare_supplier_quotes(PART_FIGURES, records)
    assert [column.actual_id for column in comparison.quotes] == ["q2"]


def test_no_quotes_gives_an_empty_table_not_a_row_of_blanks():
    comparison = cost_actuals.compare_supplier_quotes(PART_FIGURES, [record()])
    assert comparison.quotes == [] and comparison.rows == []


def test_quotes_against_an_estimate_with_no_total_are_listed_without_figures():
    figures = assembly_figures(is_complete=False, production_total_cost=None)
    comparison = cost_actuals.compare_supplier_quotes(figures, [quote(actual_id="q1")])
    assert comparison.quotes[0].status == "unavailable"
    assert all(cell.state == "unavailable" for row in comparison.rows for cell in row.cells)
    assert all(cell.gap is None for row in comparison.rows for cell in row.cells)


def test_the_quote_comparison_does_not_sort_or_pick():
    # Unparse the two functions, so the docstrings saying what they do not do
    # cannot match.
    tree = ast.parse((PACKAGE / "cost_actuals.py").read_text(encoding="utf-8"))
    for name in ("compare_supplier_quotes", "current_records"):
        function = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
        )
        if isinstance(function.body[0], ast.Expr):
            function.body = function.body[1:]
        code = ast.unparse(function)
        for forbidden in ("sorted(", ".sort(", "min(", "max(", "reversed(", "rank"):
            assert forbidden not in code, (name, forbidden)


def test_every_reader_of_the_table_keeps_the_kinds_apart():
    # The readers of the table are the repository and the two endpoints. None
    # may add records together: a sum or mean over rows is where a quote would
    # be averaged into an "actual" variance.
    def code_of(path, names):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
        ]
        assert {node.name for node in found} == set(names)
        return "\n".join(ast.unparse(node) for node in found)

    code = code_of(PACKAGE / "repository.py", {"CostActualRepository"}) + code_of(
        PACKAGE / "main.py", {"_entries", "record_actual_cost", "estimate_actual_costs"}
    )
    for forbidden in ("sum(", "mean(", "median(", "func.avg", "func.sum"):
        assert forbidden not in code, forbidden

    # And nothing else in the package touches the ORM row.
    users = set()
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(node, ast.Name) and node.id == "CostActualRow" for node in ast.walk(tree)):
            users.add(path.name)
    assert users == {"repository.py"}


# --- the API, for a quote -----------------------------------------------------


def post_quote(estimate_id, **overrides):
    body = {"figure_kind": "supplier_quote", "source": "RFQ 2026-11", "supplier_name": "Acme"}
    body.update(overrides)
    return post_actual(estimate_id, **body)


def test_a_payload_without_a_figure_kind_is_refused_and_nothing_is_stored():
    # The payload every caller sent before the field existed.
    estimate_id = create_estimate()["estimate_id"]
    response = client.post(
        f"/estimates/{estimate_id}/actuals",
        json={"source": "Client ERP actual", "cost_basis": "per_unit", "currency": "INR",
              "total_cost": 300.0},
    )
    assert response.status_code == 422
    assert any(error["loc"][-1] == "figure_kind" for error in response.json()["detail"])
    assert client.get(f"/estimates/{estimate_id}/actuals").json()["entries"] == []


def test_a_supplier_quote_is_recorded_and_compared_as_a_gap():
    estimate = create_estimate()
    per_unit = estimate["per_unit_cost"]
    response = post_quote(
        estimate["estimate_id"], total_cost=round(per_unit + 50.0, 2), quote_reference="Q-17",
        batch_quantity=4,
    )

    assert response.status_code == 201
    entry = response.json()
    assert entry["record"]["figure_kind"] == "supplier_quote"
    assert entry["record"]["supplier_name"] == "Acme"
    assert entry["record"]["quote_reference"] == "Q-17"
    assert entry["variance"]["figure_kind"] == "supplier_quote"
    total = next(item for item in entry["variance"]["lines"] if item["category"] == "total")
    assert total["gap"] == pytest.approx(50.0, abs=0.01)
    assert total["variance"] is None
    assert total["actual_per_unit"] is None
    assert "above should-cost" in total["statement"]


def test_recording_a_quote_changes_nothing_in_the_estimate():
    estimate_id = create_estimate()["estimate_id"]
    before = client.get(f"/estimates/{estimate_id}").json()
    assert post_quote(estimate_id, material_cost=10.0).status_code == 201
    assert client.get(f"/estimates/{estimate_id}").json() == before


def test_a_supplier_name_on_an_actual_is_refused_by_the_api():
    estimate_id = create_estimate()["estimate_id"]
    assert post_actual(estimate_id, supplier_name="Acme").status_code == 422
    assert client.get(f"/estimates/{estimate_id}/actuals").json()["entries"] == []


def test_the_listing_sets_quotes_side_by_side_in_the_order_entered():
    estimate_id = create_estimate()["estimate_id"]
    post_quote(estimate_id, supplier_name="Dear", total_cost=900.0)
    post_actual(estimate_id, total_cost=310.0)
    post_quote(estimate_id, supplier_name="Cheap", total_cost=100.0)
    post_quote(estimate_id, supplier_name=None, source="Phone quote", total_cost=500.0)

    listing = client.get(f"/estimates/{estimate_id}/actuals").json()
    assert [entry["record"]["figure_kind"] for entry in listing["entries"]] == [
        "supplier_quote", "actual", "supplier_quote", "supplier_quote"
    ]
    side = listing["supplier_quotes"]
    assert [column["supplier_name"] for column in side["quotes"]] == ["Dear", "Cheap", None]
    assert side["quotes"][2]["source"] == "Phone quote"
    assert side["ranked"] is False and side["preferred"] is None
    assert side["order"] == "as_entered"
    total = next(row for row in side["rows"] if row["category"] == "total")
    assert [cell["quote_per_unit"] for cell in total["cells"]] == [900.0, 100.0, 500.0]
    material = next(row for row in side["rows"] if row["category"] == "material")
    assert {cell["state"] for cell in material["cells"]} == {"not_supplied"}
    assert all(cell["quote_per_unit"] is None for cell in material["cells"])


def test_a_quote_is_corrected_by_a_new_record_and_the_original_is_kept():
    estimate_id = create_estimate()["estimate_id"]
    original = post_quote(estimate_id, total_cost=400.0).json()["record"]
    corrected = post_quote(
        estimate_id, total_cost=380.0, supersedes_actual_id=original["actual_id"]
    )
    assert corrected.status_code == 201
    assert post_quote(estimate_id, supersedes_actual_id=original["actual_id"]).status_code == 409

    listing = client.get(f"/estimates/{estimate_id}/actuals").json()
    assert listing["entries"][0]["record"] == original
    assert listing["entries"][0]["is_current"] is False
    assert [column["actual_id"] for column in listing["supplier_quotes"]["quotes"]] == [
        corrected.json()["record"]["actual_id"]
    ]


def test_a_correction_may_change_the_kind_and_the_original_still_says_what_was_entered():
    estimate_id = create_estimate()["estimate_id"]
    original = post_actual(estimate_id, total_cost=400.0).json()["record"]
    assert post_quote(
        estimate_id, total_cost=400.0, supersedes_actual_id=original["actual_id"]
    ).status_code == 201

    listing = client.get(f"/estimates/{estimate_id}/actuals").json()
    assert listing["entries"][0]["record"]["figure_kind"] == "actual"
    assert listing["entries"][0]["is_current"] is False
    assert listing["entries"][1]["record"]["figure_kind"] == "supplier_quote"
    assert len(listing["supplier_quotes"]["quotes"]) == 1


def test_the_quote_comparison_is_deterministic():
    estimate_id = create_estimate()["estimate_id"]
    post_quote(estimate_id, total_cost=400.0, material_cost=12.0)
    post_quote(estimate_id, total_cost=300.0)
    first = client.get(f"/estimates/{estimate_id}/actuals").json()
    assert client.get(f"/estimates/{estimate_id}/actuals").json() == first


# --- the UI, for a quote ------------------------------------------------------


def actuals_script_code() -> str:
    source = (STATIC / "actuals.js").read_text(encoding="utf-8")
    return re.sub(r"^\s*//.*$", "", source, flags=re.MULTILINE)


def test_the_kind_selector_preselects_nothing_and_is_sent():
    code = actuals_script_code()
    selector = code[code.index('<select id="actual-kind" required>'):]
    selector = selector[: selector.index("</select>")]
    assert selector.index('<option value="">') < selector.index('<option value="actual">')
    assert '<option value="supplier_quote">' in selector
    assert "selected" not in selector
    assert 'figure_kind: optionalText("actual-kind")' in code


def test_supplier_fields_are_sent_as_null_unless_the_record_is_a_quote():
    code = actuals_script_code()
    assert 'supplier_name: isQuote ? optionalText("actual-supplier") : null' in code
    assert 'quote_reference: isQuote ? optionalText("actual-quote-ref") : null' in code


def test_the_quote_table_is_the_servers_and_the_browser_neither_sorts_nor_subtracts():
    code = actuals_script_code()
    assert "supplierQuotesMarkup(data.supplier_quotes)" in code
    for forbidden in (".sort(", ".reverse(", "Math.min", "Math.max", ".reduce("):
        assert forbidden not in code, forbidden
    # Arithmetic on a quote's fields — a gap or a percentage worked out here.
    assert not re.search(r"(cell|row|quote|comparison)\.\w+\s*[-*/]", code)
    assert not re.search(r"[\w)\]]\s*[-*/]\s*(cell|row|quote|comparison)\.", code)


# --- migration 0008 -----------------------------------------------------------


def _engine_at(revision):
    from alembic import command

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        command.upgrade(_alembic_config(connection), revision)
    return engine


OLD_ROW = (
    "insert into cost_actuals (actual_id, estimate_id, recorded_at, source, cost_basis, "
    "batch_quantity, currency, total_cost, material_cost, machining_cost, additional_cost, "
    "cost_date, client_part_number, drawing_revision, notes, supersedes_actual_id) values "
)


def test_upgrading_a_0007_database_keeps_every_recorded_actual_and_calls_it_an_actual():
    engine = _engine_at("0007")
    with engine.begin() as connection:
        connection.execute(text(
            OLD_ROW + "('old1', 'e1', '2026-10-20 09:00:00', 'Client ERP actual', 'per_unit', "
            "NULL, 'INR', 300.0, NULL, 250.5, NULL, '2025-03-31', 'FL-100', 'C', 'first', NULL)"
        ))
        connection.execute(text(
            OLD_ROW + "('old2', 'e1', '2026-10-21 09:00:00', 'Corrected', 'per_batch', "
            "4, 'INR', 1240.0, 0.0, NULL, NULL, NULL, NULL, NULL, '', 'old1')"
        ))
        before = connection.execute(text("select * from cost_actuals order by id")).fetchall()
        columns_before = [column["name"] for column in inspect(connection).get_columns("cost_actuals")]
    assert "figure_kind" not in columns_before

    assert run_migrations(engine) == "upgraded"

    with engine.connect() as connection:
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == "0010"
        after = connection.execute(
            text(f"select {', '.join(columns_before)} from cost_actuals order by id")
        ).fetchall()
        assert after == before
        added = connection.execute(
            text("select figure_kind, supplier_name, quote_reference from cost_actuals order by id")
        ).fetchall()
        assert added == [("actual", None, None), ("actual", None, None)]

    from sqlalchemy.orm import sessionmaker

    with sessionmaker(bind=engine)() as session:
        records = CostActualRepository(session).for_estimate("e1")
        assert [item.figure_kind for item in records] == ["actual", "actual"]
        # Not supplied stayed not supplied, and a stated zero stayed a zero.
        assert records[0].material_cost is None and records[0].machining_cost == 250.5
        assert records[1].material_cost == 0.0
        assert records[1].supersedes_actual_id == "old1"
        # The migrated rows are compared as actuals, exactly as before.
        result = compute_variance(PART_FIGURES, records[0])
        assert line(result, "total").variance == pytest.approx(-58.0)
        assert line(result, "total").gap is None


def test_the_upgraded_table_keeps_its_constraints_and_requires_a_kind():
    from sqlalchemy.exc import IntegrityError

    upgraded = _engine_at("0007")
    with upgraded.begin() as connection:
        connection.execute(text(
            OLD_ROW + "('old1', 'e1', '2026-10-20 09:00:00', 'ERP', 'per_unit', NULL, 'INR', "
            "300.0, NULL, NULL, NULL, NULL, NULL, NULL, '', NULL)"
        ))
    run_migrations(upgraded)
    fresh = _engine_at("head")

    def shape(engine):
        inspector = inspect(engine)
        return (
            {c["name"]: (str(c["type"]), c["nullable"], c["default"])
             for c in inspector.get_columns("cost_actuals")},
            sorted((u["name"], tuple(u["column_names"]))
                   for u in inspector.get_unique_constraints("cost_actuals")),
            sorted((i["name"], tuple(i["column_names"])) for i in inspector.get_indexes("cost_actuals")),
            inspector.get_pk_constraint("cost_actuals")["constrained_columns"],
        )

    assert shape(upgraded) == shape(fresh)
    columns = shape(fresh)[0]
    # Required, and no default left behind in the schema to answer for the caller.
    assert columns["figure_kind"][1] is False
    assert columns["figure_kind"][2] is None
    assert ("uq_cost_actual_supersedes", ("supersedes_actual_id",)) in shape(upgraded)[1]

    for engine in (upgraded, fresh):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(
                    OLD_ROW + "('new1', 'e1', '2026-11-12 09:00:00', 'ERP', 'per_unit', NULL, "
                    "'INR', 300.0, NULL, NULL, NULL, NULL, NULL, NULL, '', NULL)"
                ))


def test_a_fresh_database_records_both_kinds(db_session):
    estimate_id = create_estimate()["estimate_id"]
    assert post_actual(estimate_id).status_code == 201
    assert post_quote(estimate_id).status_code == 201
    stored = CostActualRepository(db_session).for_estimate(estimate_id)
    assert [item.figure_kind for item in stored] == ["actual", "supplier_quote"]
