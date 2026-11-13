"""Welding priced by time: a weld the engineer describes, timed and costed by the hour.

What is checked: the weld formula and every refusal it makes (no welding
parameters in the profile, no length or size); that a weld line's system time
applies only while it is still a weld; that choosing time pricing takes the
welding stage out of stage pricing so it is never charged twice; that a timed
stage with nothing timed is refused rather than priced at zero; that only a
timeable stage may be timed; and that sending no pricing choice is exactly the
stage-rate behaviour it always was. And that butt-weld edge preparation and
weld finishing are timed from the weld's length, and that weld finishing is
charged once: inside the welds' time or by its stage rate, never both, never
neither — and that clamping and tacking is charged once against the fit-up
stage in the same way.
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
    # Fit-up and weld finishing are timeable too, but only as part of the welds'
    # own time (sections 7 and 8), so their operations are welding operations.
    # Sheet cutting and bending have their own time model, measured from the flat
    # pattern (tests/test_sheet_pricing.py); injection moulding from the measured
    # governing wall (tests/test_moulding_times.py); die casting from the measured
    # casting modulus (tests/test_die_casting.py); stock cutting still has none.
    assert TIMEABLE_STAGES == {
        "welding": "welding",
        "fit_up": "welding",
        "weld_finishing": "welding",
        "sheet_cutting": "sheet_cutting",
        "bending": "bending",
        "moulding": "injection_moulding",
        "die_casting": "die_casting",
    }
    with pytest.raises(ValueError, match="only welding, sheet cutting, bending, injection moulding and high-pressure die casting have a time model"):
        timed_stages({"cutting": {"pricing": "timed"}}, ["cutting", "fit_up", "welding"])
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
    # Cutting, not fit-up: fit-up is now timeable with welding (section 8).
    publish_weld_shop()
    response = cost(stage_inputs={"cutting": {"pricing": "timed"}})
    assert response.status_code == 400
    assert "only welding, sheet cutting, bending, injection moulding and high-pressure die casting have a time model" in response.json()["detail"]


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
    assert (
        "repositioning 0.5 min per weld, added to the cycle time (per part, not setup); not "
        "fit-up work — no route stage prices it, so it stays in the weld's time"
    ) in estimate.assumptions
    assert (
        "clamping and tacking 1.25 min per weld, added to the cycle time (per part, not setup); "
        "the work a route's fit-up stage prices, so it is charged here only when fit-up is "
        "priced by weld time"
    ) in estimate.assumptions
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

    def post(stage_inputs):
        return client.post(
            "/cost/cad-part",
            files={"file": (f"{NAME}.step", step(NAME), "application/octet-stream")},
            data={**FORM, "rate_set_id": "weld-shop",
                  "process_classification": json.dumps(classification_for("cutting_welding")),
                  "manufacturing_plan": plan.model_dump_json(),
                  "stage_inputs": json.dumps(stage_inputs)},
        )

    # This weld's 9.75 min holds 1.25 min of clamping and tacking, and the route's
    # fit-up stage ("Fit-up and tacking") is a stage-rate line. Timing welding
    # alone used to be accepted here and charged tacking twice; it is refused.
    twice = post({"welding": {"pricing": "timed"}})
    assert twice.status_code == 422, twice.text
    assert twice.json()["detail"]["error"] == "fit_up_charged_twice"
    # Timing fit-up with welding charges tacking once, in the weld.
    costed = post({"welding": {"pricing": "timed"}, "fit_up": {"pricing": "timed"}})
    assert costed.status_code == 200, costed.text
    assert [s["stage"] for s in costed.json()["part_cost"]["stage_costs"]] == [
        "cutting", "weld_finishing"
    ]
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


# --- 7. edge preparation and weld finishing from the weld's length -----------

PREP = "welding_butt_prep_mm_per_min"
FINISHING = "welding_finishing_mm_per_min"
PREP_AND_FINISHING = {PREP: 100.0, FINISHING: 200.0}
BUTT = WeldSpec(weld_type="butt", process="mig", length_mm=600.0, size_mm=8.0)


def timed(weld, **extra):
    return estimate_operation_time(
        operation_type="welding", weld=weld, assumptions=TimeAssumptions(**WELDING, **extra)
    )


def test_prep_and_finishing_are_absent_by_default_like_every_welding_figure():
    assert TimeAssumptions().model_dump()[PREP] is None
    assert TimeAssumptions().model_dump()[FINISHING] is None


def test_a_butt_weld_is_charged_edge_preparation_and_finishing_from_its_length():
    estimate = timed(BUTT, **PREP_AND_FINISHING)
    # arc: ceil(8 / 6) = 2 passes; 600 x 2 / 300 = 4 min; / 0.5 = 8 min.
    # prep: 600 / 100 = 6 min. finishing: 600 / 200 = 3 min. cycle = 17 min.
    assert estimate.cycle_time_minutes == 17.0
    assert estimate.setup_time_minutes == 15.0
    assert estimate.parameters["weld_arc_cycle_minutes"] == 8.0
    assert estimate.parameters["weld_prep_minutes"] == 6.0
    assert estimate.parameters["weld_finishing_minutes"] == 3.0
    assert estimate.parameters[PREP] == 100.0 and estimate.parameters[FINISHING] == 200.0
    assert any(line.startswith("edge preparation 6.00 min = 600 mm / 100 mm/min")
               for line in estimate.assumptions)
    assert any(line.startswith("finishing 3.00 min = 600 mm / 200 mm/min")
               for line in estimate.assumptions)
    assert "cycle 17.00 min = 4.00 / 0.5 + 6.00 + 3.00" in estimate.assumptions


def test_a_fillet_weld_is_never_charged_edge_preparation():
    estimate = timed(WELD, **PREP_AND_FINISHING)
    # 8 min of arc-derived time + 600 / 200 = 3 min finishing; no preparation.
    assert estimate.cycle_time_minutes == 11.0
    assert "weld_prep_minutes" not in estimate.parameters and PREP not in estimate.parameters
    assert estimate.parameters["weld_finishing_minutes"] == 3.0
    assert any("no edge preparation" in line and "butt welds only" in line
               for line in estimate.assumptions)
    assert "cycle 11.00 min = 4.00 / 0.5 + 3.00" in estimate.assumptions
    # A prep rate alone changes no fillet weld's time.
    assert timed(WELD, **{PREP: 100.0}).cycle_time_minutes == 8.0


def test_every_component_is_named_with_its_parameters_in_the_explanation():
    estimate = timed(BUTT, **HANDLING, **PREP_AND_FINISHING)
    # 8 (arc / factor) + 0.5 + 1.25 (handling) + 6 (prep) + 3 (finishing) = 18.75.
    assert estimate.cycle_time_minutes == 18.75
    assert "cycle 18.75 min = 4.00 / 0.5 + 0.5 + 1.25 + 6.00 + 3.00" in estimate.assumptions
    joined = " | ".join(estimate.assumptions)
    for phrase in ("arc time 4.00 min = 600 × 2 / 300", "arc-on factor 0.5",
                   "repositioning 0.5 min", "clamping and tacking 1.25 min",
                   "edge preparation 6.00 min", "finishing 3.00 min"):
        assert phrase in joined


def test_prep_and_finishing_scale_with_length_not_with_passes():
    thick = WeldSpec(weld_type="butt", process="mig", length_mm=600.0, size_mm=20.0)
    estimate = timed(thick, **PREP_AND_FINISHING)
    # ceil(20 / 6) = 4 passes; 600 x 4 / 300 = 8 min arc; / 0.5 = 16. + 6 + 3 = 25.
    assert estimate.cycle_time_minutes == 25.0
    assert estimate.parameters["weld_prep_minutes"] == 6.0


def test_absent_prep_and_finishing_give_exactly_the_earlier_estimate():
    """None is not zero minutes added: the whole serialized estimate is unchanged."""
    for weld in (WELD, BUTT):
        for extra in ({}, HANDLING):
            before = timed(weld, **extra)
            explicit = timed(weld, **extra, **{PREP: None, FINISHING: None})
            assert before.model_dump_json() == explicit.model_dump_json()
            assert "weld_prep_minutes" not in before.parameters
            assert "weld_finishing_minutes" not in before.parameters
    # And still the figures pinned before these parameters existed.
    assert timed(WELD).cycle_time_minutes == 8.0
    assert timed(WELD, **HANDLING).cycle_time_minutes == 9.75
    assert "cycle 9.75 min = 4.00 / 0.5 + 0.5 + 1.25" in timed(WELD, **HANDLING).assumptions


def test_prep_and_finishing_alone_do_not_make_a_profile_time_a_weld():
    estimate = estimate_operation_time(
        operation_type="welding", weld=BUTT, assumptions=TimeAssumptions(**PREP_AND_FINISHING)
    )
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.cycle_time_minutes is None
    assert PREP not in estimate.missing and FINISHING not in estimate.missing


def test_a_zero_or_negative_rate_is_refused():
    for value in (0.0, -1.0):
        with pytest.raises(ValueError):
            TimeAssumptions(**WELDING, **{PREP: value})
        with pytest.raises(ValueError):
            TimeAssumptions(**WELDING, **{FINISHING: value})


def finishing_weld_line(weld=WELD, *, accepted=True, extra=PREP_AND_FINISHING):
    estimate = timed(weld, **extra)
    line = manual_operation(
        operation_id="OP-WELD-1", sequence_position=1, operation_type="welding",
        operation_name=f"{weld.weld_type} weld",
    ).with_weld(weld, estimate)
    return line.accepted(use_estimated_time=True) if accepted else line


def test_prep_and_finishing_change_no_acceptance_rule():
    line = finishing_weld_line(BUTT, accepted=False)
    assert line.system_time_accepted is False
    assert line.final_cycle_time is None and not line.costable
    assert line.system_suggested_cycle_time == 17.0
    assert line.system_time_parameters["weld_finishing_minutes"] == 3.0
    accepted = line.accepted(use_estimated_time=True)
    assert accepted.final_cycle_time == 17.0 and accepted.final_setup_time == 15.0
    again = accepted.with_weld(BUTT, timed(BUTT, **PREP_AND_FINISHING))
    assert again.system_time_accepted is False and again.final_cycle_time is None


def test_weld_finishing_is_timed_only_together_with_welding():
    route = ["cutting", "fit_up", "welding", "weld_finishing"]
    both = {"welding": {"pricing": "timed"}, "weld_finishing": {"pricing": "timed"}}
    assert timed_stages(both, route) == {"welding": "welding", "weld_finishing": "welding"}
    with pytest.raises(ValueError, match="only when 'welding' is too"):
        timed_stages({"weld_finishing": {"pricing": "timed"}}, route)


def cost_with(line, stage_inputs):
    plan = committed_plan(NAME)
    plan = plan.model_copy(update={"operations": [*plan.operations, line]})
    return client.post(
        "/cost/cad-part",
        files={"file": (f"{NAME}.step", step(NAME), "application/octet-stream")},
        data={**FORM, "rate_set_id": "weld-shop",
              "process_classification": json.dumps(classification_for("cutting_welding")),
              "manufacturing_plan": plan.model_dump_json(),
              "stage_inputs": json.dumps(stage_inputs)},
    )


WELDING_TIMED = {"welding": {"pricing": "timed"}}
BOTH_TIMED = {"welding": {"pricing": "timed"}, "weld_finishing": {"pricing": "timed"}}


def test_a_weld_carrying_finishing_beside_a_priced_finishing_stage_is_refused_not_charged_twice():
    publish_weld_shop()
    before = client.get("/estimates").json()["total"]
    response = cost_with(finishing_weld_line(), WELDING_TIMED)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "weld_finishing_charged_twice"
    assert detail["stage"] == "weld_finishing"
    assert detail["operations"] == ["OP-WELD-1"]
    # Pinned word for word: generalising the guard to fit-up changed none of it.
    assert detail["message"] == (
        "No estimate was produced: 1 costed weld(s) carry a finishing time from their time "
        "profile, and Weld grinding and cleaning is also priced by its stage rate, so the same "
        "grinding would be charged twice. Choose one: price weld finishing by weld time, or time "
        "the welds with a profile that has no finishing rate."
    )
    assert client.get("/estimates").json()["total"] == before


def test_weld_finishing_timed_is_charged_once_inside_the_welds_time():
    publish_weld_shop()
    response = cost_with(finishing_weld_line(), BOTH_TIMED)
    assert response.status_code == 200, response.text
    part_cost = response.json()["part_cost"]
    # The finishing stage rate is gone; its work is in the weld's 11 min.
    assert [line["stage"] for line in part_cost["stage_costs"]] == ["cutting", "fit_up"]
    assert part_cost["total_stage_cost"] == pytest.approx(5 * (25 + 40))
    weld = [o for o in part_cost["operations"] if o["process_type"] == "welding"][0]
    assert weld["cycle_time_minutes"] == 11.0
    run_hours = 11.0 * 5 / 60
    expected = 400 * run_hours + 400 * weld["setup_time_minutes"] / 60 + 150 * run_hours
    assert weld["total_operation_cost"] == pytest.approx(expected, abs=0.01)


def test_weld_finishing_timed_with_a_weld_that_carries_no_finishing_is_refused_not_dropped():
    publish_weld_shop()
    response = cost_with(weld_line(), BOTH_TIMED)  # timed by a profile with no finishing rate
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "timed_stage_not_costed"
    assert detail["stages"] == ["weld_finishing"]
    assert detail["operations"] == ["OP-WELD-1"]


def test_weld_finishing_timed_without_welding_timed_is_refused():
    publish_weld_shop()
    response = cost_with(finishing_weld_line(), {"weld_finishing": {"pricing": "timed"}})
    assert response.status_code == 400
    assert "only when 'welding' is too" in response.json()["detail"]


def test_a_typed_weld_time_is_the_engineers_whole_figure_under_either_choice():
    publish_weld_shop()
    typed = finishing_weld_line().model_copy(update={"user_cycle_time": 14.0})
    for stage_inputs in (WELDING_TIMED, BOTH_TIMED):
        response = cost_with(typed, stage_inputs)
        assert response.status_code == 200, response.text
        weld = [o for o in response.json()["part_cost"]["operations"]
                if o["process_type"] == "welding"][0]
        assert weld["cycle_time_minutes"] == 14.0


def test_a_weld_without_finishing_on_the_stage_rate_route_is_costed_exactly_as_before():
    publish_weld_shop()
    response = cost_with(weld_line(), WELDING_TIMED)
    assert response.status_code == 200, response.text
    part_cost = response.json()["part_cost"]
    assert [line["stage"] for line in part_cost["stage_costs"]] == [
        "cutting", "fit_up", "weld_finishing"
    ]
    assert part_cost["total_stage_cost"] == pytest.approx(5 * (25 + 40 + 50))


def test_the_snapshot_records_the_prep_and_finishing_rates():
    publish_weld_shop()
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    published = client.post("/time-profiles", json={
        "time_profile_id": "weld-finishing-time", "name": "Weld prep and finishing",
        "version": "1.0.0", "description": "test", "is_illustrative": True,
        "assumptions": {**baseline, **WELDING, **PREP_AND_FINISHING},
    })
    assert published.status_code == 201, published.text
    response = client.post(
        "/operations/estimate-time?time_profile_id=weld-finishing-time",
        json={"operations": [{"operation_id": "W1", "operation_type": "welding",
                              "weld": BUTT.model_dump()}]},
    )
    estimate = response.json()["estimates"][0]["estimate"]
    assert estimate["cycle_time_minutes"] == 17.0
    assert estimate["time_profile_id"] == "weld-finishing-time"
    line = manual_operation(
        operation_id="OP-WELD-1", sequence_position=1, operation_type="welding",
        operation_name="Butt weld",
    ).with_weld(BUTT, OperationTimeEstimate.model_validate(estimate)).accepted(
        use_estimated_time=True
    )
    costed = cost_with(line, BOTH_TIMED)
    assert costed.status_code == 200, costed.text
    stored = client.get(f"/estimates/{costed.json()['estimate_id']}").json()
    snapshot = stored["time_profile_snapshot"]["profiles"]["weld-finishing-time@1.0.0"]
    assert snapshot["assumptions"][PREP] == 100.0
    assert snapshot["assumptions"][FINISHING] == 200.0
    assert stored["request_snapshot"]["stage_inputs"] == BOTH_TIMED


def test_the_illustrative_profile_adds_prep_and_finishing_in_a_third_version():
    script = _publish_script()
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    second = script.build_profile(baseline, "1.1.0")
    third = script.build_profile(baseline, "1.2.0")
    assert list(script.VERSIONS) == ["1.0.0", "1.1.0", "1.2.0"]
    differing = {k for k in third["assumptions"] if third["assumptions"][k] != second["assumptions"][k]}
    assert differing == {PREP, FINISHING}
    assert third["is_illustrative"] is True
    assert "ESTIMATED" in third["description"] and "no published source" in third["description"]
    for version in script.VERSIONS:
        assert client.post("/time-profiles", json=script.build_profile(baseline, version)).status_code == 201

    def cycle(version, weld):
        response = client.post(
            f"/operations/estimate-time?time_profile_id={script.PROFILE_ID}"
            f"&time_profile_version={version}",
            json={"operations": [{"operation_id": "W1", "operation_type": "welding",
                                  "weld": weld.model_dump()}]},
        )
        return response.json()["estimates"][0]["estimate"]["cycle_time_minutes"]

    # Fillet: 600 x 2 / 300 = 4 min arc; / 0.45 = 8.889; + 0.5 + 1.0 = 10.389;
    # + 600 / 200 = 3 min finishing = 13.389. No preparation.
    assert cycle("1.1.0", WELD) == 10.39
    assert cycle("1.2.0", WELD) == 13.39
    # Butt, same length and size: + 600 / 100 = 6 min preparation = 19.389.
    assert cycle("1.1.0", BUTT) == 10.39
    assert cycle("1.2.0", BUTT) == 19.39


def test_the_seeded_baseline_has_no_prep_or_finishing_rate():
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    assert baseline[PREP] is None and baseline[FINISHING] is None


# --- 8. tacking is charged once: by the fit-up stage rate or inside the welds --

FIT_UP_TIMED = {"welding": {"pricing": "timed"}, "fit_up": {"pricing": "timed"}}


def tack_weld_line():
    return finishing_weld_line(WELD, extra={TACK: 1.25})


def test_fit_up_is_timed_only_together_with_welding():
    route = ["cutting", "fit_up", "welding", "weld_finishing"]
    assert timed_stages(FIT_UP_TIMED, route) == {"welding": "welding", "fit_up": "welding"}
    with pytest.raises(ValueError, match="'fit_up' can be priced by time only when 'welding' is too"):
        timed_stages({"fit_up": {"pricing": "timed"}}, route)


def test_the_tack_component_is_recorded_under_its_parameter_and_repositioning_is_unguarded():
    estimate = timed(WELD, **HANDLING)
    # The tack minutes are the component fit-up's guard reads; repositioning is
    # in the cycle too, but it is no stage's work.
    assert estimate.parameters[TACK] == 1.25
    assert any(line.startswith("repositioning 0.5 min") and "not fit-up work" in line
               for line in estimate.assumptions)
    assert any(line.startswith("clamping and tacking 1.25 min")
               and "only when fit-up is priced by weld time" in line
               for line in estimate.assumptions)


def test_a_weld_carrying_tack_beside_a_priced_fit_up_stage_is_refused_not_charged_twice():
    publish_weld_shop()
    before = client.get("/estimates").json()["total"]
    response = cost_with(tack_weld_line(), WELDING_TIMED)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "fit_up_charged_twice"
    assert detail["stage"] == "fit_up"
    assert detail["operations"] == ["OP-WELD-1"]
    assert detail["message"] == (
        "No estimate was produced: 1 costed weld(s) carry a tack time from their time profile, "
        "and Fit-up and tacking is also priced by its stage rate, so the same tacking would be "
        "charged twice. Choose one: price fit-up by weld time, or time the welds with a profile "
        "that has no tack time."
    )
    assert client.get("/estimates").json()["total"] == before


def test_repositioning_alone_is_never_refused_beside_a_priced_fit_up_stage():
    publish_weld_shop()
    line = finishing_weld_line(WELD, extra={REPOSITION: 0.5})
    response = cost_with(line, WELDING_TIMED)
    assert response.status_code == 200, response.text
    assert [s["stage"] for s in response.json()["part_cost"]["stage_costs"]] == [
        "cutting", "fit_up", "weld_finishing"
    ]


def test_fit_up_timed_with_a_weld_that_carries_no_tack_is_refused_not_dropped():
    publish_weld_shop()
    response = cost_with(weld_line(), FIT_UP_TIMED)  # timed by a profile with no tack time
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "timed_stage_not_costed"
    assert detail["stages"] == ["fit_up"]
    assert detail["operations"] == ["OP-WELD-1"]
    assert "their tacking would be charged nowhere" in detail["message"]


def test_fit_up_timed_without_welding_timed_is_refused():
    publish_weld_shop()
    response = cost_with(tack_weld_line(), {"fit_up": {"pricing": "timed"}})
    assert response.status_code == 400
    assert "only when 'welding' is too" in response.json()["detail"]


def test_a_typed_weld_time_is_the_engineers_whole_figure_for_tacking_too():
    publish_weld_shop()
    typed = tack_weld_line().model_copy(update={"user_cycle_time": 14.0})
    for stage_inputs in (WELDING_TIMED, FIT_UP_TIMED):
        response = cost_with(typed, stage_inputs)
        assert response.status_code == 200, response.text


def test_timed_fit_up_on_the_hand_entered_path_is_refused():
    """With operation requirements instead of a plan, no weld can show a tack time."""
    publish_weld_shop()
    requirements = json.dumps([{
        "operation_id": "W1", "operation_name": "Weld", "process_type": "welding",
        "setup_time_minutes": 15, "cycle_time_minutes": 10, "quantity": 5,
    }])

    def post(stage_inputs):
        return client.post(
            "/cost/cad-part",
            files={"file": (f"{NAME}.step", step(NAME), "application/octet-stream")},
            data={**FORM, "rate_set_id": "weld-shop",
                  "process_classification": json.dumps(classification_for("cutting_welding")),
                  "operation_requirements": requirements,
                  "stage_inputs": json.dumps(stage_inputs)},
        )

    refused = post(FIT_UP_TIMED)
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "timed_stage_not_costed"
    assert detail["stages"] == ["fit_up"]
    assert "needs a manufacturing plan whose welds carry a tack time" in detail["message"]
    both = post({**FIT_UP_TIMED, "weld_finishing": {"pricing": "timed"}})
    assert both.status_code == 422
    assert both.json()["detail"]["stages"] == ["fit_up", "weld_finishing"]
    # Welding alone timed on this path is as before: costed, every stage rate kept.
    plain = post(WELDING_TIMED)
    assert plain.status_code == 200, plain.text
    assert [s["stage"] for s in plain.json()["part_cost"]["stage_costs"]] == [
        "cutting", "fit_up", "weld_finishing"
    ]


def test_welding_and_fit_up_timed_under_the_illustrative_v1_1_0_charge_tacking_once():
    script = _publish_script()
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    for version in ("1.0.0", "1.1.0"):
        assert client.post("/time-profiles", json=script.build_profile(baseline, version)).status_code == 201
    response = client.post(
        f"/operations/estimate-time?time_profile_id={script.PROFILE_ID}&time_profile_version=1.1.0",
        json={"operations": [{"operation_id": "W1", "operation_type": "welding",
                              "weld": WELD.model_dump()}]},
    )
    estimate = response.json()["estimates"][0]["estimate"]
    # 600 x 2 / 300 = 4 min arc; / 0.45 = 8.889; + 0.5 reposition + 1.0 tack = 10.39.
    assert estimate["cycle_time_minutes"] == 10.39
    assert estimate["parameters"][TACK] == 1.0
    line = manual_operation(
        operation_id="OP-WELD-1", sequence_position=1, operation_type="welding",
        operation_name="Fillet weld",
    ).with_weld(WELD, OperationTimeEstimate.model_validate(estimate)).accepted(
        use_estimated_time=True
    )
    publish_weld_shop()
    assert cost_with(line, WELDING_TIMED).json()["detail"]["error"] == "fit_up_charged_twice"
    response = cost_with(line, FIT_UP_TIMED)
    assert response.status_code == 200, response.text
    part_cost = response.json()["part_cost"]
    # No fit-up stage line: 5 off x (25 cutting + 50 weld finishing).
    assert [s["stage"] for s in part_cost["stage_costs"]] == ["cutting", "weld_finishing"]
    assert part_cost["total_stage_cost"] == pytest.approx(5 * (25 + 50))
    weld = [o for o in part_cost["operations"] if o["process_type"] == "welding"][0]
    assert weld["cycle_time_minutes"] == 10.39
    run_hours = 10.39 * 5 / 60
    expected = 400 * run_hours + 400 * weld["setup_time_minutes"] / 60 + 150 * run_hours
    assert weld["total_operation_cost"] == pytest.approx(expected, abs=0.01)


def test_all_three_weld_stages_timed_under_v1_2_0_leave_only_cutting_on_a_stage_rate():
    publish_weld_shop()
    line = finishing_weld_line(BUTT, extra={**HANDLING, **PREP_AND_FINISHING})
    # A weld with tack and finishing, with fit-up timed but finishing on its rate.
    assert cost_with(line, FIT_UP_TIMED).json()["detail"]["error"] == "weld_finishing_charged_twice"
    response = cost_with(line, {**FIT_UP_TIMED, "weld_finishing": {"pricing": "timed"}})
    assert response.status_code == 200, response.text
    part_cost = response.json()["part_cost"]
    assert [s["stage"] for s in part_cost["stage_costs"]] == ["cutting"]
    # 8 + 0.5 + 1.25 + 6 prep + 3 finishing.
    weld = [o for o in part_cost["operations"] if o["process_type"] == "welding"][0]
    assert weld["cycle_time_minutes"] == 18.75
