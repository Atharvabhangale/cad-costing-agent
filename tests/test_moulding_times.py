"""Injection moulding timed from the measured governing wall, and priced by time.

What is checked:

1. The governing wall — the largest ball found inside the material at the middle
   of a sampled wall — on three fixtures whose answer was worked out by hand,
   and that collecting the spans leaves the minimum-wall measurement unchanged.
2. The cooling, shot and per-part times against hand-computed values (1e-6 s),
   the cavity count dividing the shot exactly, every refusal (no moulding
   parameters, no cavities, no thermal row for the material), the parameters
   absent by default so the seeded profile times no moulding and every other
   estimate is exactly what it was, serialized whole.
3. The moulding line: one fixed-id operation, proposed and never accepted.
4. Pricing: left on its stage rate, moulding is exactly what it was; priced by
   time, the press is charged once by the line's time, the material once on the
   moulding stage line at the rate set's material price (with the engineer's
   runner yield), and the mould's one-off charge, amortised, still applied — a
   hand-worked total. The guards: no committed time, a cavity count the line was
   not timed for, a line timed for another material.
5. Boundaries, the published script, and the browser's mirrors.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import math
import re
from functools import lru_cache
from pathlib import Path

import pytest
import trimesh
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.features.wall_thickness import measure_governing_wall, measure_wall_thickness
from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import ManufacturingPlan
from cad_costing_agent.moulding_operations import MOULD_OPERATION_ID, MOULD_SETUP_ID
from cad_costing_agent.operation_time import (
    TimeAssumptions,
    TimeStatus,
    WeldSpec,
    estimate_operation_time,
)
from cad_costing_agent.stage_costing import TIMED_KEEPS_MATERIAL_AND_TOOL, TIMEABLE_STAGES
from tests.step_fixtures import GOVERNING_WALL_FIXTURES, WALL_FIXTURES, moulded_box_2mm
from tests.test_process_classification import decide

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "cad_costing_agent"
EXACT = 1e-6

ABS = {
    "material": "abs",
    "thermal_diffusivity_mm2_per_s": 0.1,
    "melt_temperature_c": 230.0,
    "mould_temperature_c": 60.0,
    "eject_temperature_c": 90.0,
}
PP = {
    "material": "pp",
    "thermal_diffusivity_mm2_per_s": 0.1,
    "melt_temperature_c": 220.0,
    "mould_temperature_c": 40.0,
    "eject_temperature_c": 90.0,
}
MOULDING = dict(
    moulding_materials=[ABS, PP],
    moulding_fill_seconds=1.5,
    moulding_pack_seconds=3.0,
    moulding_open_eject_seconds=5.0,
    moulding_setup_minutes=60.0,
)
MOULDING_KEYS = tuple(MOULDING)

# Hand-computed: t = s² / (π² α) · ln((4/π)(Tm − Tw)/(Te − Tw)).
# 2 mm ABS: 4 / (9.8696044 × 0.1) = 4.0528473 s; ln(1.2732395 × 170/30) =
# ln(7.2150239) = 1.9761727; product 8.009097 s.
COOLING = {
    (2.0, "abs"): 8.009097225580703,
    (6.0, "abs"): 72.08187503022633,  # 9 × the 2 mm figure: cooling goes with s²
    (4.0, "abs"): 32.03638890232281,
    (2.0, "pp"): 6.170453278003762,  # ln(1.2732395 × 180/50) = 1.5232...
}


def mould(wall=2.0, cavities=4.0, material="abs", **extra):
    dimensions = {"governing_wall_mm": wall}
    if cavities is not None:
        dimensions["cavities"] = cavities
    return estimate_operation_time(
        operation_type="injection_moulding",
        dimensions=dimensions,
        material=material,
        assumptions=TimeAssumptions(**{**MOULDING, **extra}),
    )


# --- 1. the governing wall ----------------------------------------------------------


@pytest.mark.parametrize("name", sorted(GOVERNING_WALL_FIXTURES))
def test_each_fixture_reports_its_hand_worked_governing_wall(name):
    builder, expected = GOVERNING_WALL_FIXTURES[name]
    result = measure_governing_wall(read_step_shape(builder()))
    assert result.measured and result.not_measured_reason is None
    assert result.thickness_mm == pytest.approx(expected, abs=EXACT)
    assert result.limitations and "never thinner" in result.limitations[0]


def test_the_boss_is_found_because_a_span_runs_down_its_axis():
    builder, _ = GOVERNING_WALL_FIXTURES["plate_2mm_with_6mm_boss"]
    result = measure_governing_wall(read_step_shape(builder()))
    # The span from the boss top to the plate's underside, 10 + 2 mm, ball on the axis.
    assert result.span_mm == pytest.approx(12.0, abs=EXACT)
    assert (result.centre.x, result.centre.y, result.centre.z) == pytest.approx((40.0, 30.0, 6.0), abs=EXACT)
    assert result.minimum_wall_mm == pytest.approx(2.0, abs=EXACT)


def test_a_span_along_a_plate_is_not_read_as_its_thickness():
    builder, _ = GOVERNING_WALL_FIXTURES["plate_4mm"]
    result = measure_governing_wall(read_step_shape(builder()))
    assert result.span_mm > 50.0  # a ray across the plate, from a side face
    assert result.thickness_mm == pytest.approx(4.0, abs=EXACT)


@pytest.mark.parametrize("name", sorted(WALL_FIXTURES))
def test_collecting_spans_leaves_the_wall_measurement_unchanged(name):
    builder, _ = WALL_FIXTURES[name]
    shape = read_step_shape(builder())
    spans: list = []
    plain = measure_wall_thickness(shape).model_dump(exclude={"elapsed_ms"})
    collected = measure_wall_thickness(shape, spans=spans).model_dump(exclude={"elapsed_ms"})
    assert plain == collected
    assert len(spans) == sum(face["samples"] for face in plain["faces"])


def test_the_governing_wall_is_deterministic():
    shape = read_step_shape(GOVERNING_WALL_FIXTURES["plate_2mm_with_6mm_boss"][0]())
    first = measure_governing_wall(shape).model_dump(exclude={"elapsed_ms"})
    assert first == measure_governing_wall(shape).model_dump(exclude={"elapsed_ms"})


def test_the_endpoint_measures_exact_parts_and_refuses_a_mesh():
    response = client.post(
        "/cad/governing-wall",
        files={"file": ("box.step", moulded_box_2mm(), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["thickness_mm"] == pytest.approx(2.0, abs=EXACT)
    stl = trimesh.creation.box(extents=(10.0, 10.0, 10.0)).export(file_type="stl")
    response = client.post(
        "/cad/governing-wall", files={"file": ("box.stl", stl, "application/octet-stream")}
    )
    assert response.status_code in (400, 415)


# --- 2. the time model ----------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(COOLING))
def test_cooling_shot_and_per_part_times_are_the_hand_worked_values(key):
    wall, material = key
    estimate = mould(wall=wall, cavities=1.0, material=material)
    assert estimate.time_status is TimeStatus.ESTIMATED
    p = estimate.parameters
    assert p["moulding_cooling_seconds"] == pytest.approx(COOLING[key], abs=EXACT)
    assert p["moulding_shot_seconds"] == pytest.approx(1.5 + 3.0 + COOLING[key] + 5.0, abs=EXACT)
    assert estimate.setup_time_minutes == 60.0
    assert estimate.method == "injection_moulding_time_v1"
    assert any("cooling" in line and "π²" in line for line in estimate.assumptions)


@pytest.mark.parametrize("cavities", [1, 2, 3, 4, 8, 16])
def test_the_cavities_divide_the_shot_exactly(cavities):
    estimate = mould(cavities=float(cavities))
    shot = 1.5 + 3.0 + COOLING[(2.0, "abs")] + 5.0
    assert estimate.parameters["moulding_seconds_per_part"] == pytest.approx(shot / cavities, abs=EXACT)
    assert estimate.cycle_time_minutes == round(shot / cavities / 60.0, 4)
    assert estimate.parameters["cavities"] == float(cavities)


def test_a_moulding_cycle_is_reported_finer_than_a_machining_one():
    # 17.509097 s / 4 = 4.377274 s = 0.072955 min: 0.0730, not 0.07.
    assert mould(cavities=4.0).cycle_time_minutes == 0.073


@pytest.mark.parametrize(
    "cavities,material,missing",
    [(None, "abs", ["cavities"]), (2.5, "abs", ["cavities"]), (0.0, "abs", ["cavities"]),
     (4.0, None, ["material"])],
)
def test_no_cavity_count_and_no_material_is_never_assumed(cavities, material, missing):
    estimate = mould(cavities=cavities, material=material)
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.missing == missing
    assert estimate.cycle_time_minutes is None and estimate.setup_time_minutes is None


def test_a_material_the_profile_does_not_list_is_not_borrowed():
    estimate = mould(material="nylon")
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.missing == ["moulding_materials"]
    assert "nylon" in estimate.reason and "abs, pp" in estimate.reason


def test_the_material_name_is_matched_regardless_of_case():
    assert mould(material="ABS").parameters == mould(material="abs").parameters


@pytest.mark.parametrize(
    "row",
    [
        {**ABS, "eject_temperature_c": 250.0},  # ejected hotter than the melt
        {**ABS, "mould_temperature_c": 95.0},  # mould hotter than the ejection
        {**ABS, "thermal_diffusivity_mm2_per_s": 0.0},
    ],
)
def test_impossible_thermal_rows_are_refused(row):
    with pytest.raises(ValidationError):
        TimeAssumptions(moulding_materials=[row])


def test_a_material_listed_twice_or_an_empty_table_is_refused():
    with pytest.raises(ValidationError, match="more than once"):
        TimeAssumptions(moulding_materials=[ABS, {**ABS, "material": "ABS"}])
    with pytest.raises(ValidationError, match="at least one row"):
        TimeAssumptions(moulding_materials=[])


def test_the_seeded_baseline_times_no_moulding():
    baseline = TimeAssumptions()
    assert all(getattr(baseline, key) is None for key in MOULDING_KEYS)
    estimate = estimate_operation_time(
        operation_type="injection_moulding",
        dimensions={"governing_wall_mm": 2.0, "cavities": 4.0},
        material="abs",
    )
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.missing == list(MOULDING_KEYS)
    assert "no moulding parameters" in estimate.reason
    stored = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    assert all(stored[key] is None for key in MOULDING_KEYS)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(operation_type="drilling", feature_type="hole", feature_subtype="through",
             dimensions={"diameter_mm": 20.0, "depth_mm": 15.0}),
        dict(operation_type="milling", feature_type="pocket", dimensions={"length_mm": 40.0,
             "width_mm": 20.0, "depth_mm": 5.0}),
        dict(operation_type="turning", feature_type="cylinder", dimensions={"diameter_mm": 30.0,
             "length_mm": 50.0}),
        dict(operation_type="welding", weld=WeldSpec(length_mm=600.0, size_mm=8.0)),
        dict(operation_type="bending", dimensions={"bend_count": 3.0}),
    ],
)
def test_moulding_parameters_change_no_other_estimate_serialized_whole(kwargs):
    others = dict(
        welding_mig_travel_speed_mm_per_min=300.0, welding_tig_travel_speed_mm_per_min=100.0,
        welding_mm_per_pass=6.0, welding_arc_on_factor=0.5, welding_setup_minutes=15.0,
        bending_minutes_per_bend=0.25, bending_setup_minutes=20.0,
    )
    plain = estimate_operation_time(**kwargs, assumptions=TimeAssumptions(**others))
    explicit_none = estimate_operation_time(
        **kwargs, assumptions=TimeAssumptions(**others, **{key: None for key in MOULDING_KEYS})
    )
    with_moulding = estimate_operation_time(
        **kwargs, material="abs", assumptions=TimeAssumptions(**others, **MOULDING)
    )
    assert plain.time_status is TimeStatus.ESTIMATED
    assert plain.model_dump_json() == explicit_none.model_dump_json() == with_moulding.model_dump_json()


def test_the_estimate_time_endpoint_times_a_moulding_operation():
    body = {
        "assumptions": MOULDING,
        "operations": [{"operation_id": "M1", "operation_type": "injection_moulding",
                        "material": "abs",
                        "dimensions": {"governing_wall_mm": 2.0, "cavities": 4.0}}],
    }
    response = client.post("/operations/estimate-time", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["estimates"][0]["estimate"]["cycle_time_minutes"] == 0.073


# --- 3. the moulding line -----------------------------------------------------------------

FORM = {"material": "ABS", "source_unit": "mm", "part_id": "P-MOULD", "part_name": "Moulded box",
        "quantity": "1000"}


@lru_cache(maxsize=None)
def box() -> bytes:
    return moulded_box_2mm()


def publish_profile(profile_id="mould-shop-time"):
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    response = client.post(
        "/time-profiles",
        json={"time_profile_id": profile_id, "name": "Mould shop", "version": "1.0.0",
              "description": "test", "is_illustrative": True,
              "assumptions": {**baseline, **MOULDING}},
    )
    assert response.status_code == 201, response.text


STAGE_RATES = {
    "moulding": {"unit_basis": "per_kg", "unit_rate": 400.0, "batch_charge": 300000.0},
    "degating": {"unit_basis": "per_piece", "unit_rate": 0.5},
}


def publish_rates(*, stage_rates=STAGE_RATES):
    base = client.get("/rates/poc-default").json()
    response = client.post(
        "/rates",
        json={"rate_set_id": "mould-shop", "name": "Mould shop", "version": "1.0.0",
              "currency": "INR", "is_illustrative": False,
              "materials": {**base["materials"],
                            "abs": {"name": "ABS", "cost_per_kg": 180.0, "density_kg_m3": 1050.0}},
              "operations": {**base["operations"],
                             "injection_moulding": {"machine_rate_per_hour": 1200.0,
                                                    "labor_rate_per_hour": 300.0}},
              "additional_costs": {"overhead_rate_percent": 0, "scrap_rate_percent": 0,
                                   "tooling_cost_per_batch": 0},
              "stage_rates": stage_rates},
    )
    assert response.status_code == 201, response.text


def governing() -> dict:
    response = client.post(
        "/cad/governing-wall", files={"file": ("box.step", box(), "application/octet-stream")}
    )
    assert response.status_code == 200, response.text
    return response.json()


def line(cavities=4, material="abs", profile="mould-shop-time") -> dict:
    query = f"?time_profile_id={profile}" if profile else ""
    response = client.post(
        f"/operations/moulding-line{query}",
        json={"governing_wall": governing(), "material": material, "cavities": cavities},
    )
    assert response.status_code == 200, response.text
    return response.json()


def plan_of(body: dict, *, accept: bool = True) -> ManufacturingPlan:
    plan = ManufacturingPlan(
        setups=body["setups"], operations=body["operations"], units="mm", source_filename="box.step"
    )
    if accept:
        for operation in plan.operations:
            plan = plan.replacing(operation.accepted(use_estimated_time=True))
        for setup in plan.setups:
            plan = plan.replacing_setup(setup.accepted(use_estimated_time=True))
    return plan


@lru_cache(maxsize=None)
def _classification() -> str:
    suggested = client.post(
        "/cad/operations/suggest", files={"file": ("box.step", box(), "application/octet-stream")}
    ).json()
    response = decide(suggested["process_classification"], "edited", "injection_moulding")
    assert response.status_code == 200, response.text
    return response.text


def cost(plan: ManufacturingPlan | None, stage_inputs=None, **form):
    data = {**FORM, "rate_set_id": "mould-shop",
            "process_classification": _classification(), **form}
    if plan is not None:
        data["manufacturing_plan"] = plan.model_dump_json()
    if stage_inputs is not None:
        data["stage_inputs"] = json.dumps(stage_inputs)
    return client.post(
        "/cost/cad-part", files={"file": ("box.step", box(), "application/octet-stream")}, data=data
    )


def test_the_line_is_one_fixed_id_proposal_nobody_has_accepted():
    publish_profile()
    body = line()
    assert [o["operation_id"] for o in body["operations"]] == [MOULD_OPERATION_ID]
    assert [s["setup_id"] for s in body["setups"]] == [MOULD_SETUP_ID]
    operation = body["operations"][0]
    assert operation["system_suggested_operation"] == "injection_moulding"
    assert operation["system_time_accepted"] is False
    assert operation["dimensions"] == {"governing_wall_mm": 2.0, "cavities": 4.0}
    assert operation["system_suggested_cycle_time"] == 0.073
    assert body["time_profile_id"] == "mould-shop-time"
    # Generating again replaces by id; the browser filters by these same ids.
    assert [o["operation_id"] for o in line(cavities=2)["operations"]] == [MOULD_OPERATION_ID]


def test_without_cavities_the_line_is_proposed_needing_input():
    publish_profile()
    operation = line(cavities=None)["operations"][0]
    assert operation["system_time_status"] == "needs_user_input"
    assert "cavities" in operation["system_time_missing"]


def test_the_seeded_profile_proposes_the_line_but_times_nothing():
    operation = line(profile=None)["operations"][0]
    assert operation["system_time_status"] == "needs_user_input"
    assert operation["system_suggested_cycle_time"] is None


# --- 4. pricing -----------------------------------------------------------------------------

TIMED = {"moulding": {"pricing": "timed", "cavities": 4, "yield_percent": 80.0,
                      "amortisation_quantity": 100000}}
# The box: 60·40·30 − 56·36·28 = 15552 mm³; ABS 1050 kg/m³ → 0.0163296 kg.
FINISHED_KG = 15552e-9 * 1050.0


def test_without_a_pricing_choice_moulding_is_its_stage_rate_as_before():
    publish_profile()
    publish_rates()
    response = cost(plan_of(line()))
    assert response.status_code == 200, response.text
    part_cost = response.json()["part_cost"]
    assert [s["stage"] for s in part_cost["stage_costs"]] == ["moulding", "degating"]
    moulding = part_cost["stage_costs"][0]
    assert moulding["unit_rate"] == 400.0
    # The pre-moulding-time key set: no timed-only key leaks onto a stage-rate line.
    assert "unit_rate_source" not in moulding
    assert part_cost["material_charged"] is False
    assert part_cost["operations"] == []  # the line is outside a route that times nothing


def test_timed_moulding_charges_press_material_and_mould_each_once_to_a_hand_worked_total():
    publish_profile()
    publish_rates()
    response = cost(plan_of(line()), stage_inputs=TIMED)
    assert response.status_code == 200, response.text
    part_cost = response.json()["part_cost"]
    lines = {s["stage"]: s for s in part_cost["stage_costs"]}
    assert list(lines) == ["moulding", "degating"]
    moulding = lines["moulding"]
    # Material: the rate set's ABS price, never the 400/kg stage rate.
    assert moulding["unit_rate"] == 180.0 and moulding["unit_rate_source"] == "material_rate"
    assert moulding["base_mass_kg"] == pytest.approx(FINISHED_KG, rel=1e-9)
    assert moulding["mass_kg"] == pytest.approx(FINISHED_KG / 0.8, rel=1e-9)
    material = 180.0 * FINISHED_KG / 0.8 * 1000  # 3674.16
    assert moulding["run_cost"] == pytest.approx(material, abs=0.01)
    # The mould: 300000 × 1000 / 100000 = 3000, the rest carried by other pieces.
    assert moulding["full_batch_charge"] == 300000.0
    assert moulding["batch_charge"] == pytest.approx(3000.0, abs=0.01)
    # Material is not charged a second time on the material line.
    assert part_cost["material_charged"] is False
    assert part_cost["total_material_cost"] == 0.0
    # The press: 1200/h on 60 min setup + 1000 × 0.073 min, 300/h on the run:
    # 1200 × 133 / 60 + 300 × 73 / 60 = 2660 + 365 = 3025.
    (operation,) = part_cost["operations"]
    assert operation["process_type"] == "injection_moulding"
    assert operation["cycle_time_minutes"] == 0.073
    assert operation["total_operation_cost"] == pytest.approx(3025.0, abs=0.01)
    degating = 0.5 * 1000
    expected = material + 3000.0 + degating + 3025.0
    assert part_cost["total_stage_cost"] + part_cost["total_operation_cost"] == pytest.approx(expected, abs=0.02)
    assert part_cost["total_part_cost"] == pytest.approx(expected, abs=0.02)


def test_timed_moulding_without_a_published_mould_charges_no_mould_and_says_so():
    publish_profile()
    publish_rates(stage_rates={"degating": STAGE_RATES["degating"]})
    response = cost(plan_of(line()), stage_inputs={"moulding": {"pricing": "timed", "cavities": 4}})
    assert response.status_code == 200, response.text
    moulding = response.json()["part_cost"]["stage_costs"][0]
    assert moulding["batch_charge"] == 0.0
    assert "no mould is charged" in moulding["rate_note"]


def test_timed_moulding_with_no_accepted_time_is_refused_not_zero():
    publish_profile()
    publish_rates()
    before = client.get("/estimates").json()["total"]
    response = cost(plan_of(line(), accept=False), stage_inputs=TIMED)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "timed_stage_not_costed"
    assert detail["stages"] == ["moulding"]
    assert "moulding line" in detail["message"]
    assert client.get("/estimates").json()["total"] == before


def test_a_line_timed_for_other_cavities_is_refused():
    publish_profile()
    publish_rates()
    response = cost(plan_of(line(cavities=2)), stage_inputs=TIMED)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "moulding_cavities_disagree"
    no_count = {"moulding": {k: v for k, v in TIMED["moulding"].items() if k != "cavities"}}
    response = cost(plan_of(line()), stage_inputs=no_count)
    assert response.json()["detail"]["error"] == "moulding_cavities_disagree"


def test_a_typed_moulding_time_is_the_engineers_whole_figure():
    publish_profile()
    publish_rates()
    plan = plan_of(line(cavities=2), accept=False)
    operation = plan.operations[0].with_times(cycle_time_minutes=0.05, setup_time_minutes=60.0)
    plan = plan.replacing(operation)
    for setup in plan.setups:
        plan = plan.replacing_setup(setup.accepted(use_estimated_time=True))
    response = cost(plan, stage_inputs=TIMED)
    assert response.status_code == 200, response.text
    assert response.json()["part_cost"]["operations"][0]["cycle_time_minutes"] == 0.05


def test_a_line_timed_for_another_material_is_refused():
    publish_profile()
    publish_rates()
    response = cost(plan_of(line(material="pp")), stage_inputs=TIMED)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "moulding_time_for_another_material"


def test_cavities_on_a_stage_rate_moulding_route_are_refused_not_ignored():
    publish_profile()
    publish_rates()
    response = cost(plan_of(line()), stage_inputs={"moulding": {"cavities": 4}})
    assert response.status_code == 400
    assert "priced by its stage rate" in response.json()["detail"]


@pytest.mark.parametrize("value", [0, 2.5, "four", True])
def test_a_cavity_count_must_be_a_whole_number_of_at_least_one(value):
    publish_profile()
    publish_rates()
    response = cost(plan_of(line()), stage_inputs={"moulding": {"pricing": "timed", "cavities": value}})
    assert response.status_code == 400
    assert "whole number" in response.json()["detail"]


def test_the_tool_life_is_still_refused_below_the_quantity_on_a_timed_line():
    publish_profile()
    publish_rates()
    short = {"moulding": {**TIMED["moulding"], "amortisation_quantity": 10}}
    response = cost(plan_of(line()), stage_inputs=short)
    assert response.status_code == 400
    assert "a second tool" in response.json()["detail"]


def test_a_moulding_entry_changes_nothing_on_a_route_without_moulding():
    """Stage inputs are shared across compared routes; one for moulding is ignored elsewhere."""
    publish_rates()
    suggested = client.post(
        "/cad/operations/suggest", files={"file": ("box.step", box(), "application/octet-stream")}
    ).json()
    milled = decide(suggested["process_classification"], "edited", "cnc_milling").text
    plain = cost(None, process_classification=milled, material="Steel")
    entered = cost(None, process_classification=milled, material="Steel", stage_inputs=TIMED)
    assert plain.status_code == entered.status_code == 200
    drop = lambda body: {k: v for k, v in body.items() if k != "estimate_id"}  # noqa: E731
    first, second = plain.json(), entered.json()
    first["part_cost"].pop("estimate_id", None)
    second["part_cost"].pop("estimate_id", None)
    assert drop(first) == drop(second)


# --- 5. boundaries, script and mirrors --------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            named.add(node.module)
        elif isinstance(node, ast.Import):
            named.update(alias.name for alias in node.names)
    return named


def test_the_line_builder_imports_no_costing_and_no_measurement():
    named = {m for m in _imports(PACKAGE / "moulding_operations.py") if m.startswith("cad_costing_agent")}
    assert named == {
        "cad_costing_agent.features.model",
        "cad_costing_agent.manufacturing_plan",
        "cad_costing_agent.operation_suggestions",
        "cad_costing_agent.operation_time",
    }


def test_only_main_reads_the_moulding_line_builder():
    readers = [
        path.name
        for path in PACKAGE.rglob("*.py")
        if any(m.endswith("moulding_operations") for m in _imports(path))
    ]
    assert readers == ["main.py"]


def test_only_moulding_keeps_a_stage_line_when_timed_and_the_catalog_says_so():
    assert TIMED_KEEPS_MATERIAL_AND_TOOL == {"moulding"}
    assert TIMEABLE_STAGES["moulding"] == "injection_moulding"
    catalog = client.get("/process-routes").json()
    assert catalog["timed_material_stages"] == ["moulding"]


def _js_code(name: str) -> str:
    """The script with its comments removed, so prose cannot satisfy a pattern."""
    source = (PACKAGE / "static" / name).read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def test_the_browser_adds_the_line_from_the_server_and_works_nothing_out():
    code = _js_code("process.js")
    assert "/cad/governing-wall" in code and "/operations/moulding-line" in code
    block = code.split("async function addMouldLine()", 1)[1].split("document.addEventListener", 1)[0]
    # No time, cost or division is computed in the browser.
    assert not re.search(r"[^/]/\s*\d|\*\s*\d|Math\.", block)
    work = _js_code("plan.js").split("const TIMED_STAGE_WORK = ", 1)[1].split("\n};", 1)[0]
    assert "add-moulding-line" in work


def test_the_time_setup_round_trips_the_moulding_table():
    code = _js_code("timesetup.js")
    assert "moulding_materials" in code and "t-mould-table" in code
    for key in ("thermal_diffusivity_mm2_per_s", "melt_temperature_c", "mould_temperature_c",
                "eject_temperature_c"):
        assert key in code


def _publish_script():
    path = ROOT / "scripts" / "publish_illustrative_moulding_time_profile.py"
    spec = importlib.util.spec_from_file_location("publish_illustrative_moulding", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_illustrative_moulding_profile_is_the_baseline_plus_labelled_moulding_figures():
    script = _publish_script()
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    profile = script.build_profile(baseline, "1.0.0")
    assert profile["is_illustrative"] is True
    assert "ESTIMATED" in profile["description"] and "no published source" in profile["description"]
    differing = {k for k in profile["assumptions"] if profile["assumptions"][k] != baseline.get(k)}
    assert differing == set(MOULDING_KEYS)
    assumptions = TimeAssumptions(**profile["assumptions"])
    assert all("ESTIMATED" in row.note for row in assumptions.moulding_materials)
    lines = (ROOT / "scripts" / "publish_illustrative_moulding_time_profile.py").read_text().splitlines()
    for key in MOULDING_KEYS[1:]:
        index = next(i for i, text in enumerate(lines) if f'"{key}":' in text)
        assert "ESTIMATED" in lines[index - 1]
    with pytest.raises(ValueError, match="already carries moulding parameters"):
        script.build_profile({"moulding_fill_seconds": 1.0})


def test_the_hand_worked_cooling_constant_is_the_formula():
    # The constants above, recomputed once from the formula written out literally.
    s, alpha, melt, mould_t, eject = 2.0, 0.1, 230.0, 60.0, 90.0
    value = s * s / (math.pi * math.pi * alpha) * math.log(4.0 / math.pi * (melt - mould_t) / (eject - mould_t))
    assert value == pytest.approx(COOLING[(2.0, "abs")], abs=1e-12)
