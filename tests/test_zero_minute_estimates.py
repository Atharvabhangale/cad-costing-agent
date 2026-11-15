"""A zero-minute system estimate must never read as a usable estimate.

Found on a real needle-roller bearing: 36 blind holes Ø1 mm x 0.2 mm deep. The
drilling formula gives 0.0024 min for each, which rounded to 0.00 and was
reported as `estimated`. Accepted, such a time commits nothing — the plan costs
only a cycle above zero — so the operations were estimated, accepted and absent
from the cost.

Two halves are held here:

- the estimator refuses a cycle below the resolution it reports to, with the
  unrounded figure recorded, and invents no minimum;
- whatever still arrives with a zero system time (a stored plan, a hand-built
  payload) never applies, and is listed as uncosted with its own reason.

A typed zero is a different statement — "this takes no time" — and is refused
outright rather than costed at nothing. A typed zero *setup* is a decision.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cad_costing_agent import assembly_plan, manufacturing_plan, operation_time
from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.features import recognize_features
from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import (
    NOT_COSTED_ZERO_SYSTEM_TIME,
    ZERO_CYCLE_TIME_REFUSED,
    ManufacturingPlan,
    PlannedOperation,
    TimeSource,
    build_plan,
)
from cad_costing_agent.operation_suggestions import suggest_operations
from cad_costing_agent.operation_time import (
    DEFAULT_ASSUMPTIONS,
    MOULDING_TIME_DECIMALS,
    TIME_DECIMALS,
    TimeAssumptions,
    TimeStatus,
    estimate_operation_time,
    time_resolution_minutes,
)
from tests import step_fixtures

client = TestClient(app)
STATIC = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent" / "static"

FORM = {
    "material": "Steel",
    "source_unit": "mm",
    "part_id": "P-ZERO",
    "part_name": "Plate",
    "quantity": "10",
    "rate_set_id": "poc-default",
}


def drill(diameter: float, depth: float, subtype: str = "blind", **kwargs):
    return estimate_operation_time(
        operation_type="drilling",
        feature_type="hole",
        feature_subtype=subtype,
        dimensions={"diameter_mm": diameter, "depth_mm": depth},
        **kwargs,
    )


# --- the estimator: the rounding boundary --------------------------------------
#
# A Ø1 mm drill at the baseline runs at the 6000 rpm limit, so it feeds at
# 0.15 x 6000 = 900 mm/min, and a blind hole travels depth + 2 mm approach.
# A time is reported to 0.01 min, so it reads as 0.00 below 0.005 min: 4.5 mm
# of travel, a hole 2.5 mm deep. That is the one boundary — the reported figure
# being zero — and a time that reports as 0.01 is an ordinary rounded estimate.


def test_the_resolution_is_the_reporting_resolution_of_each_process():
    assert time_resolution_minutes("drilling") == 10.0**-TIME_DECIMALS == 0.01
    assert time_resolution_minutes("milling") == 0.01
    assert time_resolution_minutes("turning") == 0.01
    assert time_resolution_minutes("injection_moulding") == 10.0**-MOULDING_TIME_DECIMALS
    assert time_resolution_minutes("die_casting") == 10.0**-MOULDING_TIME_DECIMALS


def test_the_real_parts_hole_is_not_estimated_at_zero():
    """Ø1 x 0.2 blind: 2.2 mm of travel at 900 mm/min is 0.002444 min."""
    estimate = drill(1.0, 0.2)

    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.estimated is False
    # Null, never zero: nothing downstream can add it into a total.
    assert estimate.cycle_time_minutes is None
    assert estimate.setup_time_minutes is None
    assert estimate.total_time_minutes is None
    assert estimate.confidence is None
    assert estimate.missing == ["cycle_time_minutes"]
    # The unrounded figure is there for the engineer to see.
    assert estimate.parameters["unrounded_cycle_time_minutes"] == pytest.approx(2.2 / 900.0, abs=1e-9)
    assert estimate.parameters["time_resolution_minutes"] == 0.01
    assert "0.002444 min" in estimate.reason
    assert "rounds to 0 at the 0.01 min" in estimate.reason
    assert "enter a cycle time" in estimate.reason
    assert "No minimum time is assumed." in estimate.reason
    assert any("unrounded cycle 0.002444 min" in line for line in estimate.assumptions)
    # Which formula said so, and the geometry it read, are still recorded.
    assert estimate.method == "drilling_time_baseline_v1"
    assert estimate.evidence
    assert estimate.drilling_steps == [1.0]
    # No alternative is offered a time when the hole itself has none.
    assert estimate.drilling_options == []


def test_just_below_the_boundary_needs_a_time():
    estimate = drill(1.0, 2.49)  # 4.49 / 900 = 0.004989 min, reported as 0.00

    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.cycle_time_minutes is None
    assert 0 < estimate.parameters["unrounded_cycle_time_minutes"] < 0.005


def test_exactly_at_the_boundary_is_an_estimate():
    estimate = drill(1.0, 2.5)  # 4.5 / 900 = 0.005 min, reported as 0.01

    assert estimate.time_status is TimeStatus.ESTIMATED
    assert estimate.cycle_time_minutes == 0.01
    assert "unrounded_cycle_time_minutes" not in estimate.parameters


def test_just_above_the_boundary_is_an_estimate():
    estimate = drill(1.0, 2.51)  # 4.51 / 900 = 0.005011 min, reported as 0.01

    assert estimate.time_status is TimeStatus.ESTIMATED
    assert estimate.cycle_time_minutes == 0.01
    assert estimate.missing == []
    assert "unrounded_cycle_time_minutes" not in estimate.parameters
    assert "time_resolution_minutes" not in estimate.parameters


@pytest.mark.parametrize("depth", [4.0, 6.99, 7.0, 7.01])
def test_a_time_that_reports_as_the_resolution_is_exactly_what_it_was(depth):
    """0.0067 to 0.0100 min all report as 0.01, as they always did: ordinary rounding."""
    estimate = drill(1.0, depth)

    assert estimate.time_status is TimeStatus.ESTIMATED
    assert estimate.cycle_time_minutes == 0.01
    assert estimate.setup_time_minutes == DEFAULT_ASSUMPTIONS.drilling_setup_minutes
    assert "unrounded_cycle_time_minutes" not in estimate.parameters


def test_no_estimate_is_ever_zero_minutes():
    """Across the whole small-hole range: a time, or none — never 0.00."""
    for tenth in range(1, 80):
        estimate = drill(1.0, tenth / 10.0)
        if estimate.estimated:
            assert estimate.cycle_time_minutes > 0
        else:
            assert estimate.cycle_time_minutes is None


def test_an_ordinary_estimate_is_exactly_what_it_was():
    """Above the boundary nothing is added: no new key, no new sentence."""
    estimate = drill(10.0, 20.0, "through")

    assert estimate.time_status is TimeStatus.ESTIMATED
    assert estimate.cycle_time_minutes > 0
    assert set(estimate.parameters) == {
        "drilling_cutting_speed_m_per_min",
        "drilling_feed_mm_per_rev",
        "drilling_approach_mm",
        "drilling_breakthrough_mm",
        "drilling_setup_minutes",
        "drilling_pilot_threshold_mm",
        "drilling_pilot_diameter_mm",
        "drilling_max_step_ratio",
        "drilling_step_count",
        "min_spindle_rpm",
        "max_spindle_rpm",
        "resolved_spindle_rpm",
        "resolved_feed_rate_mm_per_min",
    }
    assert not any("unrounded" in line for line in estimate.assumptions)


@pytest.mark.parametrize(
    ("operation_type", "feature_type", "dimensions", "method"),
    [
        # 1 + 1 mm approach at 0.08 x 4 x 2122 rpm = 679 mm/min: 0.0029 min.
        ("milling", "chamfer", {"length_mm": 1.0}, "milling_blend_finishing_time_baseline_v1"),
        # 1 mm² / 7.2 mm stepover + 1 mm approach at 679 mm/min: 0.0017 min.
        ("milling", "plane", {"area_mm2": 1.0}, "milling_facing_time_baseline_v1"),
        # 2 passes x (0.5 + 1) mm at 0.2 x 6000 rpm = 1200 mm/min: 0.0025 min.
        ("turning", "cylinder", {"diameter_mm": 5.0, "length_mm": 0.5}, "turning_time_baseline_v1"),
        # 1 pass x (1 + 1) mm at 1200 mm/min: 0.0017 min.
        ("turning", "plane", {"length_mm": 2.0}, "facing_time_baseline_v1"),
    ],
)
def test_every_machining_formula_refuses_a_cycle_that_rounds_to_nothing(
    operation_type, feature_type, dimensions, method
):
    assumptions = TimeAssumptions(milling_approach_mm=1.0, turning_approach_mm=1.0)
    estimate = estimate_operation_time(
        operation_type=operation_type,
        feature_type=feature_type,
        dimensions=dimensions,
        assumptions=assumptions,
    )

    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.cycle_time_minutes is None
    assert estimate.method == method
    assert 0 < estimate.parameters["unrounded_cycle_time_minutes"] < 0.005
    assert estimate.missing == ["cycle_time_minutes"]


def test_a_profile_driven_process_is_held_to_the_same_rule():
    """Bending: one bend at 0.004 min per bend reports as 0.00 min."""
    assumptions = TimeAssumptions(bending_minutes_per_bend=0.004, bending_setup_minutes=5.0)
    estimate = estimate_operation_time(
        operation_type="bending", dimensions={"bend_count": 1.0}, assumptions=assumptions
    )
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.parameters["unrounded_cycle_time_minutes"] == 0.004

    three = estimate_operation_time(
        operation_type="bending", dimensions={"bend_count": 3.0}, assumptions=assumptions
    )
    assert three.time_status is TimeStatus.ESTIMATED
    assert three.cycle_time_minutes == 0.01


def test_an_alternative_drilling_sequence_that_rounds_to_nothing_is_not_offered():
    """Every drill at the rpm limit, 2.2 mm of travel: 0.0024 min a pass.

    One drill (0.0024) and pilot + drill (0.0049) report as 0.00; the rule's
    three drills (0.0073) report as 0.01. Only the last carries a usable time.
    """
    assumptions = TimeAssumptions(drilling_cutting_speed_m_per_min=400.0)
    estimate = drill(20.0, 0.2, assumptions=assumptions)

    assert estimate.time_status is TimeStatus.ESTIMATED
    assert estimate.cycle_time_minutes == 0.01
    assert [option.steps for option in estimate.drilling_options] == [[6.0, 15.0, 20.0]]
    assert all(option.cycle_time_minutes > 0 for option in estimate.drilling_options)
    assert estimate.drilling_options[0].suggested is True


def test_the_batch_counts_it_as_needing_input():
    response = client.post(
        "/operations/estimate-time",
        json={
            "units": "mm",
            "operations": [
                {
                    "operation_id": "OP-001",
                    "operation_type": "drilling",
                    "feature_type": "hole",
                    "feature_subtype": "blind",
                    "dimensions": {"diameter_mm": 1.0, "depth_mm": 0.2},
                },
                {
                    "operation_id": "OP-002",
                    "operation_type": "drilling",
                    "feature_type": "hole",
                    "feature_subtype": "through",
                    "dimensions": {"diameter_mm": 10.0, "depth_mm": 20.0},
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["estimated_count"] == 1
    assert body["needs_input_count"] == 1
    tiny = body["estimates"][0]["estimate"]
    assert tiny["time_status"] == "needs_user_input"
    assert tiny["cycle_time_minutes"] is None
    assert tiny["parameters"]["unrounded_cycle_time_minutes"] > 0


# --- the plan: a zero system time never applies -----------------------------------


def tiny_hole(**updates) -> PlannedOperation:
    base = PlannedOperation(
        operation_id="OP-TINY",
        sequence_position=1,
        operation_name="Drill Ø1",
        system_suggested_operation="drilling",
        feature_type="hole",
        feature_subtype="blind",
        dimensions={"diameter_mm": 1.0, "depth_mm": 0.2},
    )
    return base.with_estimate(drill(1.0, 0.2)).model_copy(update=updates)


def stored_zero(**updates) -> PlannedOperation:
    """The shape the defect left behind: "estimated" at 0.0 min, and accepted."""
    return PlannedOperation(
        operation_id="OP-OLD",
        sequence_position=2,
        operation_name="Drill Ø1 (stored)",
        system_suggested_operation="drilling",
        system_suggested_setup_time=5.0,
        system_suggested_cycle_time=0.0,
        system_time_status=TimeStatus.ESTIMATED,
        system_time_accepted=True,
        system_drilling_steps=[1.0],
    ).model_copy(update=updates)


def good_hole() -> PlannedOperation:
    base = PlannedOperation(
        operation_id="OP-GOOD",
        sequence_position=3,
        operation_name="Drill Ø10",
        system_suggested_operation="drilling",
        feature_type="hole",
        feature_subtype="through",
        dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
    )
    return base.with_estimate(drill(10.0, 20.0, "through")).accepted(use_estimated_time=True)


def test_a_planned_tiny_hole_needs_a_time_and_cannot_be_accepted():
    operation = tiny_hole()

    assert operation.system_time_status is TimeStatus.NEEDS_USER_INPUT
    assert operation.system_suggested_cycle_time is None
    assert operation.system_time_parameters["unrounded_cycle_time_minutes"] > 0

    accepted = operation.accepted(use_estimated_time=True)
    assert accepted.system_time_accepted is False
    assert accepted.time_source is TimeSource.NEEDS_INPUT
    assert accepted.final_cycle_time is None
    assert accepted.costable is False


def test_the_engineers_time_for_a_tiny_hole_is_costed():
    operation = tiny_hole().with_times(cycle_time_minutes=0.05)

    assert operation.time_source is TimeSource.USER
    assert operation.final_cycle_time == 0.05
    assert operation.costable is True
    # What the formula said is still on the record beside it.
    assert operation.system_time_parameters["unrounded_cycle_time_minutes"] > 0


def test_a_stored_zero_estimate_never_applies_even_when_accepted():
    operation = stored_zero()

    assert operation.system_time_is_zero is True
    assert operation.system_time_applies is False
    assert operation.time_source is TimeSource.NEEDS_INPUT
    assert operation.final_cycle_time is None
    assert operation.final_setup_time is None
    assert operation.costable is False
    assert operation.not_costable_reason == NOT_COSTED_ZERO_SYSTEM_TIME
    # Accepting again commits nothing either.
    fresh = stored_zero(system_time_accepted=False).accepted(use_estimated_time=True)
    assert fresh.system_time_accepted is False


def test_a_zero_time_for_another_process_keeps_the_changed_process_reason():
    """The zero reason is about the process selected; a changed process has its own."""
    operation = stored_zero().with_operation_type("milling")

    assert operation.system_time_is_zero is False
    assert "this operation is now milling" in operation.not_costable_reason


def test_a_positive_estimate_is_not_called_zero():
    operation = good_hole()

    assert operation.system_time_is_zero is False
    assert operation.system_time_applies is True
    assert operation.costable is True
    assert operation.not_costable_reason is None


def test_the_plan_lists_both_as_uncosted_and_costs_only_the_timed_one():
    plan = ManufacturingPlan(operations=[tiny_hole(), stored_zero(), good_hole()])

    assert [o.operation_id for o in plan.uncosted_operations()] == ["OP-TINY", "OP-OLD"]
    assert [r.operation_id for r in plan.to_operation_requirements(1)] == ["OP-GOOD"]
    summary = plan.time_summary(1)
    assert summary.uncosted_operation_ids == ["OP-TINY", "OP-OLD"]
    assert summary.setups[0].costable_operation_count == 1
    assert plan.counts_toward_cost(plan.get("OP-OLD")) is False


def test_a_component_plan_names_them_as_missing_inputs():
    """The assembly path: a component with such holes says what is missing."""
    plan = ManufacturingPlan(operations=[tiny_hole(), stored_zero(), good_hole()])

    missing = assembly_plan._missing_inputs(plan)

    assert any(line.startswith("OP-TINY ") and "no committed cycle time" in line for line in missing)
    assert any(line.startswith("OP-OLD ") and "no committed cycle time" in line for line in missing)
    assert not any(line.startswith("OP-GOOD ") for line in missing)


# --- a typed zero ---------------------------------------------------------------


def test_a_typed_zero_cycle_time_is_refused_and_says_what_it_means():
    with pytest.raises(ValueError) as refused:
        tiny_hole().with_times(cycle_time_minutes=0)
    assert str(refused.value) == ZERO_CYCLE_TIME_REFUSED
    assert "greater than zero" in ZERO_CYCLE_TIME_REFUSED
    assert "removed from the plan" in ZERO_CYCLE_TIME_REFUSED

    with pytest.raises(ValidationError):
        PlannedOperation(
            operation_id="OP-001",
            sequence_position=1,
            user_operation_type="drilling",
            user_cycle_time=0,
        )


def test_a_typed_zero_cycle_time_never_reaches_a_cost_through_the_api():
    plan = ManufacturingPlan(operations=[good_hole()]).model_dump(mode="json")
    plan["operations"][0]["user_cycle_time"] = 0

    response = client.post(
        "/operations/plan-cost-preview?rate_set_id=poc-default&quantity=1", json=plan
    )
    assert response.status_code == 422


def test_a_typed_zero_setup_time_is_a_decision():
    operation = good_hole().with_times(setup_time_minutes=0)

    assert operation.final_setup_time == 0
    assert operation.costable is True


# --- the estimate's own response ---------------------------------------------------


@lru_cache(maxsize=None)
def _plate() -> bytes:
    return step_fixtures.plate_with_four_holes()


@lru_cache(maxsize=None)
def _built() -> str:
    recognition = recognize_features(read_step_shape(_plate()), filename="plate.step")
    return build_plan(
        suggest_operations(recognition).suggestions,
        units=recognition.units,
        source_filename="plate.step",
    ).model_dump_json()


def _accepted_plan() -> ManufacturingPlan:
    plan = ManufacturingPlan.model_validate_json(_built())
    for operation in plan.operations:
        plan = plan.replacing(operation.accepted(use_estimated_time=True))
    for setup in plan.setups:
        plan = plan.replacing_setup(setup.accepted(use_estimated_time=True))
    return plan


def _cost(plan: ManufacturingPlan):
    return client.post(
        "/cost/cad-part",
        files={"file": ("plate.step", _plate(), "application/octet-stream")},
        data={**FORM, "manufacturing_plan": plan.model_dump_json()},
    )


def test_a_fully_costed_estimates_response_is_what_it_always_was():
    plan = _accepted_plan()
    assert plan.uncosted_operations() == []

    response = _cost(plan)

    assert response.status_code == 200, response.text
    assert set(response.json()) == {
        "estimate_id",
        "geometry",
        "mass",
        "part_cost",
        "material_basis",
        "stock_mass",
    }


def test_the_response_lists_what_was_left_out_with_each_reason():
    plan = _accepted_plan()
    whole = _cost(plan).json()
    drilling = [o for o in plan.ordered if o.system_suggested_operation == "drilling"]
    stored, tiny = drilling[0], drilling[1]

    # One hole as the defect stored it; one as the estimator now reports it.
    plan = plan.replacing(
        stored.model_copy(update={"system_suggested_cycle_time": 0.0, "system_drilling_options": []})
    )
    plan = plan.replacing(
        tiny.model_copy(update={"dimensions": {"diameter_mm": 1.0, "depth_mm": 0.2}})
        .with_estimate(drill(1.0, 0.2))
    )

    response = _cost(plan)

    assert response.status_code == 200, response.text
    body = response.json()
    listed = {entry["operation_id"]: entry for entry in body["uncosted_operations"]}
    assert set(listed) == {stored.operation_id, tiny.operation_id}
    assert listed[stored.operation_id]["reason"] == NOT_COSTED_ZERO_SYSTEM_TIME
    assert listed[stored.operation_id]["time_status"] == "estimated"
    assert listed[tiny.operation_id]["time_status"] == "needs_user_input"
    assert "rounds to 0 at the 0.01 min" in listed[tiny.operation_id]["time_reason"]
    assert listed[tiny.operation_id]["operation_type"] == "drilling"
    assert body["uncosted_note"].startswith("2 planned operation(s)")
    # Everything the response always carried is still there, unchanged in shape.
    assert set(body) - {"uncosted_operations", "uncosted_note"} == set(whole)
    assert len(body["part_cost"]["operations"]) == len(whole["part_cost"]["operations"]) - 2

    # The stored estimate and its quotation say the same.
    snapshot = client.get(f"/estimates/{body['estimate_id']}").json()["plan_snapshot"]
    assert set(snapshot["uncosted_operation_ids"]) == set(listed)
    quotation = client.get(
        f"/estimates/{body['estimate_id']}/quotation", params={"view": "engineering"}
    ).json()
    warning = next(w for w in quotation["warnings"] if w["code"] == "partial_costing")
    assert stored.operation_id in warning["detail"]
    assert tiny.operation_id in warning["detail"]

    # And the plan's impact summary names them before anything is stored.
    preview = client.post(
        "/operations/plan-cost-preview?rate_set_id=poc-default&quantity=10",
        json=plan.model_dump(mode="json"),
    ).json()
    assert set(preview["time"]["uncosted_operation_ids"]) == set(listed)


# --- the browser mirror --------------------------------------------------------------


def _js_code(name: str) -> str:
    source = (STATIC / name).read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def _js_function(code: str, name: str) -> str:
    return code.split(f"function {name}(", 1)[1].split("\n}\n", 1)[0]


def _python_property(name: str) -> str:
    source = textwrap.dedent(inspect.getsource(getattr(PlannedOperation, name).fget))
    tree = ast.parse(source)
    function = tree.body[0]
    if isinstance(function.body[0], ast.Expr) and isinstance(function.body[0].value, ast.Constant):
        function.body.pop(0)
    return ast.unparse(tree)


def test_the_table_and_the_backend_agree_that_a_zero_system_time_never_applies():
    plan = _js_code("plan.js")

    applies = _js_function(plan, "systemTimeApplies")
    assert "systemCycleForChoice(operation) > 0" in applies
    assert "!= null" not in applies.split("estimatedFor(operation) != null", 1)[1]
    assert "cycle is not None and cycle > 0" in _python_property("system_time_applies")

    zero = _js_function(plan, "systemTimeIsZero")
    assert 'operation.system_time_status === "estimated"' in zero
    assert "effectiveType(operation) === estimatedFor(operation)" in zero
    assert "cycle <= 0" in zero
    assert "cycle is not None and cycle <= 0" in _python_property("system_time_is_zero")

    # A row is still costable only with a committed positive cycle time.
    assert "finalCycle(operation) > 0" in _js_function(plan, "isCostable")


def test_the_table_says_the_backends_sentence_for_a_zero_system_time():
    plan = _js_code("plan.js")
    block = plan.split("const ZERO_SYSTEM_TIME_NOTE =", 1)[1].split(";", 1)[0]
    said = "".join(re.findall(r'"([^"]*)"', block))

    assert said == NOT_COSTED_ZERO_SYSTEM_TIME
    assert "systemTimeIsZero(operation)" in _js_function(plan, "planExplanation")


def test_the_table_reads_the_refusal_and_compares_nothing_with_a_threshold():
    plan = _js_code("plan.js")
    below = _js_function(plan, "estimateBelowResolution")

    assert 'operation.system_time_status === "needs_user_input"' in below
    assert "parameters.unrounded_cycle_time_minutes != null" in below
    # The resolution is the backend's; the browser never restates it.
    assert "0.01" not in plan
    assert "time_resolution_minutes" not in plan
    note = _js_function(plan, "refreshUncostedNote")
    assert "estimateBelowResolution(operation) || systemTimeIsZero(operation)" in note
    assert "No minimum is assumed." in note


def test_a_typed_zero_cycle_is_not_kept_as_the_engineers_time():
    plan = _js_code("plan.js")

    assert 'const zeroCycle = which === "cycle" && raw !== "" && !(Number(raw) > 0);' in plan
    assert "if (zeroCycle) operation.user_cycle_time = null;" in plan
    assert "ZERO_CYCLE_TIME_NOTE" in _js_function(plan, "refreshUncostedNote")
    block = plan.split("const ZERO_CYCLE_TIME_NOTE =", 1)[1].split(";", 1)[0]
    said = "".join(re.findall(r'"([^"]*)"', block))
    assert "A cycle time of 0 is not a time" in said
    assert "removed from the plan" in said


def test_the_plan_builder_copies_the_fields_the_refusal_is_read_from():
    plan = _js_code("plan.js")
    assert "operation.system_time_parameters = estimate.parameters || {};" in plan
    assert "operation.system_time_status = estimate.time_status;" in plan


# --- structure -------------------------------------------------------------------------


def test_no_minimum_time_is_substituted_anywhere():
    """The refusal returns no time; nothing clamps a cycle up to the resolution."""
    from tests.test_manufacturing_plan import code_without_prose

    for module in (operation_time, manufacturing_plan):
        code = code_without_prose(module)
        assert "max(cycle" not in code
        assert "max(resolution" not in code
        assert "minimum_cycle" not in code
        assert "math.ceil(cycle" not in code
    assert DEFAULT_ASSUMPTIONS.model_dump().keys() == TimeAssumptions().model_dump().keys()


# --- a part quotation with operations left out is refused ------------------------
#
# The part counterpart of the assembly rule: a planned operation with no committed
# time is in no figure, so the customer document is refused until a time is typed
# or the operation is removed. The engineering view stays, and a quotation issued
# before the rule is not revoked by it.


def _part_with_one_untimed_hole():
    plan = _accepted_plan()
    tiny = next(o for o in plan.ordered if o.system_suggested_operation == "drilling")
    plan = plan.replacing(
        tiny.model_copy(update={"dimensions": {"diameter_mm": 1.0, "depth_mm": 0.2}})
        .with_estimate(drill(1.0, 0.2))
    )
    return plan, tiny.operation_id


def test_a_part_quotation_with_an_operation_left_out_is_refused_and_names_it():
    plan, untimed = _part_with_one_untimed_hole()
    body = _cost(plan).json()
    estimate_id = body["estimate_id"]

    customer = client.get(f"/estimates/{estimate_id}/quotation").json()
    assert customer["available"] is False
    blocker = next(b for b in customer["blockers"] if b["code"] == "part_operations_without_time")
    assert untimed in blocker["detail"]
    assert blocker["detail"].startswith("1 planned operation(s) have no committed time")

    engineering = client.get(
        f"/estimates/{estimate_id}/quotation", params={"view": "engineering"}
    ).json()
    assert engineering["available"] is False
    assert engineering["engineering"] is not None

    issued = client.post(f"/estimates/{estimate_id}/quotation", json={"customer_name": "Acme"})
    assert issued.status_code == 409
    assert untimed in issued.json()["detail"]


def test_typing_a_time_or_removing_the_operation_makes_the_part_quotable():
    plan, untimed = _part_with_one_untimed_hole()
    operation = next(o for o in plan.operations if o.operation_id == untimed)

    typed = plan.replacing(operation.with_times(cycle_time_minutes=0.05))
    document = client.get(f"/estimates/{_cost(typed).json()['estimate_id']}/quotation").json()
    assert document["available"] is True
    assert not any(w["code"] == "partial_costing" for w in document["warnings"])

    from cad_costing_agent.operation_suggestions import UserAction

    removed = plan.replacing(operation.model_copy(update={"user_action": UserAction.REJECTED}))
    document = client.get(f"/estimates/{_cost(removed).json()['estimate_id']}/quotation").json()
    assert document["available"] is True


def test_a_quotation_issued_before_the_rule_is_not_revoked_by_it():
    """Re-reading an issued document passes `already_issued`; the new blocker is
    dropped there, so a document issued under the old rule still renders as
    issued, with the `partial_costing` warning it always carried. Issuing passes
    an `issued_at` but not that flag, and is refused (the test above)."""
    from datetime import datetime, timezone

    from cad_costing_agent.estimates import EstimateDetail
    from cad_costing_agent.quotation import build_quotation

    plan, untimed = _part_with_one_untimed_hole()
    stored = client.get(f"/estimates/{_cost(plan).json()['estimate_id']}").json()
    detail = EstimateDetail(
        estimate_id=stored["estimate_id"],
        request_snapshot=stored.get("request_snapshot") or {},
        result_snapshot=stored.get("result_snapshot") or {},
        rate_set_snapshot=stored.get("rate_set_snapshot"),
        plan_snapshot=stored.get("plan_snapshot"),
        time_profile_snapshot=stored.get("time_profile_snapshot"),
    )

    preview = build_quotation(detail)
    assert preview.available is False

    being_issued = build_quotation(detail, issued_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert being_issued.available is False

    issued = build_quotation(
        detail, issued_at=datetime(2026, 1, 1, tzinfo=timezone.utc), already_issued=True
    )
    assert issued.available is True
    assert any(w.code == "partial_costing" and untimed in w.detail for w in issued.warnings)
