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

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import manual_operation
from cad_costing_agent.operation_time import (
    OperationTimeEstimate,
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


def test_the_pricing_choice_is_recorded_with_the_estimate():
    publish_weld_shop()
    estimate_id = cost(stage_inputs={"welding": {"pricing": "timed"}}).json()["estimate_id"]
    stored = client.get(f"/estimates/{estimate_id}").json()
    assert stored["request_snapshot"]["stage_inputs"] == {"welding": {"pricing": "timed"}}


# --- 5. per-weld handling: reposition, clamp and tack -------------------------

REPOSITION = "welding_reposition_minutes_per_weld"
TACK = "welding_tack_minutes_per_weld"
HANDLING = {REPOSITION: 0.5, TACK: 1.25}


def weld_time(**extra):
    return estimate_operation_time(
        operation_type="welding", weld=WELD, assumptions=TimeAssumptions(**WELDING, **extra)
    )


def test_the_handling_parameters_are_absent_by_default_like_every_welding_figure():
    assert TimeAssumptions().model_dump()[REPOSITION] is None
    assert TimeAssumptions().model_dump()[TACK] is None


def test_per_weld_handling_is_added_to_the_cycle_time_and_never_to_the_setup():
    estimate = weld_time(**HANDLING)
    # arc: ceil(8 / 6) = 2 passes; 600 x 2 / 300 = 4 min; / 0.5 = 8 min.
    # handling: 0.5 + 1.25 = 1.75 min. cycle = 9.75 min. setup stays the profile's 15.
    assert estimate.cycle_time_minutes == 9.75
    assert estimate.setup_time_minutes == 15.0
    assert estimate.total_time_minutes == 24.75
    assert estimate.method == "welding_time_baseline_v1"
    assert estimate.parameters[REPOSITION] == 0.5
    assert estimate.parameters[TACK] == 1.25
    assert estimate.parameters["weld_arc_cycle_minutes"] == 8.0
    assert estimate.parameters["weld_passes"] == 2.0
    assert "repositioning 0.5 min per weld, added to the cycle time (per part, not setup)" in (
        estimate.assumptions
    )
    assert "clamping and tacking 1.25 min per weld, added to the cycle time (per part, not setup)" in (
        estimate.assumptions
    )
    assert "cycle 9.75 min = 4.00 / 0.5 + 0.5 + 1.25" in estimate.assumptions


@pytest.mark.parametrize("field, minutes, cycle", [(REPOSITION, 0.5, 8.5), (TACK, 1.25, 9.25)])
def test_each_handling_parameter_stands_on_its_own(field, minutes, cycle):
    estimate = weld_time(**{field: minutes})
    assert estimate.cycle_time_minutes == cycle
    other = TACK if field == REPOSITION else REPOSITION
    assert field in estimate.parameters and other not in estimate.parameters


def test_handling_is_per_weld_not_per_pass_or_per_millimetre():
    """A weld three times as long, in two passes, carries the same 1.75 min."""
    long_weld = WeldSpec(weld_type="fillet", process="mig", length_mm=1800.0, size_mm=8.0)
    estimate = estimate_operation_time(
        operation_type="welding", weld=long_weld,
        assumptions=TimeAssumptions(**WELDING, **HANDLING),
    )
    # 1800 x 2 / 300 = 12 min of arc; / 0.5 = 24; + 1.75.
    assert estimate.cycle_time_minutes == 25.75


def test_a_profile_without_handling_gives_exactly_the_earlier_estimate():
    """None is not zero minutes added: the whole estimate is the one it was."""
    estimate = weld_time()
    explicit = weld_time(**{REPOSITION: None, TACK: None})
    assert estimate.model_dump_json() == explicit.model_dump_json()
    assert estimate.cycle_time_minutes == 8.0
    # Exactly the recorded parameters and explanation from before handling existed.
    assert estimate.parameters == {
        "welding_mig_travel_speed_mm_per_min": 300.0,
        "welding_mm_per_pass": 6.0,
        "welding_arc_on_factor": 0.5,
        "welding_setup_minutes": 15.0,
        "weld_length_mm": 600.0,
        "weld_size_mm": 8.0,
        "weld_passes": 2.0,
    }
    assert estimate.assumptions[:4] == [
        "2 pass(es) = ceil(8 mm / 6 mm per pass)",
        "travel speed 300 mm/min (MIG)",
        "arc time 4.00 min = 600 × 2 / 300",
        "arc-on factor 0.5: cycle = arc time / factor",
    ]
    assert len(estimate.assumptions) == 5  # the four above and the setup line
    assert not any("per weld" in line for line in estimate.assumptions)


def test_handling_alone_does_not_make_a_profile_time_a_weld():
    """The handling figures add to an arc time; they are not a weld time."""
    estimate = estimate_operation_time(
        operation_type="welding", weld=WELD, assumptions=TimeAssumptions(**HANDLING)
    )
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.cycle_time_minutes is None
    assert REPOSITION not in estimate.missing and TACK not in estimate.missing


def test_a_negative_handling_time_is_refused():
    with pytest.raises(ValueError):
        TimeAssumptions(**WELDING, **{REPOSITION: -1.0})


def test_handling_changes_no_acceptance_rule():
    estimate = weld_time(**HANDLING)
    line = manual_operation(
        operation_id="OP-WELD-1", sequence_position=1, operation_type="welding",
        operation_name="Fillet weld, base to upright",
    ).with_weld(WELD, estimate)
    # Generating the estimate commits nothing.
    assert line.system_time_accepted is False
    assert line.final_cycle_time is None and not line.costable
    assert line.system_suggested_cycle_time == 9.75
    assert line.system_time_parameters[REPOSITION] == 0.5

    accepted = line.accepted(use_estimated_time=True)
    assert accepted.final_cycle_time == 9.75 and accepted.final_setup_time == 15.0

    # Describing the weld again clears the acceptance, as before.
    again = accepted.with_weld(WELD, estimate)
    assert again.system_time_accepted is False and again.final_cycle_time is None


def publish_handling_profile():
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    body = {
        "time_profile_id": "weld-handling-time", "name": "Weld handling", "version": "1.0.0",
        "description": "test", "is_illustrative": True,
        "assumptions": {**baseline, **WELDING, **HANDLING},
    }
    response = client.post("/time-profiles", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_the_estimate_names_the_profile_and_the_snapshot_records_the_handling_parameters():
    publish_weld_shop()
    stored_profile = publish_handling_profile()
    assert stored_profile["assumptions"][REPOSITION] == 0.5
    assert stored_profile["assumptions"][TACK] == 1.25

    response = client.post(
        "/operations/estimate-time?time_profile_id=weld-handling-time",
        json={"operations": [{"operation_id": "W1", "operation_type": "welding",
                              "weld": WELD.model_dump()}]},
    )
    assert response.status_code == 200, response.text
    estimate = response.json()["estimates"][0]["estimate"]
    assert estimate["cycle_time_minutes"] == 9.75
    assert estimate["time_profile_id"] == "weld-handling-time"
    assert estimate["parameters"][TACK] == 1.25

    line = manual_operation(
        operation_id="OP-WELD-1", sequence_position=1, operation_type="welding",
        operation_name="Fillet weld, base to upright",
    ).with_weld(WELD, OperationTimeEstimate.model_validate(estimate))
    line = line.accepted(use_estimated_time=True)
    plan = committed_plan(NAME)
    plan = plan.model_copy(update={"operations": [*plan.operations, line]})
    costed = client.post(
        "/cost/cad-part",
        files={"file": (f"{NAME}.step", step(NAME), "application/octet-stream")},
        data={**FORM, "rate_set_id": "weld-shop",
              "process_classification": json.dumps(classification_for("cutting_welding")),
              "manufacturing_plan": plan.model_dump_json(),
              "stage_inputs": json.dumps({"welding": {"pricing": "timed"}})},
    )
    assert costed.status_code == 200, costed.text
    weld = [o for o in costed.json()["part_cost"]["operations"] if o["process_type"] == "welding"]
    assert weld[0]["cycle_time_minutes"] == 9.75

    stored = client.get(f"/estimates/{costed.json()['estimate_id']}").json()
    snapshot = stored["time_profile_snapshot"]["profiles"]["weld-handling-time@1.0.0"]
    assert snapshot["assumptions"][REPOSITION] == 0.5
    assert snapshot["assumptions"][TACK] == 1.25


# --- 6. the illustrative profile's versions -----------------------------------


def _publish_script():
    path = Path(__file__).parents[1] / "scripts" / "publish_illustrative_welding_time_profile.py"
    spec = importlib.util.spec_from_file_location("publish_illustrative_welding", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_illustrative_profile_keeps_its_first_version_and_adds_handling_in_a_new_one():
    script = _publish_script()
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    first = script.build_profile(baseline, "1.0.0")
    second = script.build_profile(baseline, "1.1.0")

    # 1.0.0 is what was published before handling existed: no handling figure.
    assert first["assumptions"][REPOSITION] is None and first["assumptions"][TACK] is None
    assert first["assumptions"]["welding_arc_on_factor"] == 0.45
    # 1.1.0 differs from it by the two handling parameters only.
    differing = {
        key for key in second["assumptions"] if second["assumptions"][key] != first["assumptions"][key]
    }
    assert differing == {REPOSITION, TACK}
    assert second["is_illustrative"] is True and first["is_illustrative"] is True
    assert "ESTIMATED" in second["description"] and "no published source" in second["description"]

    for profile in (first, second):
        assert client.post("/time-profiles", json=profile).status_code == 201
    # A published version is immutable; the script checks for it instead of posting again.
    assert client.post("/time-profiles", json=second).status_code == 409

    def cycle(version):
        response = client.post(
            f"/operations/estimate-time?time_profile_id={script.PROFILE_ID}"
            f"&time_profile_version={version}",
            json={"operations": [{"operation_id": "W1", "operation_type": "welding",
                                  "weld": WELD.model_dump()}]},
        )
        return response.json()["estimates"][0]["estimate"]["cycle_time_minutes"]

    # 600 x 2 / 300 = 4 min of arc; / 0.45 = 8.89. Then + 0.5 + 1.0 = 10.39.
    assert cycle("1.0.0") == 8.89
    assert cycle("1.1.0") == 10.39


def test_the_seeded_baseline_still_times_no_weld():
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    assert all(baseline[name] is None for name in (*WELDING, REPOSITION, TACK))
