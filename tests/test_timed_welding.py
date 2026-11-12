"""Welding priced by time: a weld the engineer describes, timed and costed by the hour.

What is checked: the weld formula and every refusal it makes (no welding
parameters in the profile, no length or size); that a weld line's system time
applies only while it is still a weld; that choosing time pricing takes the
welding stage out of stage pricing so it is never charged twice; that a timed
stage with nothing timed is refused rather than priced at zero; that only a
timeable stage may be timed; and that sending no pricing choice is exactly the
stage-rate behaviour it always was.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import manual_operation
from cad_costing_agent.operation_time import (
    TimeAssumptions,
    TimeStatus,
    WeldSpec,
    estimate_operation_time,
)
from cad_costing_agent.stage_costing import TIMEABLE_STAGES, timed_stages
from tests.test_process_classification import FORM, committed_plan, step
from tests.test_stage_costing import NAME, classification_for

client = TestClient(app)

WELDING = dict(
    welding_mig_travel_speed_mm_per_min=300.0,
    welding_tig_travel_speed_mm_per_min=100.0,
    welding_mm_per_pass=6.0,
    welding_arc_on_factor=0.5,
    welding_setup_minutes=15.0,
)
WELD = WeldSpec(weld_type="fillet", process="mig", length_mm=600.0, size_mm=8.0)
STAGE_RATES = {
    "cutting": {"unit_basis": "per_piece", "unit_rate": 25.0},
    "fit_up": {"unit_basis": "per_piece", "unit_rate": 40.0},
    "welding": {"unit_basis": "per_piece", "unit_rate": 100.0},
    "weld_finishing": {"unit_basis": "per_piece", "unit_rate": 50.0},
}
WELD_RATE = {"machine_rate_per_hour": 400.0, "labor_rate_per_hour": 150.0}


# --- 1. the formula -----------------------------------------------------------


def test_a_weld_is_timed_from_its_length_size_and_the_profiles_parameters():
    estimate = estimate_operation_time(
        operation_type="welding", weld=WELD, assumptions=TimeAssumptions(**WELDING)
    )
    # ceil(8 / 6) = 2 passes; 600 x 2 / 300 = 4 min of arc; / 0.5 = 8 min.
    assert estimate.time_status is TimeStatus.ESTIMATED
    assert estimate.cycle_time_minutes == 8.0
    assert estimate.setup_time_minutes == 15.0
    assert estimate.method == "welding_time_baseline_v1"
    assert estimate.parameters["weld_passes"] == 2.0
    assert any("not read from the CAD" in line for line in estimate.evidence)


def test_tig_uses_its_own_travel_speed():
    tig = WeldSpec(weld_type="butt", process="tig", length_mm=100.0, size_mm=3.0)
    estimate = estimate_operation_time(
        operation_type="welding", weld=tig, assumptions=TimeAssumptions(**WELDING)
    )
    # 1 pass; 100 / 100 = 1 min of arc; / 0.5 = 2 min.
    assert estimate.cycle_time_minutes == 2.0


def test_the_built_in_baseline_assumes_no_welding_figure():
    """The seeded profile describes a machining shop; it does not time a weld."""
    assert all(getattr(TimeAssumptions(), name) is None for name in WELDING)
    estimate = estimate_operation_time(operation_type="welding", weld=WELD)
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.cycle_time_minutes is None
    assert "no welding parameters" in estimate.reason
    assert "welding_mig_travel_speed_mm_per_min" in estimate.missing


@pytest.mark.parametrize(
    "weld, missing",
    [
        (WeldSpec(size_mm=8.0), ["weld_length_mm"]),
        (WeldSpec(length_mm=600.0), ["weld_size_mm"]),
        (None, ["weld_length_mm", "weld_size_mm"]),
    ],
)
def test_a_weld_missing_its_length_or_size_is_never_given_a_time(weld, missing):
    estimate = estimate_operation_time(
        operation_type="welding", weld=weld, assumptions=TimeAssumptions(**WELDING)
    )
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.cycle_time_minutes is None and estimate.setup_time_minutes is None
    assert estimate.missing == missing


def test_the_weld_time_endpoint_takes_a_weld():
    body = {
        "assumptions": WELDING,
        "operations": [{"operation_id": "W1", "operation_type": "welding",
                        "weld": WELD.model_dump()}],
    }
    response = client.post("/operations/estimate-time", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["estimates"][0]["estimate"]["cycle_time_minutes"] == 8.0


# --- 2. a weld line in the plan -----------------------------------------------


def weld_line(*, accepted: bool = True):
    estimate = estimate_operation_time(
        operation_type="welding", weld=WELD, assumptions=TimeAssumptions(**WELDING)
    )
    line = manual_operation(
        operation_id="OP-WELD-1", sequence_position=1, operation_type="welding",
        operation_name="Fillet weld, base to upright",
    ).with_weld(WELD, estimate)
    return line.accepted(use_estimated_time=True) if accepted else line


def test_a_weld_lines_time_is_the_systems_but_the_operation_is_not_a_suggestion():
    line = weld_line()
    # No rule proposed welding, so nothing claims it did.
    assert line.system_suggested_operation is None
    assert line.system_time_operation_type == "welding"
    assert line.system_time_applies
    assert line.final_cycle_time == 8.0 and line.final_setup_time == 15.0
    assert line.costable


def test_an_estimate_is_not_committed_until_accepted():
    line = weld_line(accepted=False)
    assert line.final_cycle_time is None and not line.costable


def test_describing_the_weld_again_clears_the_acceptance():
    line = weld_line()
    longer = WeldSpec(weld_type="fillet", process="mig", length_mm=900.0, size_mm=8.0)
    again = line.with_weld(
        longer,
        estimate_operation_time(
            operation_type="welding", weld=longer, assumptions=TimeAssumptions(**WELDING)
        ),
    )
    assert again.system_suggested_cycle_time == 12.0
    assert again.system_time_accepted is False and again.final_cycle_time is None


def test_a_weld_time_does_not_follow_the_line_into_another_process():
    line = weld_line().with_operation_type("milling")
    assert not line.system_time_applies
    assert line.final_cycle_time is None
    assert "estimated time was for welding" in line.not_costable_reason


# --- 3. which stages may be timed --------------------------------------------


def test_only_welding_can_be_timed():
    assert TIMEABLE_STAGES == {"welding": "welding"}
    with pytest.raises(ValueError, match="only welding has a time model"):
        timed_stages({"fit_up": {"pricing": "timed"}}, ["cutting", "fit_up", "welding"])
    with pytest.raises(ValueError, match="must be 'stage_rate' or 'timed'"):
        timed_stages({"welding": {"pricing": "hourly"}}, ["welding"])


def test_absent_pricing_is_the_stage_rate_and_other_routes_are_ignored():
    assert timed_stages({"welding": {"mass_kg": 2.0}}, ["welding"]) == {}
    assert timed_stages({"welding": {"pricing": "timed"}}, ["casting", "fettling"]) == {}
    assert timed_stages({"welding": {"pricing": "timed"}}, ["welding"]) == {"welding": "welding"}


# --- 4. costing the route -----------------------------------------------------


def publish_weld_shop(*, with_welding_rate: bool = True):
    base = client.get("/rates/poc-default").json()
    operations = dict(base["operations"])
    if with_welding_rate:
        operations["welding"] = WELD_RATE
    body = {
        "rate_set_id": "weld-shop", "name": "Weld shop", "version": "1.0.0",
        "currency": "INR", "is_illustrative": False,
        "materials": base["materials"], "operations": operations,
        "additional_costs": {"overhead_rate_percent": 0, "scrap_rate_percent": 0,
                             "tooling_cost_per_batch": 0},
        "stage_rates": STAGE_RATES,
    }
    response = client.post("/rates", json=body)
    assert response.status_code == 201, response.text


def cost(*, stage_inputs=None, with_weld=True):
    plan = committed_plan(NAME)
    if with_weld:
        plan = plan.model_copy(update={"operations": [*plan.operations, weld_line()]})
    data = {**FORM, "rate_set_id": "weld-shop",
            "process_classification": json.dumps(classification_for("cutting_welding")),
            "manufacturing_plan": plan.model_dump_json()}
    if stage_inputs is not None:
        data["stage_inputs"] = json.dumps(stage_inputs)
    return client.post(
        "/cost/cad-part",
        files={"file": (f"{NAME}.step", step(NAME), "application/octet-stream")},
        data=data,
    )


def test_without_a_pricing_choice_welding_is_its_stage_rate_as_before():
    publish_weld_shop()
    part_cost = cost(with_weld=False).json()["part_cost"]
    assert [line["stage"] for line in part_cost["stage_costs"]] == [
        "cutting", "fit_up", "welding", "weld_finishing"
    ]
    # 5 off x (25 + 40 + 100 + 50).
    assert part_cost["total_stage_cost"] == pytest.approx(1075.0)
    assert part_cost["total_operation_cost"] == 0.0


def test_timed_welding_is_costed_by_the_hour_and_never_also_by_its_stage_rate():
    publish_weld_shop()
    response = cost(stage_inputs={"welding": {"pricing": "timed"}})
    assert response.status_code == 200, response.text
    body = response.json()
    part_cost = body["part_cost"]
    assert [line["stage"] for line in part_cost["stage_costs"]] == [
        "cutting", "fit_up", "weld_finishing"
    ]
    assert part_cost["total_stage_cost"] == pytest.approx(5 * (25 + 40 + 50))
    welds = [line for line in part_cost["operations"] if line["process_type"] == "welding"]
    assert len(welds) == 1
    weld = welds[0]
    assert weld["cycle_time_minutes"] == 8.0
    # machine 400/h on setup + run, labour 150/h on run: 5 off x 8 min.
    run_hours = 8.0 * 5 / 60
    expected = 400 * run_hours + 400 * weld["setup_time_minutes"] / 60 + 150 * run_hours
    assert weld["total_operation_cost"] == pytest.approx(expected, abs=0.01)
    # The milling and drilling the route does not include are still not costed.
    assert {line["process_type"] for line in part_cost["operations"]} == {"welding"}
    # Nothing reports welding as outside the route.
    assert not any("welding" in note for note in body.get("route_differences", []))


def test_a_timed_stage_with_no_committed_weld_time_is_refused_not_zero():
    publish_weld_shop()
    response = cost(stage_inputs={"welding": {"pricing": "timed"}}, with_weld=False)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "timed_stage_not_costed"
    assert detail["stages"] == ["welding"]


def test_timed_welding_without_a_welding_rate_is_refused_with_that_reason():
    publish_weld_shop(with_welding_rate=False)
    response = cost(stage_inputs={"welding": {"pricing": "timed"}})
    assert response.status_code == 400
    assert "no rate for process type 'welding'" in response.json()["detail"]


def test_timing_a_stage_without_a_time_model_is_refused():
    publish_weld_shop()
    response = cost(stage_inputs={"fit_up": {"pricing": "timed"}})
    assert response.status_code == 400
    assert "only welding has a time model" in response.json()["detail"]


def test_a_yield_on_a_timed_stage_is_refused_not_dropped():
    # A timed stage is left out of stage pricing, so its yield would vanish
    # silently. Priced by its stage rate it is refused too: welding pours nothing.
    publish_weld_shop()
    before = client.get("/estimates").json()["total"]
    timed = cost(stage_inputs={"welding": {"pricing": "timed", "yield_percent": 80.0}})
    assert timed.status_code == 400
    assert "priced by time" in timed.json()["detail"]
    by_rate = cost(stage_inputs={"welding": {"yield_percent": 80.0}}, with_weld=False)
    assert by_rate.status_code == 400
    assert "Welding" in by_rate.json()["detail"]
    assert client.get("/estimates").json()["total"] == before


def test_the_pricing_choice_is_recorded_with_the_estimate():
    publish_weld_shop()
    estimate_id = cost(stage_inputs={"welding": {"pricing": "timed"}}).json()["estimate_id"]
    stored = client.get(f"/estimates/{estimate_id}").json()
    assert stored["request_snapshot"]["stage_inputs"] == {"welding": {"pricing": "timed"}}
