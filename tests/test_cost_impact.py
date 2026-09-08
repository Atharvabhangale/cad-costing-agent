"""Cost and assumption impact analysis: why one estimate differs from another.

Two properties matter more than any individual case.

**The decomposition is exact.** Every impact is an algebraic share of the total,
so the parts must sum to the whole. A residual is reported as `rounding` when it
is pennies and `unexplained` when it is not — never absorbed into a neighbouring
category, because a half-explanation that looks complete is worse than a stated
gap. Several tests below check that sum directly.

**Nothing is attributed that the data does not support.** When one input moved,
the whole difference is attributed to it. When several moved, the answer says
`multiple_inputs` and shows every before/after rather than inventing causal
percentages. Attribution counts *inputs*, not fields — adding a setup changes
both the setup count and the total setup minutes, and calling that "multiple
inputs" would be technically true and practically misleading.
"""

from __future__ import annotations

import json
from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.estimates import (
    ATTRIBUTION_MULTIPLE,
    ATTRIBUTION_NONE,
    ATTRIBUTION_SINGLE,
    ROUNDING_ALLOWANCE,
)
from cad_costing_agent.features import recognize_features
from cad_costing_agent.main import STATIC_PATH, app
from cad_costing_agent.manufacturing_plan import DEFAULT_SETUP_ID, build_plan
from cad_costing_agent.operation_suggestions import suggest_operations
from tests import step_fixtures

client = TestClient(app)

FORM = {
    "material": "Steel",
    "source_unit": "mm",
    "part_id": "P-IMPACT",
    "part_name": "Plate",
    "quantity": "10",
    "rate_set_id": "poc-default",
}


@lru_cache(maxsize=None)
def step(name: str = "plate_with_four_holes") -> bytes:
    return getattr(step_fixtures, name)()


@lru_cache(maxsize=None)
def _built(name: str = "plate_with_four_holes") -> str:
    recognition = recognize_features(read_step_shape(step(name)), filename=f"{name}.step")
    plan = build_plan(
        suggest_operations(recognition).suggestions,
        units=recognition.units,
        source_filename=f"{name}.step",
    )
    holes = {o.operation_id for o in plan.ordered if o.system_suggested_operation == "drilling"}
    plan = plan.model_copy(
        update={
            "operations": [
                o.accepted(use_estimated_time=True)
                for o in plan.operations
                if o.operation_id in holes
            ]
        }
    )
    plan = plan.replacing_setup(plan.setup(DEFAULT_SETUP_ID).with_setup_time(10.0))
    return plan.model_dump_json()


def base_plan():
    from cad_costing_agent.manufacturing_plan import ManufacturingPlan

    return ManufacturingPlan.model_validate_json(_built())


def estimate(plan=None, **overrides) -> str:
    data = {**FORM, **overrides}
    if plan is not None:
        data["manufacturing_plan"] = plan.model_dump_json()
    response = client.post(
        "/cost/cad-part",
        files={"file": ("plate.step", step(), "application/octet-stream")},
        data=data,
    )
    assert response.status_code == 200, response.text
    return response.json()["estimate_id"]


def compare(baseline: str, comparison: str, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = client.get(
        f"/estimates/compare?baseline={baseline}&comparison={comparison}"
        + (f"&{query}" if query else "")
    )
    assert response.status_code == 200, response.text
    return response.json()


def impacts_by_category(result) -> dict[str, float]:
    totals: dict[str, float] = {}
    for impact in result["impacts"]:
        totals[impact["category"]] = totals.get(impact["category"], 0.0) + impact["delta"]
    return totals


def assert_impacts_sum_to_total(result):
    """The parts must add up to the whole. This is the property, not a detail."""
    explained = sum(impact["delta"] for impact in result["impacts"])
    actual = result["total_part_cost"]["delta"]
    assert explained == pytest.approx(actual, abs=0.02), (
        f"impacts sum to {explained}, actual delta {actual}"
    )


def drilling_ops(plan):
    return [o for o in plan.ordered if o.system_suggested_operation == "drilling"]


# --- nothing changed ---------------------------------------------------------


def test_two_identical_estimates_have_no_impact_and_no_changes():
    first = estimate(base_plan())
    second = estimate(base_plan())

    result = compare(first, second)

    assert result["total_part_cost"]["delta"] == 0.0
    assert result["changes"] == []
    assert result["impacts"] == []
    assert result["attribution"] == ATTRIBUTION_NONE
    assert "Nothing differs" in result["attribution_note"]


def test_comparing_an_estimate_with_itself_is_zero():
    only = estimate(base_plan())

    result = compare(only, only)

    assert result["total_part_cost"]["delta"] == 0.0
    assert result["impacts"] == []


# --- one input at a time -----------------------------------------------------


def test_a_quantity_change_is_attributed_to_the_quantity():
    before = estimate(base_plan(), quantity="10")
    after = estimate(base_plan(), quantity="20")

    result = compare(before, after)

    assert result["attribution"] == ATTRIBUTION_SINGLE
    assert "the quantity" in result["attribution_note"]
    categories = impacts_by_category(result)
    assert categories["material_quantity"] > 0
    assert categories["operation_time"] > 0
    assert_impacts_sum_to_total(result)


def test_an_operation_time_change_is_attributed_to_the_operations():
    plan = base_plan()
    operation = drilling_ops(plan)[0]
    before = estimate(plan)
    after = estimate(plan.replacing(operation.with_times(cycle_time_minutes=9.9)))

    result = compare(before, after)

    assert result["attribution"] == ATTRIBUTION_SINGLE
    assert "the operations" in result["attribution_note"]
    categories = impacts_by_category(result)
    assert categories["operation_time"] > 0
    assert "setup_time" not in categories
    assert_impacts_sum_to_total(result)


def test_a_setup_grouping_change_is_attributed_to_the_setups_alone():
    """Adding a setup moves the count and the minutes; the engineer moved one thing."""
    plan = base_plan()
    before = estimate(plan)

    moved = drilling_ops(plan)[-1]
    second = plan.with_setup(name="Side fixture", setup_time_minutes=10.0)
    split = plan.with_added_setup(second).with_operation_in_setup(
        moved.operation_id, second.setup_id
    )
    after = estimate(split)

    result = compare(before, after)

    assert result["attribution"] == ATTRIBUTION_SINGLE
    assert "the setup grouping" in result["attribution_note"]
    categories = impacts_by_category(result)
    # 10 extra setup minutes at the drilling machine rate of 1800/h.
    assert categories["setup_time"] == pytest.approx(1800.0 * 10.0 / 60.0, abs=0.01)
    assert "operation_time" not in categories
    assert_impacts_sum_to_total(result)


def test_a_setup_time_change_is_attributed_to_the_setups():
    plan = base_plan()
    before = estimate(plan)
    after = estimate(plan.replacing_setup(plan.setup(DEFAULT_SETUP_ID).with_setup_time(25.0)))

    result = compare(before, after)

    assert result["attribution"] == ATTRIBUTION_SINGLE
    categories = impacts_by_category(result)
    assert categories["setup_time"] == pytest.approx(1800.0 * 15.0 / 60.0, abs=0.01)
    assert_impacts_sum_to_total(result)


def test_a_rate_set_change_is_one_input_even_though_it_moves_many_figures():
    """The material price and the machine rates both come *from* the rate set."""
    before = estimate(base_plan())
    after = estimate(base_plan(), rate_set_id="poc-machine-shop", rate_set_version="2.0.0")

    result = compare(before, after)

    assert result["attribution"] == ATTRIBUTION_SINGLE
    assert "the costing profile" in result["attribution_note"]
    categories = impacts_by_category(result)
    assert categories["material_rate"] > 0
    assert categories["operation_rate"] > 0
    assert categories["overhead"] > 0
    assert categories["tooling"] > 0
    assert_impacts_sum_to_total(result)


def test_an_overhead_change_is_isolated_to_overhead():
    before = estimate(base_plan())
    after = estimate(base_plan(), overhead_rate_percent="20")

    result = compare(before, after)

    categories = impacts_by_category(result)
    assert categories["overhead"] > 0
    assert "material_rate" not in categories
    assert "operation_time" not in categories
    assert_impacts_sum_to_total(result)


def test_a_scrap_change_is_isolated_to_scrap():
    before = estimate(base_plan(), overhead_rate_percent="0", scrap_rate_percent="0")
    after = estimate(base_plan(), overhead_rate_percent="0", scrap_rate_percent="7")

    categories = impacts_by_category(compare(before, after))

    assert categories["scrap"] > 0
    assert "overhead" not in categories


def test_a_tooling_change_is_isolated_to_tooling():
    before = estimate(base_plan(), tooling_cost_per_batch="0")
    after = estimate(base_plan(), tooling_cost_per_batch="450")

    result = compare(before, after)
    categories = impacts_by_category(result)

    assert categories["tooling"] == pytest.approx(450.0, abs=0.01)
    assert_impacts_sum_to_total(result)


def test_a_material_change_is_attributed_to_the_material():
    before = estimate(base_plan(), material="Steel")
    after = estimate(base_plan(), material="Aluminium")

    result = compare(before, after)

    labels = {change["label"] for change in result["changes"]}
    assert "Material" in labels
    assert "Density (kg/m³)" in labels
    assert "Mass (kg)" in labels
    # Mass belongs to the CAD group and the material to its own, so both moved.
    assert result["attribution"] == ATTRIBUTION_MULTIPLE
    assert_impacts_sum_to_total(result)


# --- operations added, removed and retyped -----------------------------------


def test_an_added_operation_is_its_own_category_not_a_longer_cycle_time():
    plan = base_plan()
    before = estimate(plan)

    from cad_costing_agent.manufacturing_plan import manual_operation

    after = estimate(
        plan.with_added(
            manual_operation(
                operation_id="OP-USER-1",
                sequence_position=99,
                operation_type="milling",
                operation_name="Deburr",
                cycle_time_minutes=2.0,
                setup_id=DEFAULT_SETUP_ID,
            )
        )
    )

    result = compare(before, after)
    categories = impacts_by_category(result)

    assert categories["operation_added"] > 0
    added = [o for o in result["operations"] if o["status"] == "added"]
    assert [o["operation_id"] for o in added] == ["OP-USER-1"]
    assert_impacts_sum_to_total(result)


def test_a_removed_operation_is_its_own_category():
    plan = base_plan()
    before = estimate(plan)
    dropped = drilling_ops(plan)[0]
    after = estimate(plan.replacing(dropped.rejected()))

    result = compare(before, after)
    categories = impacts_by_category(result)

    assert categories["operation_removed"] < 0
    removed = [o for o in result["operations"] if o["status"] == "removed"]
    assert [o["operation_id"] for o in removed] == [dropped.operation_id]
    assert_impacts_sum_to_total(result)


def test_a_changed_process_shows_up_in_the_operation_diff():
    plan = base_plan()
    operation = drilling_ops(plan)[0]
    before = estimate(plan)
    after = estimate(
        plan.replacing(
            operation.with_operation_type("milling").with_times(cycle_time_minutes=2.0)
        )
    )

    result = compare(before, after)
    row = next(o for o in result["operations"] if o["operation_id"] == operation.operation_id)

    assert row["status"] == "changed"
    assert row["process_before"] == "drilling"
    assert row["process_after"] == "milling"
    assert any("process" in change for change in row["changes"])
    assert_impacts_sum_to_total(result)


def test_an_operation_moved_between_setups_is_flagged_as_moved():
    plan = base_plan()
    moved = drilling_ops(plan)[-1]
    before = estimate(plan)
    second = plan.with_setup(name="Side", setup_time_minutes=10.0)
    after = estimate(
        plan.with_added_setup(second).with_operation_in_setup(moved.operation_id, second.setup_id)
    )

    result = compare(before, after)
    row = next(o for o in result["operations"] if o["operation_id"] == moved.operation_id)

    assert row["moved_setup"] is True
    assert row["setup_id_before"] == DEFAULT_SETUP_ID
    assert row["setup_id_after"] == second.setup_id
    assert any("moved to setup" in change for change in row["changes"])


def test_operation_ids_are_preserved_across_the_comparison():
    plan = base_plan()
    before = estimate(plan)
    after = estimate(plan.replacing(drilling_ops(plan)[0].with_times(cycle_time_minutes=3.0)))

    result = compare(before, after)

    ids = {o["operation_id"] for o in result["operations"]}
    assert {o.operation_id for o in drilling_ops(plan)} <= ids


# --- sequence ----------------------------------------------------------------


def test_a_sequence_change_is_reported_as_costing_nothing():
    """The limitation is stated rather than hidden — that is the point."""
    plan = base_plan()
    holes = drilling_ops(plan)
    before = estimate(plan)
    # Move an operation into a second setup and back, so only the order differs.
    reordered = plan.moved(holes[0].operation_id, 1)
    after = estimate(reordered)

    result = compare(before, after)

    assert result["total_part_cost"]["delta"] == 0.0
    assert result["sequence"] is not None
    assert result["sequence"]["cost_impact"] == 0.0
    assert "no sequence-dependent cost" in result["sequence"]["note"]


def test_a_sequence_only_difference_never_claims_a_cost_driver():
    plan = base_plan()
    holes = drilling_ops(plan)
    second = plan.with_setup(name="Second", setup_time_minutes=10.0)
    one = plan.with_added_setup(second).with_operation_in_setup(
        holes[0].operation_id, second.setup_id
    )
    two = plan.with_added_setup(second).with_operation_in_setup(
        holes[1].operation_id, second.setup_id
    )

    result = compare(estimate(one), estimate(two))

    # Same shape, different member: the money is identical.
    assert result["total_part_cost"]["delta"] == pytest.approx(0.0, abs=0.02)
    priced = [change for change in result["changes"] if change["affects_cost"]]
    assert priced == [] or all(change["label"] != "Setup count" for change in priced)


def test_a_change_the_model_does_not_price_is_marked_as_such():
    plan = base_plan()
    before = estimate(plan)
    after = estimate(plan.moved(drilling_ops(plan)[0].operation_id, 1))

    result = compare(before, after)
    sequence_changes = [c for c in result["changes"] if c["field"] == "sequence"]

    for change in sequence_changes:
        assert change["affects_cost"] is False


# --- several inputs at once --------------------------------------------------


def test_several_inputs_changing_is_said_rather_than_apportioned():
    plan = base_plan()
    before = estimate(plan)

    moved = drilling_ops(plan)[-1]
    second = plan.with_setup(name="Side", setup_time_minutes=10.0)
    split = plan.with_added_setup(second).with_operation_in_setup(
        moved.operation_id, second.setup_id
    )
    split = split.replacing(drilling_ops(plan)[0].with_times(cycle_time_minutes=9.9))
    after = estimate(split, quantity="20", rate_set_id="poc-machine-shop", rate_set_version="2.0.0")

    result = compare(before, after)

    assert result["attribution"] == ATTRIBUTION_MULTIPLE
    assert "no single one explains the difference" in result["attribution_note"]
    assert "the costing profile" in result["attribution_note"]
    assert "the setup grouping" in result["attribution_note"]
    # Every before/after is shown rather than a causal percentage.
    assert len(result["changes"]) >= 4
    assert "%" not in result["attribution_note"]
    assert_impacts_sum_to_total(result)


def test_the_part_that_needed_two_changes_is_attributed_to_neither():
    """The interaction term is its own category, never folded into one side."""
    plan = base_plan()
    before = estimate(plan)
    after = estimate(
        plan.replacing(drilling_ops(plan)[0].with_times(cycle_time_minutes=9.9)),
        quantity="20",
        rate_set_id="poc-machine-shop",
        rate_set_version="2.0.0",
    )

    result = compare(before, after)
    categories = impacts_by_category(result)

    assert "combined" in categories
    combined = [i for i in result["impacts"] if i["category"] == "combined"]
    assert all("both changed" in i["description"] for i in combined)
    assert_impacts_sum_to_total(result)


def test_no_causal_percentages_are_ever_invented():
    plan = base_plan()
    before = estimate(plan)
    after = estimate(plan, quantity="20", rate_set_id="poc-machine-shop", rate_set_version="2.0.0")

    result = compare(before, after)

    for impact in result["impacts"]:
        # Impacts are money and a description of the factors, never a share.
        assert "%" not in impact["description"]
        assert set(impact) >= {"category", "description", "before", "after", "delta", "currency"}


# --- exactness ---------------------------------------------------------------


def test_a_remainder_within_rounding_is_labelled_rounding_not_unexplained():
    before = estimate(base_plan(), quantity="10")
    after = estimate(base_plan(), quantity="20")

    result = compare(before, after)
    residuals = [i for i in result["impacts"] if i["category"] in {"rounding", "unexplained"}]

    for residual in residuals:
        assert residual["category"] == "rounding"
        assert abs(residual["delta"]) <= ROUNDING_ALLOWANCE
        assert "rounded to two decimals" in residual["description"]


def test_every_comparison_accounts_for_its_whole_delta():
    """Swept across the change kinds, because a gap anywhere is a wrong answer."""
    plan = base_plan()
    variants = [
        dict(),
        dict(quantity="25"),
        dict(material="Aluminium"),
        dict(overhead_rate_percent="15"),
        dict(scrap_rate_percent="8"),
        dict(tooling_cost_per_batch="99"),
        dict(rate_set_id="poc-machine-shop", rate_set_version="2.0.0"),
    ]
    baseline = estimate(plan)

    for overrides in variants:
        assert_impacts_sum_to_total(compare(baseline, estimate(plan, **overrides)))


def test_the_comparison_is_deterministic():
    before = estimate(base_plan())
    after = estimate(base_plan(), quantity="20")

    assert compare(before, after) == compare(before, after)


# --- currencies --------------------------------------------------------------


def test_two_currencies_are_not_subtracted():
    client.post(
        "/rates",
        json={
            "rate_set_id": "euro-shop",
            "name": "Euro Shop",
            "version": "1.0.0",
            "currency": "EUR",
            "is_illustrative": False,
            "materials": {"steel": {"name": "Steel", "cost_per_kg": 0.9, "density_kg_m3": 7850}},
            "operations": {
                "drilling": {"machine_rate_per_hour": 1500.0, "labor_rate_per_hour": 400.0}
            },
        },
    )
    before = estimate(base_plan())
    after = estimate(base_plan(), rate_set_id="euro-shop")

    result = compare(before, after)

    assert result["currencies_match"] is False
    assert result["impacts"] == []
    assert "different currencies" in result["attribution_note"]
    # The input differences are still shown, because those are still meaningful.
    assert any(change["label"] == "Currency" for change in result["changes"])


# --- assumptions -------------------------------------------------------------


def test_the_assumptions_endpoint_reports_what_the_estimate_rested_on():
    estimate_id = estimate(base_plan())

    body = client.get(f"/estimates/{estimate_id}/assumptions").json()

    assert body["material"] == "Steel"
    assert body["rate_set_id"] == "poc-default"
    assert body["currency"] == "INR"
    assert body["mass_kg"] > 0
    assert body["volume_mm3"] > 0
    assert body["quantity"] == 10
    assert body["setup_count"] == 1
    assert body["operation_count"] > 0
    assert body["total_setup_time_minutes"] == 10.0
    assert body["setups"][0]["setup_time_minutes"] == 10.0
    assert body["sequence"]


def test_cycle_minutes_in_the_assumptions_are_per_part():
    """Otherwise a quantity change would look like a second change to the plan."""
    ten = client.get(f"/estimates/{estimate(base_plan(), quantity='10')}/assumptions").json()
    twenty = client.get(f"/estimates/{estimate(base_plan(), quantity='20')}/assumptions").json()

    assert ten["total_cycle_time_minutes"] == twenty["total_cycle_time_minutes"]


def test_an_unknown_estimate_has_no_assumptions():
    assert client.get("/estimates/nope/assumptions").status_code == 404


def test_the_assumptions_are_read_back_not_recomputed():
    """A newer rate set must not change what a stored estimate says it used."""
    estimate_id = estimate(base_plan())
    before = client.get(f"/estimates/{estimate_id}/assumptions").json()

    client.post(
        "/rates/poc-default/versions",
        json={
            "name": "POC Default",
            "version": "9.9.9",
            "is_illustrative": True,
            "materials": {"steel": {"name": "Steel", "cost_per_kg": 99.0, "density_kg_m3": 7850}},
            "operations": {
                "drilling": {"machine_rate_per_hour": 9999.0, "labor_rate_per_hour": 9999.0}
            },
        },
    )

    assert client.get(f"/estimates/{estimate_id}/assumptions").json() == before


# --- history immutability ----------------------------------------------------


def test_a_comparison_never_alters_either_estimate():
    before = estimate(base_plan())
    after = estimate(base_plan(), quantity="20")
    stored_before = client.get(f"/estimates/{before}").json()
    stored_after = client.get(f"/estimates/{after}").json()

    compare(before, after)
    compare(after, before)

    assert client.get(f"/estimates/{before}").json() == stored_before
    assert client.get(f"/estimates/{after}").json() == stored_after


def test_which_side_is_newer_is_decided_by_recording_order():
    first = estimate(base_plan())
    second = estimate(base_plan(), quantity="20")

    forward = compare(first, second)
    backward = compare(second, first)

    assert forward["older_estimate_id"] == first
    assert backward["older_estimate_id"] == first
    assert forward["total_part_cost"]["delta"] == -backward["total_part_cost"]["delta"]


def test_detail_can_be_switched_off_for_totals_only():
    before = estimate(base_plan())
    after = estimate(base_plan(), quantity="20")

    lean = compare(before, after, detail="false")

    assert lean["detailed"] is False
    assert lean["impacts"] == []
    assert lean["changes"] == []
    # The totals are still there, exactly as they always were.
    assert lean["total_part_cost"]["delta"] > 0


def test_a_detailed_comparison_says_it_is_detailed():
    before = estimate(base_plan())
    after = estimate(base_plan(), quantity="20")

    assert compare(before, after)["detailed"] is True


def test_an_unknown_estimate_id_is_a_404():
    only = estimate(base_plan())

    assert client.get(f"/estimates/compare?baseline={only}&comparison=nope").status_code == 404


# --- nothing else changed ----------------------------------------------------


def test_costing_and_the_stored_totals_are_unchanged():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("plate.step", step(), "application/octet-stream")},
        data={**FORM, "manufacturing_plan": base_plan().model_dump_json()},
    )
    part_cost = response.json()["part_cost"]

    assert part_cost["formula"] == (
        "total_part_cost = total_material_cost + total_operation_cost + total_additional_cost"
    )
    assert part_cost["currency"] == "INR"


def test_the_comparison_layer_recalculates_nothing():
    from pathlib import Path

    import cad_costing_agent.estimates as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    # It reads snapshots; it must never reach for a calculator or a repository.
    for forbidden in [
        "calculate_part_cost",
        "calculate_operation_cost",
        "calculate_material_cost",
        "from cad_costing_agent.repository",
        "resolve_operation",
    ]:
        assert forbidden not in source


def test_rate_sets_and_time_profiles_are_untouched_by_comparing():
    rates = client.get("/rates/poc-default").json()
    profiles = client.get("/time-profiles").json()

    before = estimate(base_plan())
    compare(before, estimate(base_plan(), quantity="20"))

    assert client.get("/rates/poc-default").json() == rates
    assert client.get("/time-profiles").json() == profiles


# --- the UI ------------------------------------------------------------------


def test_the_result_shows_an_assumptions_panel():
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    assert 'id="assumptions-card"' in markup
    assert 'id="assumptions-body"' in markup
    assert "Assumptions behind this estimate" in markup
    assert 'src="impact.js"' in markup


def test_the_result_shows_a_why_did_this_change_panel():
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    impact_js = (STATIC_PATH / "impact.js").read_text(encoding="utf-8")

    assert 'id="impact-card"' in markup
    assert "Why did this change?" in markup
    assert "renderImpactAgainstPrevious" in impact_js
    assert "Cost drivers" in impact_js


def test_the_ui_makes_profile_versions_openable():
    impact_js = (STATIC_PATH / "impact.js").read_text(encoding="utf-8")

    assert "versionLink" in impact_js
    assert "/rates/" in impact_js
    assert "/time-profiles/" in impact_js


def test_the_ui_computes_no_deltas_of_its_own():
    """A plausible delta invented in the browser is a convincing wrong answer."""
    impact_js = (STATIC_PATH / "impact.js").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in impact_js.splitlines() if not line.strip().startswith("//")
    )

    for forbidden in [
        "machine_rate_per_hour",
        "cost_per_kg *",
        "total_after -",
        "after - before",
    ]:
        assert forbidden not in body
    # It reads the server's decomposition rather than deriving one.
    assert "comparison.impacts" in body
    assert "attribution_note" in body


def test_the_ui_shows_the_side_by_side_sections():
    impact_js = (STATIC_PATH / "impact.js").read_text(encoding="utf-8")

    for section in [
        "Geometry",
        "Material",
        "Costing profile",
        "Time profile",
        "Manufacturing plan",
        "Additional costs",
    ]:
        assert section in impact_js
    assert "sideBySideAssumptions" in impact_js
    assert "row-changed" in impact_js


def test_the_ui_surfaces_the_sequence_limitation():
    impact_js = (STATIC_PATH / "impact.js").read_text(encoding="utf-8")

    assert "sequenceMarkup" in impact_js
    assert "sequence.note" in impact_js
