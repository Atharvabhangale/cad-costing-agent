"""Sheet cutting and bending times, from the measured flat pattern and bend count.

What is checked: the two formulas against hand-worked values; the cutting
table's rule (a row exactly, a straight line between two rows, and no time at
all outside the table — never extrapolated); every refusal (no sheet
parameters in the profile, a measured figure missing); that the parameters are
absent by default so the seeded profile times no sheet work and every other
estimate is exactly what it was, serialized whole; that the explanation and
recorded parameters name every figure used; and the illustrative profile's
publish script.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cad_costing_agent.main import app
from cad_costing_agent.operation_time import (
    TimeAssumptions,
    TimeStatus,
    WeldSpec,
    estimate_operation_time,
)

client = TestClient(app)

TABLE = [
    {"thickness_mm": 1.0, "cut_speed_mm_per_min": 6000.0, "pierce_seconds": 0.6},
    {"thickness_mm": 3.0, "cut_speed_mm_per_min": 3000.0, "pierce_seconds": 1.8},
    {"thickness_mm": 6.0, "cut_speed_mm_per_min": 1500.0, "pierce_seconds": 3.0},
]
CUTTING = dict(sheet_cutting_table=TABLE, sheet_cutting_setup_minutes=10.0)
BENDING = dict(bending_minutes_per_bend=0.25, bending_setup_minutes=20.0)
SHEET_KEYS = (
    "sheet_cutting_table",
    "sheet_cutting_setup_minutes",
    "sheet_cutting_handling_minutes_per_part",
    "bending_minutes_per_bend",
    "bending_setup_minutes",
    "bending_reposition_minutes_per_bend",
    "bending_handling_minutes_per_part",
)


def cut(thickness=2.0, perimeter=900.0, pierces=3.0, **extra):
    return estimate_operation_time(
        operation_type="sheet_cutting",
        dimensions={"cut_perimeter_mm": perimeter, "pierce_count": pierces, "thickness_mm": thickness},
        assumptions=TimeAssumptions(**CUTTING, **extra),
    )


def bend(bends=3.0, **extra):
    return estimate_operation_time(
        operation_type="bending",
        dimensions={"bend_count": bends, "thickness_mm": 2.0},
        assumptions=TimeAssumptions(**BENDING, **extra),
    )


# --- 1. cutting ---------------------------------------------------------------


def test_between_two_rows_speed_and_pierce_are_read_on_a_straight_line():
    estimate = cut(thickness=2.0)
    # Halfway from 1 mm to 3 mm: 4500 mm/min and 1.2 s.
    # 900 / 4500 = 0.2 min of cut; 3 x 1.2 / 60 = 0.06 min of piercing.
    assert estimate.time_status is TimeStatus.ESTIMATED
    assert estimate.cycle_time_minutes == 0.26
    assert estimate.setup_time_minutes == 10.0
    assert estimate.method == "sheet_cutting_time_v1"
    assert estimate.parameters["sheet_cut_speed_mm_per_min"] == 4500.0
    assert estimate.parameters["sheet_pierce_seconds"] == pytest.approx(1.2)
    assert estimate.parameters["sheet_cut_table_lower_thickness_mm"] == 1.0
    assert estimate.parameters["sheet_cut_table_upper_thickness_mm"] == 3.0
    assert estimate.parameters["cut_perimeter_mm"] == 900.0
    assert estimate.parameters["pierce_count"] == 3.0
    assert estimate.assumptions[0] == (
        "cut speed 4500 mm/min and pierce 1.2 s at 2 mm: interpolated linearly between the 1 mm "
        "row (6000 mm/min, 0.6 s) and the 3 mm row (3000 mm/min, 1.8 s)"
    )
    assert "cut 0.20 min = 900 mm / 4500 mm/min" in estimate.assumptions
    assert "pierces 0.06 min = 3 × 1.2 s / 60" in estimate.assumptions
    assert any("measured from the unfolded CAD model" in line for line in estimate.evidence)


def test_on_a_row_that_row_is_used():
    estimate = cut(thickness=3.0)
    # 900 / 3000 = 0.3; 3 x 1.8 / 60 = 0.09.
    assert estimate.cycle_time_minutes == 0.39
    assert estimate.parameters["sheet_cut_table_lower_thickness_mm"] == 3.0
    assert estimate.parameters["sheet_cut_table_upper_thickness_mm"] == 3.0
    assert "the 3 mm row" in estimate.assumptions[0]


@pytest.mark.parametrize("thickness", [0.5, 6.5, 12.0])
def test_outside_the_table_no_cut_speed_is_extrapolated(thickness):
    estimate = cut(thickness=thickness)
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.cycle_time_minutes is None and estimate.setup_time_minutes is None
    assert estimate.missing == ["sheet_cutting_table"]
    assert "not extrapolated" in estimate.reason and "1–6 mm" in estimate.reason


def test_unloading_is_added_per_part_and_only_when_the_profile_states_it():
    estimate = cut(sheet_cutting_handling_minutes_per_part=0.3)
    assert estimate.cycle_time_minutes == 0.56
    assert estimate.setup_time_minutes == 10.0
    assert estimate.parameters["sheet_cutting_handling_minutes_per_part"] == 0.3
    assert "cycle 0.56 min = 0.20 + 0.06 + 0.3" in estimate.assumptions
    assert "sheet_cutting_handling_minutes_per_part" not in cut().parameters


@pytest.mark.parametrize(
    "dimensions, missing",
    [
        ({"pierce_count": 3.0, "thickness_mm": 2.0}, ["cut_perimeter_mm"]),
        ({"cut_perimeter_mm": 900.0, "thickness_mm": 2.0}, ["pierce_count"]),
        ({"cut_perimeter_mm": 900.0, "pierce_count": 2.5, "thickness_mm": 2.0}, ["pierce_count"]),
        ({"cut_perimeter_mm": 900.0, "pierce_count": 3.0}, ["thickness_mm"]),
        ({}, ["cut_perimeter_mm", "pierce_count", "thickness_mm"]),
    ],
)
def test_a_cut_missing_a_measured_figure_is_never_given_a_time(dimensions, missing):
    estimate = estimate_operation_time(
        operation_type="sheet_cutting", dimensions=dimensions, assumptions=TimeAssumptions(**CUTTING)
    )
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.missing == missing
    assert "K-factor" in estimate.reason


def test_a_cutting_table_must_be_thinnest_first_and_not_empty():
    with pytest.raises(ValidationError, match="thinnest first"):
        TimeAssumptions(sheet_cutting_table=list(reversed(TABLE)))
    with pytest.raises(ValidationError, match="thinnest first"):
        TimeAssumptions(sheet_cutting_table=[TABLE[0], TABLE[0]])
    with pytest.raises(ValidationError, match="at least one row"):
        TimeAssumptions(sheet_cutting_table=[])
    with pytest.raises(ValidationError):
        TimeAssumptions(
            sheet_cutting_table=[{"thickness_mm": 1.0, "cut_speed_mm_per_min": 0.0, "pierce_seconds": 1.0}]
        )


# --- 2. bending ------------------------------------------------------------------


def test_bending_is_a_stroke_per_measured_bend():
    estimate = bend()
    # 3 x 0.25 = 0.75 min; setup 20.
    assert estimate.cycle_time_minutes == 0.75
    assert estimate.setup_time_minutes == 20.0
    assert estimate.method == "bending_time_v1"
    assert estimate.parameters == {
        "bending_minutes_per_bend": 0.25,
        "bending_setup_minutes": 20.0,
        "bend_count": 3.0,
    }
    assert estimate.assumptions[0] == "strokes 0.75 min = 3 × 0.25 min per bend"


def test_repositioning_is_between_bends_and_handling_is_per_part():
    estimate = bend(bending_reposition_minutes_per_bend=0.15, bending_handling_minutes_per_part=0.3)
    # 0.75 + (3 - 1) x 0.15 + 0.3 = 1.35.
    assert estimate.cycle_time_minutes == 1.35
    assert "cycle 1.35 min = 0.75 + 0.30 + 0.3" in estimate.assumptions
    one = bend(bends=1.0, bending_reposition_minutes_per_bend=0.15)
    assert one.cycle_time_minutes == 0.25


@pytest.mark.parametrize("bends", [None, 0.0, 1.5])
def test_a_bend_count_that_is_not_a_whole_measured_number_gets_no_time(bends):
    dimensions = {} if bends is None else {"bend_count": bends}
    estimate = estimate_operation_time(
        operation_type="bending", dimensions=dimensions, assumptions=TimeAssumptions(**BENDING)
    )
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.missing == ["bend_count"]


# --- 3. absent by default, and nothing else changes ------------------------------


def test_the_seeded_baseline_times_no_sheet_work():
    baseline = TimeAssumptions()
    assert all(getattr(baseline, key) is None for key in SHEET_KEYS)
    for process, name in (("sheet_cutting", "sheet_cutting_table"), ("bending", "bending_minutes_per_bend")):
        estimate = estimate_operation_time(
            operation_type=process,
            dimensions={"cut_perimeter_mm": 900.0, "pierce_count": 3.0, "thickness_mm": 2.0, "bend_count": 2.0},
        )
        assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
        assert name in estimate.missing
        assert "no sheet-cutting parameters" in estimate.reason or "no bending parameters" in estimate.reason
    stored = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    assert all(stored[key] is None for key in SHEET_KEYS)


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
    ],
)
def test_sheet_parameters_change_no_other_estimate_serialized_whole(kwargs):
    welding = dict(
        welding_mig_travel_speed_mm_per_min=300.0, welding_tig_travel_speed_mm_per_min=100.0,
        welding_mm_per_pass=6.0, welding_arc_on_factor=0.5, welding_setup_minutes=15.0,
    )
    plain = estimate_operation_time(**kwargs, assumptions=TimeAssumptions(**welding))
    explicit_none = estimate_operation_time(
        **kwargs, assumptions=TimeAssumptions(**welding, **{key: None for key in SHEET_KEYS})
    )
    with_sheet = estimate_operation_time(
        **kwargs,
        assumptions=TimeAssumptions(**welding, **CUTTING, **BENDING, bending_handling_minutes_per_part=0.3),
    )
    assert plain.time_status is TimeStatus.ESTIMATED
    assert plain.model_dump_json() == explicit_none.model_dump_json() == with_sheet.model_dump_json()


def test_the_estimate_time_endpoint_times_a_measured_cut():
    body = {
        "assumptions": CUTTING,
        "operations": [{"operation_id": "C1", "operation_type": "sheet_cutting",
                        "dimensions": {"cut_perimeter_mm": 900.0, "pierce_count": 3.0, "thickness_mm": 2.0}}],
    }
    response = client.post("/operations/estimate-time", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["estimates"][0]["estimate"]["cycle_time_minutes"] == 0.26


def test_a_profile_with_a_cutting_table_publishes_and_snapshots_it():
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    body = {
        "time_profile_id": "sheet-test-time", "name": "Sheet test", "version": "1.0.0",
        "description": "test", "is_illustrative": True,
        "assumptions": {**baseline, **CUTTING, **BENDING},
    }
    response = client.post("/time-profiles", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["assumptions"]["sheet_cutting_table"] == TABLE
    response = client.post(
        "/operations/estimate-time?time_profile_id=sheet-test-time",
        json={"operations": [{"operation_id": "B1", "operation_type": "bending",
                              "dimensions": {"bend_count": 2.0}}]},
    )
    estimate = response.json()["estimates"][0]["estimate"]
    assert estimate["cycle_time_minutes"] == 0.5
    assert estimate["time_profile_id"] == "sheet-test-time"


# --- 4. the illustrative profile -----------------------------------------------------


def _publish_script():
    path = Path(__file__).parents[1] / "scripts" / "publish_illustrative_sheet_time_profile.py"
    spec = importlib.util.spec_from_file_location("publish_illustrative_sheet", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_illustrative_sheet_profile_is_the_baseline_plus_labelled_sheet_figures():
    script = _publish_script()
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    profile = script.build_profile(baseline, "1.0.0")
    assert profile["is_illustrative"] is True
    assert "ESTIMATED" in profile["description"] and "no published source" in profile["description"]
    differing = {key for key in profile["assumptions"] if profile["assumptions"][key] != baseline.get(key)}
    assert differing == set(SHEET_KEYS)
    # It validates as a time profile exactly as published.
    assumptions = TimeAssumptions(**profile["assumptions"])
    thicknesses = [row.thickness_mm for row in assumptions.sheet_cutting_table]
    assert thicknesses == sorted(thicknesses)
    # Every sheet figure is commented as an estimate in the script itself.
    source = (Path(__file__).parents[1] / "scripts" / "publish_illustrative_sheet_time_profile.py").read_text()
    for key in SHEET_KEYS[1:]:
        line = next(i for i, text in enumerate(source.splitlines()) if f'"{key}":' in text)
        assert "ESTIMATED" in source.splitlines()[line - 1]


def test_the_script_refuses_to_overwrite_a_baseline_that_already_times_sheet_work():
    script = _publish_script()
    with pytest.raises(ValueError, match="already carries sheet parameters"):
        script.build_profile({"bending_minutes_per_bend": 0.5})
