"""Large holes are drilled smallest first: the step rule, its options, and the choice.

The rule — a pilot above a threshold, then drills at most a ratio times the one
before — has illustrative defaults (13 mm, 6 mm, 2.5) chosen to reproduce a
shop's "three drills for Ø20". These tests pin the rule's arithmetic and, more
importantly, the honesty around it: the suggestion is never overwritten, a
sequence the engineer picks is timed by the system or not at all, and nothing
about holes at or below the threshold changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import ManufacturingPlan, PlannedOperation
from cad_costing_agent.operation_suggestions import UserAction
from cad_costing_agent.operation_time import (
    DEFAULT_ASSUMPTIONS,
    TimeAssumptions,
    TimeStatus,
    drilling_steps,
    estimate_operation_time,
)

client = TestClient(app)
STATIC = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent" / "static"


def estimate(diameter: float, depth: float = 16.0, subtype: str = "through", **kwargs):
    return estimate_operation_time(
        operation_type="drilling",
        feature_type="hole",
        feature_subtype=subtype,
        dimensions={"diameter_mm": diameter, "depth_mm": depth},
        **kwargs,
    )


# --- the rule -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("diameter", "expected"),
    [
        (8, [8]),
        (12, [12]),
        (13, [13]),
        (14, [6, 14]),
        (15, [6, 15]),
        (20, [6, 15, 20]),
        (25, [6, 15, 25]),
        (30, [6, 15, 30]),
        (40, [6, 15, 37.5, 40]),
    ],
)
def test_the_default_rule_matches_the_agreed_table(diameter, expected):
    assert drilling_steps(diameter, DEFAULT_ASSUMPTIONS) == expected


def test_a_twenty_millimetre_hole_takes_three_drills():
    assert len(drilling_steps(20, DEFAULT_ASSUMPTIONS)) == 3


def test_the_rule_is_the_profiles_not_hard_coded():
    shop = TimeAssumptions(
        drilling_pilot_threshold_mm=10.0, drilling_pilot_diameter_mm=5.0, drilling_max_step_ratio=2.0
    )
    assert drilling_steps(12, shop) == [5, 10, 12]
    assert drilling_steps(10, shop) == [10]


def test_a_stored_profile_without_the_rule_reads_with_the_defaults():
    legacy = {
        key: value
        for key, value in DEFAULT_ASSUMPTIONS.model_dump().items()
        if not key.startswith("drilling_pilot") and key != "drilling_max_step_ratio"
    }
    assert TimeAssumptions.model_validate(legacy) == DEFAULT_ASSUMPTIONS


# --- the estimate -------------------------------------------------------------------


def test_a_large_hole_is_timed_as_the_sum_of_its_passes():
    stepped = estimate(20)
    assert stepped.method == "drilling_time_stepped_v1"
    assert stepped.drilling_steps == [6, 15, 20]
    passes = [estimate(d, drilling_steps=[d]).cycle_time_minutes for d in (6, 15, 20)]
    # Each pass rounds to 0.01 on its own, so the sum may differ by rounding only.
    assert stepped.cycle_time_minutes == pytest.approx(sum(passes), abs=0.02)
    assert any("Ø6 → Ø15 → Ø20" in line for line in stepped.evidence)
    assert any("not a standard" in line for line in stepped.assumptions)
    assert stepped.parameters["drilling_step_count"] == 3.0
    assert stepped.parameters["drilling_pilot_threshold_mm"] == 13.0


def test_every_option_carries_its_own_time_and_exactly_one_is_suggested():
    result = estimate(20)
    options = {tuple(option.steps): option for option in result.drilling_options}
    assert set(options) == {(20,), (6, 20), (6, 15, 20)}
    assert [option.suggested for option in result.drilling_options].count(True) == 1
    assert options[(6, 15, 20)].suggested
    assert options[(6, 15, 20)].cycle_time_minutes == result.cycle_time_minutes
    assert options[(20,)].cycle_time_minutes == estimate(20, drilling_steps=[20]).cycle_time_minutes


def test_a_hole_at_or_below_the_threshold_is_one_pass_as_before():
    result = estimate(10)
    assert result.method == "drilling_time_baseline_v1"
    assert result.drilling_steps == [10]
    assert [o.steps for o in result.drilling_options if o.suggested] == [[10]]
    # The pilot + drill alternative is still offered for the engineer to pick.
    assert [6, 10] in [o.steps for o in result.drilling_options]


@pytest.mark.parametrize("steps", [[15, 6, 20], [6, 15], [0, 20], []])
def test_an_unusable_sequence_is_refused_rather_than_timed(steps):
    result = estimate(20, drilling_steps=steps)
    assert result.time_status is TimeStatus.NEEDS_USER_INPUT
    assert result.cycle_time_minutes is None


def test_a_hole_without_dimensions_offers_no_options():
    result = estimate_operation_time(operation_type="drilling", dimensions={"diameter_mm": 20})
    assert result.time_status is TimeStatus.NEEDS_USER_INPUT
    assert result.drilling_options == []


# --- the engineer's choice ------------------------------------------------------


def planned(diameter: float = 20.0) -> PlannedOperation:
    base = PlannedOperation(
        operation_id="OP-001",
        sequence_position=1,
        operation_name=f"Drill Ø{diameter:g}",
        system_suggested_operation="drilling",
        feature_type="hole",
        feature_subtype="through",
        dimensions={"diameter_mm": diameter, "depth_mm": 16.0},
    )
    return base.with_estimate(estimate(diameter))


def test_the_suggested_sequence_is_recorded_on_the_plan():
    operation = planned()
    assert operation.system_drilling_steps == [6, 15, 20]
    assert operation.drilling_steps == [6, 15, 20]
    assert len(operation.system_drilling_options) == 3


def test_choosing_another_offered_sequence_uses_the_systems_time_for_it_once_accepted():
    operation = planned()
    single = next(o for o in operation.system_drilling_options if o.steps == [20])

    chosen = operation.with_drilling_steps([20])
    assert chosen.user_drilling_steps == [20]
    assert chosen.system_drilling_steps == [6, 15, 20]  # the suggestion is untouched
    assert chosen.user_action is UserAction.EDITED
    assert chosen.system_time_accepted is False
    assert chosen.final_cycle_time is None  # nothing is committed by choosing

    accepted = chosen.model_copy(update={"system_time_accepted": True})
    assert accepted.final_cycle_time == single.cycle_time_minutes
    assert accepted.system_suggested_cycle_time == operation.system_suggested_cycle_time


def test_choosing_the_suggestion_again_is_not_a_correction():
    operation = planned().with_drilling_steps([20]).with_drilling_steps([6, 15, 20])
    assert operation.user_drilling_steps is None
    assert operation.user_action is UserAction.ACCEPTED


def test_a_sequence_the_system_never_offered_has_no_system_time():
    operation = planned().with_drilling_steps([8, 20]).model_copy(
        update={"system_time_accepted": True}
    )
    assert operation.system_time_applies is False
    assert operation.final_cycle_time is None
    assert operation.with_times(cycle_time_minutes=1.5).final_cycle_time == 1.5


def test_costing_follows_the_chosen_sequence():
    operation = planned().with_drilling_steps([20]).model_copy(
        update={"system_time_accepted": True}
    )
    plan = ManufacturingPlan(operations=[operation])
    single = next(o for o in operation.system_drilling_options if o.steps == [20])
    (requirement,) = plan.to_operation_requirements(1)
    assert requirement.cycle_time_minutes == single.cycle_time_minutes


def test_the_choice_survives_the_api_round_trip_and_is_costed():
    operation = planned().with_drilling_steps([6, 20]).model_copy(
        update={"system_time_accepted": True}
    )
    plan = ManufacturingPlan(operations=[operation])
    response = client.post(
        "/operations/plan-cost-preview?rate_set_id=poc-default&quantity=1",
        json=plan.model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    pilot = next(o for o in operation.system_drilling_options if o.steps == [6, 20])
    assert response.json()["time"]["total_cycle_time_minutes"] == pilot.cycle_time_minutes


def test_the_estimate_endpoint_times_an_engineers_sequence():
    body = {
        "operations": [
            {
                "operation_id": "OP-1",
                "operation_type": "drilling",
                "feature_type": "hole",
                "feature_subtype": "through",
                "dimensions": {"diameter_mm": 20, "depth_mm": 16},
                "drilling_steps": [20],
            }
        ]
    }
    result = client.post("/operations/estimate-time", json=body).json()["estimates"][0]["estimate"]
    assert result["drilling_steps"] == [20]
    assert result["method"] == "drilling_time_baseline_v1"


# --- the browser ------------------------------------------------------------------


def test_the_plan_builder_copies_the_drilling_fields_from_the_estimate():
    source = (STATIC / "plan.js").read_text(encoding="utf-8")
    assert "operation.system_drilling_steps = estimate.drilling_steps" in source
    assert "operation.system_drilling_options = estimate.drilling_options" in source


def test_the_time_profile_screen_exposes_the_drilling_rule():
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    for param in ("drilling_pilot_threshold_mm", "drilling_pilot_diameter_mm", "drilling_max_step_ratio"):
        assert f'data-param="{param}"' in markup
