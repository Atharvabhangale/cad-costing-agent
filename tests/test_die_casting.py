"""High-pressure die casting timed by Chvorinov's rule, priced by time, and proposed cores.

What is checked:

1. The casting modulus V/A of exact fixtures (a 40 mm cube, a 100 x 100 x 5 plate),
   measured through the one geometry path, and refused for a mesh.
2. The solidification, shot and per-part times against hand-computed values,
   cavities dividing the shot exactly, confirmed cores adding their time, and every
   refusal (no die-casting parameters, no cavities, no alloy row, cores confirmed
   with no per-core time). All parameters absent by default, so the seeded profile
   times nothing and every other estimate — moulding included — is what it was.
3. The die-casting line: one fixed-id operation, proposed and never accepted.
4. Pricing: the machine charged once by the line's time, the alloy once on the
   stage line at the rate set's material price with the engineer's yield, the die's
   one-off charge once (amortised) — a hand-worked total; guards for an uncommitted
   time, other cavities, other cores, another alloy and another part.
5. Proposed cores: undercut faces grouped by shared edges (one region for a side
   hole, two for two separate holes, one for a closed T-slot), refused for a mesh,
   charging nothing until a count is confirmed.
6. Route catalog, boundaries, the published script and the browser's mirrors.
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
from cad_costing_agent.features import draw_direction
from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import ManufacturingPlan
from cad_costing_agent.moulding_operations import DIE_CAST_OPERATION_ID, DIE_CAST_SETUP_ID
from cad_costing_agent.operation_time import (
    TimeAssumptions,
    TimeStatus,
    WeldSpec,
    chvorinov_seconds,
    estimate_operation_time,
)
from cad_costing_agent.process_classification import (
    NOT_ASSESSED,
    ROUTE_CATALOG,
    ManufacturingFamily,
)
from cad_costing_agent.stage_costing import (
    _SHOT_WORDS,
    MATERIAL_SUPPLYING_STAGES,
    TIMED_KEEPS_MATERIAL_AND_TOOL,
    TIMEABLE_STAGES,
)
from tests import draw_fixtures
from tests.step_fixtures import PROCESS_FIXTURES, _box, _cut, _cylinder, to_step
from tests.test_process_classification import decide

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "cad_costing_agent"
EXACT = 1e-6

ALUMINIUM = {"material": "aluminium", "mould_constant": 0.5, "exponent": 2.0}
ZINC = {"material": "zinc", "mould_constant": 0.8, "exponent": 2.0}
DIE_CASTING = dict(
    die_casting_alloys=[ALUMINIUM, ZINC],
    die_casting_fill_seconds=0.1,
    die_casting_open_eject_seconds=8.0,
    die_casting_spray_seconds=4.0,
    die_casting_setup_minutes=90.0,
)
REQUIRED_KEYS = tuple(DIE_CASTING)
CORE = dict(die_casting_core_seconds_per_core=2.0)

# Hand-worked. Cube 40: V = 64000, A = 6 x 1600 = 9600, V/A = 6.666667 mm;
# t = 0.5 x 6.666667² = 22.222222 s; shot = 0.1 + 22.222222 + 8 + 4 = 34.322222 s.
CUBE_V, CUBE_A = 64000.0, 9600.0
CUBE_SOLID = 0.5 * (40.0 / 6.0) ** 2
CUBE_SHOT = 0.1 + CUBE_SOLID + 8.0 + 4.0
# Plate 100 x 100 x 5: V = 50000, A = 2 x 10000 + 4 x 500 = 22000, V/A = 2.272727 mm;
# t = 0.5 x 5.165289 = 2.582645 s.
PLATE_V, PLATE_A = 50000.0, 22000.0
PLATE_SOLID = 0.5 * (50000.0 / 22000.0) ** 2


@lru_cache(maxsize=None)
def cube() -> bytes:
    return to_step(_box(40.0, 40.0, 40.0))


@lru_cache(maxsize=None)
def plate() -> bytes:
    return to_step(_box(100.0, 100.0, 5.0))


@lru_cache(maxsize=None)
def two_side_holes() -> bytes:
    """60 x 40 x 20 block with two separate Ø8 holes along X at y = 10 and y = 30.

    Against Z each bore is an undercut, and they share no edge: two regions.
    """
    block = _cut(_box(60.0, 40.0, 20.0), _cylinder(4.0, 80.0, (-10.0, 10.0, 10.0), (1.0, 0.0, 0.0)))
    return to_step(_cut(block, _cylinder(4.0, 80.0, (-10.0, 30.0, 10.0), (1.0, 0.0, 0.0))))


def cast(volume=CUBE_V, area=CUBE_A, cavities=2.0, material="aluminium", cores=None, **extra):
    dimensions = {"casting_volume_mm3": volume, "casting_surface_area_mm2": area}
    if cavities is not None:
        dimensions["cavities"] = cavities
    if cores is not None:
        dimensions["cores"] = cores
    return estimate_operation_time(
        operation_type="die_casting",
        dimensions=dimensions,
        material=material,
        assumptions=TimeAssumptions(**{**DIE_CASTING, **extra}),
    )


def _post(path: str, data: bytes, name: str = "part.step", **form):
    return client.post(path, files={"file": (name, data, "application/octet-stream")}, data=form)


# --- 1. the casting modulus ------------------------------------------------------------


@pytest.mark.parametrize(
    "builder,volume,area", [(cube, CUBE_V, CUBE_A), (plate, PLATE_V, PLATE_A)]
)
def test_the_casting_modulus_is_the_exact_volume_over_the_exact_area(builder, volume, area):
    response = _post("/cad/casting-modulus", builder())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["measured"] is True and body["geometry_source"] == "brep"
    assert body["volume_mm3"] == pytest.approx(volume, rel=1e-12)
    assert body["surface_area_mm2"] == pytest.approx(area, rel=1e-12)
    assert body["modulus_mm"] == pytest.approx(volume / area, rel=1e-12)
    assert body["limitations"]


def test_the_casting_modulus_is_the_costing_geometry_not_a_second_measurement():
    source = (PACKAGE / "main.py").read_text(encoding="utf-8")
    section = source.split("# ------------------------------------------------------ high-pressure die casting", 1)[1]
    code = ast.unparse(ast.parse(section))
    assert "_geometry_of(_import(" in code
    for forbidden in ("BRepGProp", "measure_brep_shape", "read_step_shape", "trimesh"):
        assert forbidden not in code, forbidden


def test_a_mesh_has_no_casting_modulus():
    stl = trimesh.creation.box(extents=(10.0, 10.0, 10.0)).export(file_type="stl")
    response = _post("/cad/casting-modulus", stl, name="box.stl")
    assert response.status_code == 400
    assert "exact B-Rep" in response.json()["detail"]


# --- 2. the time model -----------------------------------------------------------------


def test_chvorinov_is_the_formula_written_out():
    assert chvorinov_seconds(CUBE_V, CUBE_A, 0.5, 2.0) == pytest.approx(22.222222222222, abs=1e-9)
    assert chvorinov_seconds(PLATE_V, PLATE_A, 0.5, 2.0) == pytest.approx(2.582644628099, abs=1e-9)
    # n is the row's: 1.5 gives B x (V/A)^1.5.
    assert chvorinov_seconds(CUBE_V, CUBE_A, 0.5, 1.5) == pytest.approx(0.5 * (20.0 / 3.0) ** 1.5, abs=1e-12)


@pytest.mark.parametrize(
    "volume,area,solid", [(CUBE_V, CUBE_A, CUBE_SOLID), (PLATE_V, PLATE_A, PLATE_SOLID)]
)
def test_solidification_shot_and_per_part_are_the_hand_worked_values(volume, area, solid):
    estimate = cast(volume=volume, area=area, cavities=1.0)
    assert estimate.time_status is TimeStatus.ESTIMATED
    p = estimate.parameters
    assert p["die_casting_solidification_seconds"] == pytest.approx(solid, abs=EXACT)
    assert p["die_casting_shot_seconds"] == pytest.approx(0.1 + solid + 8.0 + 4.0, abs=EXACT)
    assert p["casting_modulus_mm"] == pytest.approx(volume / area, abs=EXACT)
    assert estimate.setup_time_minutes == 90.0
    assert estimate.method == "die_casting_time_v1"
    assert "cores" not in p


@pytest.mark.parametrize("cavities", [1, 2, 3, 4, 8])
def test_the_cavities_divide_the_shot_exactly(cavities):
    estimate = cast(cavities=float(cavities))
    assert estimate.parameters["die_casting_seconds_per_part"] == pytest.approx(CUBE_SHOT / cavities, abs=EXACT)
    assert estimate.cycle_time_minutes == round(CUBE_SHOT / cavities / 60.0, 4)


def test_the_hand_worked_cube_cycle():
    # 34.322222 s / 2 = 17.161111 s = 0.286019 min, reported to 0.0001 min.
    assert cast().cycle_time_minutes == 0.286


def test_a_confirmed_core_adds_its_time_to_every_shot():
    estimate = cast(cores=2.0, **CORE)
    assert estimate.parameters["die_casting_shot_seconds"] == pytest.approx(CUBE_SHOT + 4.0, abs=EXACT)
    assert estimate.parameters["cores"] == 2.0
    assert estimate.cycle_time_minutes == round((CUBE_SHOT + 4.0) / 2 / 60.0, 4)
    assert any("confirmed by the engineer" in line for line in estimate.evidence)


def test_cores_without_a_per_core_time_are_never_timed_as_free():
    estimate = cast(cores=1.0)
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.missing == ["die_casting_core_seconds_per_core"]
    assert cast(cores=1.5, **CORE).missing == ["cores"]


@pytest.mark.parametrize(
    "cavities,material,missing",
    [(None, "aluminium", ["cavities"]), (2.5, "aluminium", ["cavities"]), (0.0, "aluminium", ["cavities"]),
     (2.0, None, ["material"])],
)
def test_no_cavity_count_and_no_alloy_is_never_assumed(cavities, material, missing):
    estimate = cast(cavities=cavities, material=material)
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.missing == missing
    assert estimate.cycle_time_minutes is None and estimate.setup_time_minutes is None


def test_an_alloy_the_profile_does_not_list_is_not_borrowed():
    estimate = cast(material="magnesium")
    assert estimate.missing == ["die_casting_alloys"]
    assert "magnesium" in estimate.reason and "aluminium, zinc" in estimate.reason
    assert cast(material="Aluminium").parameters == cast(material="aluminium").parameters


def test_impossible_rows_and_repeated_alloys_are_refused():
    for row in ({**ALUMINIUM, "mould_constant": 0.0}, {**ALUMINIUM, "exponent": 0.0}):
        with pytest.raises(ValidationError):
            TimeAssumptions(die_casting_alloys=[row])
    with pytest.raises(ValidationError, match="more than once"):
        TimeAssumptions(die_casting_alloys=[ALUMINIUM, {**ALUMINIUM, "material": "Aluminium"}])
    with pytest.raises(ValidationError, match="at least one row"):
        TimeAssumptions(die_casting_alloys=[])


def test_the_seeded_baseline_times_no_die_casting():
    baseline = TimeAssumptions()
    keys = (*REQUIRED_KEYS, "die_casting_core_seconds_per_core", "moulding_core_seconds_per_core")
    assert all(getattr(baseline, key) is None for key in keys)
    estimate = estimate_operation_time(
        operation_type="die_casting",
        dimensions={"casting_volume_mm3": CUBE_V, "casting_surface_area_mm2": CUBE_A, "cavities": 2.0},
        material="aluminium",
    )
    assert estimate.missing == list(REQUIRED_KEYS)
    stored = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    assert all(stored[key] is None for key in keys)


MOULDING = dict(
    moulding_materials=[{"material": "abs", "thermal_diffusivity_mm2_per_s": 0.1,
                         "melt_temperature_c": 230.0, "mould_temperature_c": 60.0,
                         "eject_temperature_c": 90.0}],
    moulding_fill_seconds=1.5, moulding_pack_seconds=3.0, moulding_open_eject_seconds=5.0,
    moulding_setup_minutes=60.0,
)


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
        dict(operation_type="injection_moulding", material="abs",
             dimensions={"governing_wall_mm": 2.0, "cavities": 4.0}),
    ],
)
def test_die_casting_and_core_parameters_change_no_other_estimate_serialized_whole(kwargs):
    others = dict(
        welding_mig_travel_speed_mm_per_min=300.0, welding_tig_travel_speed_mm_per_min=100.0,
        welding_mm_per_pass=6.0, welding_arc_on_factor=0.5, welding_setup_minutes=15.0, **MOULDING,
    )
    plain = estimate_operation_time(**kwargs, assumptions=TimeAssumptions(**others))
    with_die_casting = estimate_operation_time(
        **kwargs,
        assumptions=TimeAssumptions(**others, **DIE_CASTING, **CORE, moulding_core_seconds_per_core=3.0),
    )
    assert plain.time_status is TimeStatus.ESTIMATED
    assert plain.model_dump_json() == with_die_casting.model_dump_json()


def test_a_confirmed_core_adds_to_a_moulding_shot_too_and_only_then():
    assumptions = TimeAssumptions(**MOULDING, moulding_core_seconds_per_core=3.0)
    base = {"governing_wall_mm": 2.0, "cavities": 4.0}
    plain = estimate_operation_time(operation_type="injection_moulding", dimensions=base,
                                    material="abs", assumptions=assumptions)
    cored = estimate_operation_time(operation_type="injection_moulding",
                                    dimensions={**base, "cores": 2.0}, material="abs",
                                    assumptions=assumptions)
    shot = plain.parameters["moulding_shot_seconds"]
    assert cored.parameters["moulding_shot_seconds"] == pytest.approx(shot + 6.0, abs=EXACT)
    assert "cores" not in plain.parameters and "moulding_core_seconds_per_core" not in plain.parameters


# --- 3. the die-casting line -----------------------------------------------------------

FORM = {"material": "Aluminium", "source_unit": "mm", "part_id": "P-DC", "part_name": "Die-cast cube",
        "quantity": "1000"}


def publish_profile(profile_id="die-shop-time", extra=None):
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    response = client.post(
        "/time-profiles",
        json={"time_profile_id": profile_id, "name": "Die shop", "version": "1.0.0",
              "description": "test", "is_illustrative": True,
              "assumptions": {**baseline, **DIE_CASTING, **CORE, **(extra or {})}},
    )
    assert response.status_code == 201, response.text


STAGE_RATES = {
    "die_casting": {"unit_basis": "per_kg", "unit_rate": 500.0, "batch_charge": 400000.0},
    "degating": {"unit_basis": "per_piece", "unit_rate": 1.0},
}


def publish_rates(*, stage_rates=STAGE_RATES):
    base = client.get("/rates/poc-default").json()
    response = client.post(
        "/rates",
        json={"rate_set_id": "die-shop", "name": "Die shop", "version": "1.0.0",
              "currency": "INR", "is_illustrative": False,
              "materials": {**base["materials"],
                            "aluminium": {"name": "Aluminium", "cost_per_kg": 250.0,
                                          "density_kg_m3": 2700.0}},
              "operations": {**base["operations"],
                             "die_casting": {"machine_rate_per_hour": 1800.0,
                                             "labor_rate_per_hour": 300.0}},
              "additional_costs": {"overhead_rate_percent": 0, "scrap_rate_percent": 0,
                                   "tooling_cost_per_batch": 0},
              "stage_rates": stage_rates},
    )
    assert response.status_code == 201, response.text


def modulus(data=None) -> dict:
    response = _post("/cad/casting-modulus", data or cube())
    assert response.status_code == 200, response.text
    return response.json()


def line(cavities=2, material="aluminium", profile="die-shop-time", cores=None, data=None) -> dict:
    query = f"?time_profile_id={profile}" if profile else ""
    response = client.post(
        f"/operations/die-casting-line{query}",
        json={"casting_modulus": modulus(data), "material": material, "cavities": cavities,
              "cores": cores},
    )
    assert response.status_code == 200, response.text
    return response.json()


def plan_of(body: dict, *, accept: bool = True) -> ManufacturingPlan:
    plan = ManufacturingPlan(
        setups=body["setups"], operations=body["operations"], units="mm", source_filename="cube.step"
    )
    if accept:
        for operation in plan.operations:
            plan = plan.replacing(operation.accepted(use_estimated_time=True))
        for setup in plan.setups:
            plan = plan.replacing_setup(setup.accepted(use_estimated_time=True))
    return plan


@lru_cache(maxsize=None)
def _classification(data: bytes, route_id: str = "die_casting") -> str:
    suggested = _post("/cad/operations/suggest", data).json()
    response = decide(suggested["process_classification"], "edited", route_id)
    assert response.status_code == 200, response.text
    return response.text


def cost(plan, stage_inputs=None, data=None, **form):
    data = data or cube()
    body = {**FORM, "rate_set_id": "die-shop", "process_classification": _classification(data), **form}
    if plan is not None:
        body["manufacturing_plan"] = plan.model_dump_json()
    if stage_inputs is not None:
        body["stage_inputs"] = json.dumps(stage_inputs)
    return client.post(
        "/cost/cad-part", files={"file": ("cube.step", data, "application/octet-stream")}, data=body
    )


def test_the_line_is_one_fixed_id_proposal_nobody_has_accepted():
    publish_profile()
    body = line()
    assert [o["operation_id"] for o in body["operations"]] == [DIE_CAST_OPERATION_ID]
    assert [s["setup_id"] for s in body["setups"]] == [DIE_CAST_SETUP_ID]
    operation = body["operations"][0]
    assert operation["system_suggested_operation"] == "die_casting"
    assert operation["system_time_accepted"] is False
    assert operation["dimensions"] == {"casting_volume_mm3": pytest.approx(CUBE_V, rel=1e-12),
                                       "casting_surface_area_mm2": pytest.approx(CUBE_A, rel=1e-12),
                                       "cavities": 2.0}
    assert operation["system_suggested_cycle_time"] == 0.286
    assert [o["operation_id"] for o in line(cavities=4)["operations"]] == [DIE_CAST_OPERATION_ID]


def test_the_seeded_profile_and_a_missing_cavity_count_time_nothing():
    publish_profile()
    assert line(cavities=None)["operations"][0]["system_time_status"] == "needs_user_input"
    assert line(profile=None)["operations"][0]["system_suggested_cycle_time"] is None


# --- 4. pricing ------------------------------------------------------------------------

TIMED = {"die_casting": {"pricing": "timed", "cavities": 2, "yield_percent": 80.0,
                         "amortisation_quantity": 100000}}
CUBE_KG = 64000e-9 * 2700.0  # 0.1728 kg


def test_without_a_pricing_choice_die_casting_is_its_stage_rate():
    publish_profile()
    publish_rates()
    part_cost = cost(plan_of(line())).json()["part_cost"]
    assert [s["stage"] for s in part_cost["stage_costs"]] == ["die_casting", "degating"]
    stage = part_cost["stage_costs"][0]
    assert stage["unit_rate"] == 500.0 and "unit_rate_source" not in stage
    assert part_cost["material_charged"] is False
    assert part_cost["operations"] == []


def test_timed_die_casting_charges_machine_alloy_and_die_each_once_to_a_hand_worked_total():
    publish_profile()
    publish_rates()
    response = cost(plan_of(line()), stage_inputs=TIMED)
    assert response.status_code == 200, response.text
    part_cost = response.json()["part_cost"]
    lines = {s["stage"]: s for s in part_cost["stage_costs"]}
    assert list(lines) == ["die_casting", "degating"]
    stage = lines["die_casting"]
    # The alloy at the rate set's aluminium price, never the 500/kg stage rate.
    assert stage["unit_rate"] == 250.0 and stage["unit_rate_source"] == "material_rate"
    assert stage["mass_kg"] == pytest.approx(CUBE_KG / 0.8, rel=1e-9)
    alloy = 250.0 * CUBE_KG / 0.8 * 1000  # 54000.00
    assert stage["run_cost"] == pytest.approx(alloy, abs=0.01)
    assert "biscuit, runners and overflows" in stage["mass_note"]
    assert "die-casting machine" in stage["rate_note"] and "'die_casting' rate" in stage["rate_note"]
    # The die: 400000 x 1000 / 100000 = 4000.
    assert stage["batch_charge"] == pytest.approx(4000.0, abs=0.01)
    assert part_cost["material_charged"] is False and part_cost["total_material_cost"] == 0.0
    # The machine: 1800/h on 90 min setup + 1000 x 0.286 min, 300/h on the run:
    # 1800 x 376 / 60 + 300 x 286 / 60 = 11280 + 1430 = 12710.
    (operation,) = part_cost["operations"]
    assert operation["process_type"] == "die_casting" and operation["cycle_time_minutes"] == 0.286
    assert operation["total_operation_cost"] == pytest.approx(12710.0, abs=0.01)
    expected = alloy + 4000.0 + 1000.0 + 12710.0  # 71710
    assert part_cost["total_part_cost"] == pytest.approx(expected, abs=0.02)


def test_timed_die_casting_without_a_published_die_charges_no_die_and_says_so():
    publish_profile()
    publish_rates(stage_rates={"degating": STAGE_RATES["degating"]})
    response = cost(plan_of(line()), stage_inputs={"die_casting": {"pricing": "timed", "cavities": 2}})
    assert response.status_code == 200, response.text
    stage = response.json()["part_cost"]["stage_costs"][0]
    assert stage["batch_charge"] == 0.0 and "no die is charged" in stage["rate_note"]


def test_the_guards_refuse_and_store_nothing():
    publish_profile()
    publish_rates()
    before = client.get("/estimates").json()["total"]
    unaccepted = cost(plan_of(line(), accept=False), stage_inputs=TIMED)
    assert unaccepted.status_code == 422
    assert unaccepted.json()["detail"]["error"] == "timed_stage_not_costed"
    assert "die-casting line" in unaccepted.json()["detail"]["message"]
    other_cavities = cost(plan_of(line(cavities=4)), stage_inputs=TIMED)
    assert other_cavities.json()["detail"]["error"] == "die_casting_cavities_disagree"
    other_alloy = cost(plan_of(line(material="zinc")), stage_inputs=TIMED)
    assert other_alloy.json()["detail"]["error"] == "die_casting_time_for_another_material"
    other_part = cost(plan_of(line(data=plate())), stage_inputs=TIMED)
    assert other_part.json()["detail"]["error"] == "die_casting_time_for_another_part"
    on_stage_rate = cost(plan_of(line()), stage_inputs={"die_casting": {"cavities": 2}})
    assert on_stage_rate.status_code == 400 and "priced by its stage rate" in on_stage_rate.json()["detail"]
    assert client.get("/estimates").json()["total"] == before


def test_a_typed_die_casting_time_is_the_engineers_whole_figure():
    publish_profile()
    publish_rates()
    plan = plan_of(line(cavities=4), accept=False)
    plan = plan.replacing(plan.operations[0].with_times(cycle_time_minutes=0.2, setup_time_minutes=90.0))
    for setup in plan.setups:
        plan = plan.replacing_setup(setup.accepted(use_estimated_time=True))
    response = cost(plan, stage_inputs=TIMED)
    assert response.status_code == 200, response.text
    assert response.json()["part_cost"]["operations"][0]["cycle_time_minutes"] == 0.2


# --- 5. proposed cores ------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,axis,regions",
    [
        (lambda: draw_fixtures.block_with_side_hole(), "[0, 0, 1]", 1),
        (two_side_holes, "[0, 0, 1]", 2),
        (lambda: draw_fixtures.closed_t_slot(), None, 1),
        (lambda: draw_fixtures.plate(), None, 0),
        (lambda: draw_fixtures.block_with_side_hole(), None, 0),  # ±X is recommended: nothing undercut
    ],
)
def test_undercut_faces_sharing_edges_are_one_proposed_core(data, axis, regions):
    form = {"pull_axis": axis} if axis else {}
    response = _post("/cad/cores/propose", data(), **form)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proposed_core_count"] == regions == len(body["regions"])
    measured = _post("/cad/draw-direction", data(), **form).json()
    grouped = sorted(face for region in body["regions"] for face in region["face_ids"])
    assert grouped == sorted(measured["undercut_face_ids"])
    assert body["pull_axis_label"] == measured["pull_axis_label"]
    assert any("A proposal" in line for line in body["limitations"])


def test_two_separate_holes_are_two_regions_of_one_bore_each():
    body = _post("/cad/cores/propose", two_side_holes(), pull_axis="[0, 0, 1]").json()
    # Each bore: 2 x pi x 4 x 60 mm².
    assert [len(region["face_ids"]) for region in body["regions"]] == [1, 1]
    for region in body["regions"]:
        assert region["area_mm2"] == pytest.approx(480.0 * math.pi, abs=1e-5)
        assert region["marker"] is not None


def test_connected_components_are_grouped_by_shared_edges_only():
    neighbours = {1: {2}, 2: {1, 3}, 3: {2}, 7: set(), 9: {10}, 10: {9}}
    assert draw_direction._connected([10, 3, 1, 7, 2, 9], neighbours) == [[1, 2, 3], [7], [9, 10]]


def test_a_mesh_gets_no_core_proposal_with_the_reason():
    stl = trimesh.creation.box(extents=(10.0, 10.0, 10.0)).export(file_type="stl")
    assert _post("/cad/cores/propose", stl, name="box.stl").status_code in (400, 415)


def test_a_proposed_core_is_charged_only_once_confirmed():
    publish_profile()
    publish_rates()
    hole = draw_fixtures.block_with_side_hole()
    proposed = _post("/cad/cores/propose", hole, pull_axis="[0, 0, 1]").json()["proposed_core_count"]
    assert proposed == 1
    base = {"pricing": "timed", "cavities": 2}
    # Unconfirmed: the line is timed for no core and the estimate charges none.
    plain = cost(plan_of(line(data=hole)), stage_inputs={"die_casting": base}, data=hole)
    assert plain.status_code == 200, plain.text
    # A line timed with the proposed core but no confirmed count is refused.
    unconfirmed = cost(plan_of(line(data=hole, cores=1)), stage_inputs={"die_casting": base}, data=hole)
    assert unconfirmed.status_code == 422
    assert unconfirmed.json()["detail"]["error"] == "die_casting_cores_disagree"
    # Confirmed, the shot carries 2 s more: 2 s / 2 cavities = 1 s per part.
    confirmed = cost(plan_of(line(data=hole, cores=1)), stage_inputs={"die_casting": {**base, "cores": 1}},
                     data=hole)
    assert confirmed.status_code == 200, confirmed.text
    before = plain.json()["part_cost"]["operations"][0]["cycle_time_minutes"]
    after = confirmed.json()["part_cost"]["operations"][0]["cycle_time_minutes"]
    assert after == pytest.approx(before + 1.0 / 60.0, abs=1e-4)
    # A confirmed count the line was not timed for is refused the other way too.
    stale = cost(plan_of(line(data=hole)), stage_inputs={"die_casting": {**base, "cores": 1}}, data=hole)
    assert stale.json()["detail"]["error"] == "die_casting_cores_disagree"


def test_cores_on_a_stage_rate_or_coreless_stage_are_refused_not_ignored():
    publish_profile()
    publish_rates()
    response = cost(plan_of(line()), stage_inputs={"die_casting": {"cores": 2}})
    assert response.status_code == 400 and "no per-core charge" in response.json()["detail"]
    response = cost(plan_of(line()), stage_inputs={"degating": {"cores": 1}})
    assert response.status_code == 400 and "no mould or die" in response.json()["detail"]
    response = cost(plan_of(line()), stage_inputs={"die_casting": {**TIMED["die_casting"], "cores": -1}})
    assert response.status_code == 400 and "whole number" in response.json()["detail"]


def test_die_casting_entries_change_nothing_on_a_route_without_die_casting():
    publish_rates()
    milled = _classification(cube(), "cnc_milling")
    entries = {"die_casting": {**TIMED["die_casting"], "cores": 3}}
    plain = cost(None, process_classification=milled, material="Steel")
    entered = cost(None, process_classification=milled, material="Steel", stage_inputs=entries)
    assert plain.status_code == entered.status_code == 200
    first, second = plain.json(), entered.json()
    for body in (first, second):
        body["part_cost"].pop("estimate_id", None)
        body.pop("estimate_id", None)
    assert first == second


# --- 6. catalog, boundaries, script, mirrors --------------------------------------------


def test_die_casting_is_a_chosen_family_never_recommended():
    routes = {r.route_id: [s.stage for s in r.stages] for r in ROUTE_CATALOG.values()
              if r.family is ManufacturingFamily.DIE_CASTING}
    assert routes == {"die_casting": ["die_casting", "degating"],
                      "die_casting_machining": ["die_casting", "degating", "milling", "drilling"]}
    assert "die" in NOT_ASSESSED[0]
    for name in PROCESS_FIXTURES:
        builder = PROCESS_FIXTURES[name]
        c = _post("/cad/operations/suggest", builder()).json()["process_classification"]
        assert c["system_family"] != "die_casting"
        assert all(candidate["family"] != "die_casting" for candidate in c.get("candidates", []))


def test_die_casting_supplies_its_material_and_keeps_its_line_when_timed():
    assert "die_casting" in MATERIAL_SUPPLYING_STAGES
    assert TIMEABLE_STAGES["die_casting"] == "die_casting"
    assert "die_casting" in TIMED_KEEPS_MATERIAL_AND_TOOL
    assert set(_SHOT_WORDS) == set(TIMED_KEEPS_MATERIAL_AND_TOOL)
    catalog = client.get("/process-routes").json()
    assert "die_casting" in catalog["yield_stages"] and "die_casting" in catalog["timed_material_stages"]


def _imports(path: Path) -> set[str]:
    named: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            named.add(node.module)
        elif isinstance(node, ast.Import):
            named.update(alias.name for alias in node.names)
    return named


def _code_without_prose(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant
        ) and isinstance(body[0].value.value, str):
            body[0].value.value = ""
    return ast.unparse(tree)


def test_the_core_proposal_casts_no_ray_and_reaches_no_cost():
    tree = ast.parse((PACKAGE / "features" / "draw_direction.py").read_text(encoding="utf-8"))
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name in ("propose_cores", "_undercut_neighbours", "_connected"):
        # Identifiers only, never prose or substrings ("enumerate" holds "rate").
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(functions[name])
            if isinstance(node, (ast.Name, ast.Attribute))
        }
        forbidden = {"blocked_along", "first_hit", "ShapeRays", "measure_draw_direction", "rate_set",
                     "cost", "cycle_time_minutes", "setup_time_minutes", "setStageInput"}
        assert not names & forbidden, (name, names & forbidden)


def test_the_time_model_imports_nothing_new():
    named = {m for m in _imports(PACKAGE / "operation_time.py") if m.startswith("cad_costing_agent")}
    assert named == {"cad_costing_agent.features.model"}
    code = _code_without_prose(PACKAGE / "operation_time.py")
    assert "random" not in code


def _js_code(name: str) -> str:
    source = (PACKAGE / "static" / name).read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def test_the_browser_mirrors_the_shot_words_and_works_nothing_out():
    plan = _js_code("plan.js")
    words = plan.split("const SHOT_WORDS = ", 1)[1].split("\n};", 1)[0]
    for stage, entry in _SHOT_WORDS.items():
        line = re.search(rf"^  {stage}: \{{(.*)\}},?$", words, re.M).group(1)
        for key in ("machine", "tool", "rest"):
            assert f'{key}: "{entry[key]}"' in line, (stage, key)
    assert set(re.findall(r"^  (\w+): \{", words, re.M)) == set(_SHOT_WORDS)
    work = plan.split("const TIMED_STAGE_WORK = ", 1)[1].split("\n};", 1)[0]
    assert "add-die-casting-line" in work
    process = _js_code("process.js")
    assert "/cad/casting-modulus" in process and "/operations/die-casting-line" in process
    assert 'setStageInput(input.dataset.stage, "cores", raw === "" ? null : Number(raw))' in process
    block = process.split("async function addMouldLine()", 1)[1].split("document.addEventListener", 1)[0]
    assert not re.search(r"[^/]/\s*\d|\*\s*\d|Math\.", block)


def test_the_core_card_shows_the_servers_count_and_applies_it_only_on_a_click():
    code = _js_code("cores.js")
    assert "/cad/cores/propose" in code
    assert not re.search(r"[^/]/\s*\d|\*\s*\d|Math\.|\.length\s*[-+*/]|reduce\(", code)
    # The stage input is written in exactly one place: the "Use this count" click.
    assert code.count("setStageInput(") == 1
    handler = code.split('$("cores-use").addEventListener("click"', 1)[1]
    assert "setStageInput(" in handler
    assert '<script src="cores.js"></script>' in (PACKAGE / "static" / "index.html").read_text()


def test_the_time_setup_round_trips_the_die_casting_table():
    code = _js_code("timesetup.js")
    assert "die_casting_alloys" in code and "t-diecast-table" in code
    for key in ("mould_constant", "exponent"):
        assert key in code
    html = (PACKAGE / "static" / "index.html").read_text()
    for key in (*REQUIRED_KEYS[1:], "die_casting_core_seconds_per_core", "moulding_core_seconds_per_core"):
        assert f'data-param="{key}"' in html, key


def _publish_script():
    path = ROOT / "scripts" / "publish_illustrative_die_casting_time_profile.py"
    spec = importlib.util.spec_from_file_location("publish_illustrative_die_casting", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_illustrative_profile_is_the_baseline_plus_labelled_die_casting_figures():
    script = _publish_script()
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    profile = script.build_profile(baseline, "1.0.0")
    assert profile["is_illustrative"] is True
    assert "ESTIMATED" in profile["description"] and "no published source" in profile["description"]
    assert "https://en.wikipedia.org/wiki/Chvorinov%27s_rule" in profile["description"]
    differing = {k for k in profile["assumptions"] if profile["assumptions"][k] != baseline.get(k)}
    assert differing == {*REQUIRED_KEYS, "die_casting_core_seconds_per_core"}
    assumptions = TimeAssumptions(**profile["assumptions"])
    assert all("ESTIMATED" in row.note for row in assumptions.die_casting_alloys)
    lines = (ROOT / "scripts" / "publish_illustrative_die_casting_time_profile.py").read_text().splitlines()
    for key in (*REQUIRED_KEYS[1:], "die_casting_core_seconds_per_core"):
        index = next(i for i, text in enumerate(lines) if f'"{key}":' in text)
        assert "ESTIMATED" in lines[index - 1], key
    with pytest.raises(ValueError, match="already carries die-casting parameters"):
        script.build_profile({"die_casting_fill_seconds": 1.0})
