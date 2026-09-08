"""Deterministic processing-time estimation.

The formulas are textbook, so these tests check them the way you would check a
machinist's arithmetic: against a value worked out by hand from the same
parameters. That catches a transposed constant, which asserting "a number came
back" never would.

The rest of the file defends the two properties that make an estimated time safe
to show at all: it is **explained** — method, evidence and every parameter that
affected it — and it is **absent** rather than invented whenever the geometry or
the units cannot support one.
"""

from __future__ import annotations

import math

import pytest

from cad_costing_agent.features.model import Confidence
from cad_costing_agent.operation_time import (
    DEFAULT_ASSUMPTIONS,
    METHOD_CONFIDENCE_CEILING,
    OperationTimeInput,
    OperationTimeRequest,
    TimeAssumptions,
    TimeStatus,
    estimate_operation_time,
    estimate_operations,
)

A = DEFAULT_ASSUMPTIONS


def rpm(cutting_speed: float, diameter: float, assumptions: TimeAssumptions = A) -> float:
    raw = (1000.0 * cutting_speed) / (math.pi * diameter)
    return min(max(raw, assumptions.min_spindle_rpm), assumptions.max_spindle_rpm)


# --- drilling ----------------------------------------------------------------


def test_drilling_time_matches_the_formula_worked_out_by_hand():
    """Ø10 × 20 deep through hole, at the baseline parameters."""
    estimate = estimate_operation_time(
        operation_type="drilling",
        feature_type="hole",
        feature_subtype="through",
        dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
    )

    speed = rpm(A.drilling_cutting_speed_m_per_min, 10.0)
    feed_rate = A.drilling_feed_mm_per_rev * speed
    travel = 20.0 + A.drilling_approach_mm + A.drilling_breakthrough_mm
    assert estimate.cycle_time_minutes == pytest.approx(round(travel / feed_rate, 2))
    assert estimate.setup_time_minutes == A.drilling_setup_minutes
    assert estimate.total_time_minutes == pytest.approx(
        estimate.setup_time_minutes + estimate.cycle_time_minutes
    )
    assert estimate.method == "drilling_time_baseline_v1"


def test_a_blind_hole_gets_no_breakthrough_travel():
    """The drill stops at depth, so it is quicker than the same hole through."""
    common = {"diameter_mm": 10.0, "depth_mm": 20.0}
    through = estimate_operation_time(
        operation_type="drilling", feature_subtype="through", dimensions=common
    )
    blind = estimate_operation_time(
        operation_type="drilling", feature_subtype="blind", dimensions=common
    )

    assert blind.cycle_time_minutes < through.cycle_time_minutes
    assert any("no breakthrough travel" in line for line in blind.assumptions)


def test_a_deeper_hole_takes_longer_and_a_wider_one_takes_longer_per_mm():
    shallow = estimate_operation_time(
        operation_type="drilling", dimensions={"diameter_mm": 10.0, "depth_mm": 10.0}
    )
    deep = estimate_operation_time(
        operation_type="drilling", dimensions={"diameter_mm": 10.0, "depth_mm": 40.0}
    )
    assert deep.cycle_time_minutes > shallow.cycle_time_minutes

    # A bigger drill turns slower at the same cutting speed, so feed rate falls.
    narrow = estimate_operation_time(
        operation_type="drilling", dimensions={"diameter_mm": 5.0, "depth_mm": 20.0}
    )
    wide = estimate_operation_time(
        operation_type="drilling", dimensions={"diameter_mm": 40.0, "depth_mm": 20.0}
    )
    assert wide.cycle_time_minutes > narrow.cycle_time_minutes


def test_the_spindle_speed_is_clamped_for_a_tiny_hole():
    """1000·Vc/(π·D) runs away as D shrinks; the machine cannot."""
    estimate = estimate_operation_time(
        operation_type="drilling", dimensions={"diameter_mm": 0.05, "depth_mm": 1.0}
    )

    assert estimate.estimated
    assert any(
        f"{A.min_spindle_rpm:g}–{A.max_spindle_rpm:g} rpm" in line for line in estimate.assumptions
    )


# --- milling -----------------------------------------------------------------


def test_facing_a_plane_matches_the_formula_worked_out_by_hand():
    estimate = estimate_operation_time(
        operation_type="milling", feature_type="plane", dimensions={"area_mm2": 5000.0}
    )

    speed = rpm(A.milling_cutting_speed_m_per_min, A.milling_cutter_diameter_mm)
    feed_rate = A.milling_feed_per_tooth_mm * A.milling_teeth * speed
    stepover = A.milling_cutter_diameter_mm * A.milling_stepover_ratio
    path = 5000.0 / stepover
    assert estimate.cycle_time_minutes == pytest.approx(
        round((path + A.milling_approach_mm) / feed_rate, 2)
    )
    assert estimate.method == "milling_facing_time_baseline_v1"


def test_a_pocket_is_cut_in_axial_passes():
    shallow = estimate_operation_time(
        operation_type="milling",
        feature_type="pocket",
        dimensions={"area_mm2": 2000.0, "depth_mm": A.milling_axial_depth_of_cut_mm},
    )
    deep = estimate_operation_time(
        operation_type="milling",
        feature_type="pocket",
        dimensions={"area_mm2": 2000.0, "depth_mm": A.milling_axial_depth_of_cut_mm * 3},
    )

    assert shallow.method == "milling_pocket_time_baseline_v1"
    assert deep.cycle_time_minutes == pytest.approx(shallow.cycle_time_minutes * 3, rel=0.01)
    assert any("3 axial pass(es)" in line for line in deep.evidence)


def test_a_slot_uses_the_same_pocket_formula():
    estimate = estimate_operation_time(
        operation_type="milling",
        feature_type="slot",
        dimensions={"length_mm": 40.0, "width_mm": 8.0, "depth_mm": 4.0},
    )

    assert estimate.estimated
    assert estimate.method == "milling_pocket_time_baseline_v1"
    # The area came from length x width, and the evidence says so.
    assert any("40 × 8 mm footprint" in line for line in estimate.evidence)


def test_a_blend_is_finished_along_its_length_not_over_an_area():
    estimate = estimate_operation_time(
        operation_type="milling", feature_type="fillet", dimensions={"radius_mm": 3.0, "length_mm": 50.0}
    )

    speed = rpm(A.milling_cutting_speed_m_per_min, A.milling_cutter_diameter_mm)
    feed_rate = A.milling_feed_per_tooth_mm * A.milling_teeth * speed
    assert estimate.method == "milling_blend_finishing_time_baseline_v1"
    assert estimate.cycle_time_minutes == pytest.approx(
        round((50.0 + A.milling_approach_mm) / feed_rate, 2)
    )


def test_a_floor_area_is_preferred_over_a_bounding_footprint():
    """A pocket reports both; the floor is what the cutter actually sweeps."""
    estimate = estimate_operation_time(
        operation_type="milling",
        feature_type="pocket",
        dimensions={"floor_area_mm2": 900.0, "length_mm": 50.0, "width_mm": 50.0, "depth_mm": 2.0},
    )

    assert any("floor area 900 mm²" in line for line in estimate.evidence)


# --- turning -----------------------------------------------------------------


def test_turning_time_matches_the_formula_worked_out_by_hand():
    estimate = estimate_operation_time(
        operation_type="turning", dimensions={"diameter_mm": 60.0, "length_mm": 100.0}
    )

    speed = rpm(A.turning_cutting_speed_m_per_min, 60.0)
    feed_rate = A.turning_feed_mm_per_rev * speed
    passes = math.ceil(A.turning_stock_allowance_mm / A.turning_depth_of_cut_mm)
    travel = 100.0 + A.turning_approach_mm
    assert estimate.cycle_time_minutes == pytest.approx(round(passes * travel / feed_rate, 2))
    assert estimate.method == "turning_time_baseline_v1"
    assert estimate.setup_time_minutes == A.turning_setup_minutes


def test_turning_states_that_the_stock_allowance_is_an_assumption():
    """The CAD says nothing about the bar; the pass count rests on a guess."""
    estimate = estimate_operation_time(
        operation_type="turning", dimensions={"diameter_mm": 40.0, "length_mm": 60.0}
    )

    assert any("radial stock allowance" in line for line in estimate.assumptions)
    assert any("the CAD says nothing about the bar" in line for line in estimate.assumptions)


# --- insufficient information -> needs_user_input ----------------------------


@pytest.mark.parametrize(
    "kwargs, missing",
    [
        (dict(operation_type="drilling", dimensions={}), ["diameter_mm", "depth_mm"]),
        (dict(operation_type="drilling", dimensions={"diameter_mm": 8.0}), ["depth_mm"]),
        (dict(operation_type="drilling", dimensions={"depth_mm": 8.0}), ["diameter_mm"]),
        (dict(operation_type="turning", dimensions={"diameter_mm": 8.0}), ["length_mm"]),
        (
            dict(operation_type="milling", feature_type="plane", dimensions={}),
            ["area_mm2"],
        ),
        (
            dict(
                operation_type="milling",
                feature_type="pocket",
                dimensions={"area_mm2": 100.0},
            ),
            ["depth_mm"],
        ),
        (
            dict(operation_type="milling", feature_type="fillet", dimensions={"radius_mm": 2.0}),
            ["length_mm"],
        ),
    ],
)
def test_missing_dimensions_yield_needs_user_input_and_no_number(kwargs, missing):
    estimate = estimate_operation_time(**kwargs)

    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.estimated is False
    # Null, not zero: nothing downstream can add a missing time into a total.
    assert estimate.setup_time_minutes is None
    assert estimate.cycle_time_minutes is None
    assert estimate.total_time_minutes is None
    assert estimate.missing == missing
    assert "No time is assumed" in estimate.reason or "have to be entered" in estimate.reason


def test_an_operation_with_no_formula_is_not_guessed_at():
    estimate = estimate_operation_time(
        operation_type="deburring", dimensions={"length_mm": 100.0}
    )

    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert "no time formula for 'deburring'" in estimate.reason


def test_a_zero_or_negative_dimension_counts_as_missing():
    for value in [0.0, -5.0, float("nan"), float("inf")]:
        estimate = estimate_operation_time(
            operation_type="drilling", dimensions={"diameter_mm": value, "depth_mm": 10.0}
        )
        assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT, value


def test_a_non_numeric_dimension_is_refused_rather_than_crashing():
    estimate = estimate_operation_time(
        operation_type="drilling", dimensions={"diameter_mm": "wide", "depth_mm": 10.0}
    )

    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT


# --- units -------------------------------------------------------------------


def test_units_that_are_not_millimetres_are_refused_not_scaled():
    """A faceted export's dimensions are 'as written'; guessing is a 1000x error."""
    estimate = estimate_operation_time(
        operation_type="drilling",
        dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
        units="as written in the file",
    )

    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.missing == ["units"]
    assert "not millimetres" in estimate.reason


def test_millimetres_are_accepted_however_they_are_spelled():
    for units in ["mm", "MM", " mm "]:
        estimate = estimate_operation_time(
            operation_type="drilling",
            dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
            units=units,
        )
        assert estimate.estimated, units


# --- confidence --------------------------------------------------------------


def test_a_time_is_never_more_confident_than_medium():
    """A generic parameter baseline cannot justify `high`, ever."""
    estimate = estimate_operation_time(
        operation_type="drilling",
        dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
        suggestion_confidence=Confidence.HIGH,
    )

    assert estimate.confidence is METHOD_CONFIDENCE_CEILING is Confidence.MEDIUM


def test_a_weak_operation_suggestion_makes_an_even_weaker_time():
    estimate = estimate_operation_time(
        operation_type="drilling",
        dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
        suggestion_confidence=Confidence.LOW,
    )

    assert estimate.confidence is Confidence.LOW


# --- explanation -------------------------------------------------------------


def test_every_estimate_explains_itself():
    estimate = estimate_operation_time(
        operation_type="drilling",
        feature_subtype="through",
        dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
    )

    assert estimate.method
    assert estimate.reason
    assert estimate.evidence
    assert estimate.assumptions
    # The geometry it consumed, and the arithmetic it did with it.
    assert any("Ø10 mm" in line for line in estimate.evidence)
    assert any("spindle speed" in line for line in estimate.evidence)
    assert any("feed rate" in line for line in estimate.evidence)


def test_every_parameter_that_affected_the_number_is_reported():
    estimate = estimate_operation_time(
        operation_type="drilling", dimensions={"diameter_mm": 10.0, "depth_mm": 20.0}
    )
    assumptions = " ".join(estimate.assumptions)

    for value in [
        f"{A.drilling_cutting_speed_m_per_min:g}",
        f"{A.drilling_feed_mm_per_rev:g}",
        f"{A.drilling_approach_mm:g}",
    ]:
        assert value in assumptions


def test_setup_being_per_operation_line_is_stated_not_hidden():
    estimate = estimate_operation_time(
        operation_type="drilling", dimensions={"diameter_mm": 10.0, "depth_mm": 20.0}
    )

    assert any("per operation line" in line for line in estimate.assumptions)


def test_the_illustrative_baseline_is_flagged_as_such():
    illustrative = estimate_operation_time(
        operation_type="drilling", dimensions={"diameter_mm": 10.0, "depth_mm": 20.0}
    )
    real = estimate_operation_time(
        operation_type="drilling",
        dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
        assumptions=TimeAssumptions(is_illustrative=False),
    )

    assert illustrative.is_illustrative is True
    assert real.is_illustrative is False


# --- configurable assumptions ------------------------------------------------


def test_the_assumptions_are_configurable_and_actually_change_the_answer():
    slow = estimate_operation_time(
        operation_type="drilling",
        dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
        assumptions=TimeAssumptions(drilling_feed_mm_per_rev=0.10),
    )
    fast = estimate_operation_time(
        operation_type="drilling",
        dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
        assumptions=TimeAssumptions(drilling_feed_mm_per_rev=0.40),
    )

    assert fast.cycle_time_minutes < slow.cycle_time_minutes
    assert any("0.4 mm/rev" in line for line in fast.assumptions)


@pytest.mark.parametrize(
    "field, value",
    [
        ("drilling_feed_mm_per_rev", 0.0),
        ("drilling_feed_mm_per_rev", -1.0),
        ("milling_teeth", 0),
        ("milling_stepover_ratio", 1.5),
        ("turning_depth_of_cut_mm", 0.0),
        ("max_spindle_rpm", -10.0),
    ],
)
def test_invalid_assumptions_are_rejected_at_the_model(field, value):
    with pytest.raises(Exception):
        TimeAssumptions(**{field: value})


def test_time_assumptions_are_separate_from_rate_sets():
    """Duration and money are different concerns and must not share a model."""
    from cad_costing_agent.rate_sets import RateSet

    assumption_fields = set(TimeAssumptions.model_fields)
    rate_fields = set(RateSet.model_fields)

    assert not (assumption_fields & rate_fields) - {"is_illustrative"}
    assert "currency" not in assumption_fields
    assert not any("rate_per_hour" in field for field in assumption_fields)


# --- determinism -------------------------------------------------------------


def test_the_same_inputs_always_give_the_same_time():
    kwargs = dict(
        operation_type="milling",
        feature_type="pocket",
        dimensions={"area_mm2": 1234.5, "depth_mm": 7.0},
        suggestion_confidence=Confidence.MEDIUM,
    )

    first = estimate_operation_time(**kwargs).model_dump()
    second = estimate_operation_time(**kwargs).model_dump()

    assert first == second


def test_the_estimator_uses_no_randomness():
    """Guards against a future 'add a bit of variability' shortcut."""
    from pathlib import Path

    import cad_costing_agent.operation_time as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in ["import random", "np.random", "uniform(", "gauss(", "shuffle("]:
        assert forbidden not in source


def test_the_time_module_imports_no_costing_arithmetic():
    from pathlib import Path

    import cad_costing_agent.operation_time as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in [
        "from cad_costing_agent.costing",
        "from cad_costing_agent.operation_costing",
        "from cad_costing_agent.part_cost",
        "from cad_costing_agent.rate_sets",
        "calculate_part_cost",
    ]:
        assert forbidden not in source


def test_no_machine_learning_is_involved():
    from pathlib import Path

    import cad_costing_agent.operation_time as module

    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ["sklearn", "torch", "tensorflow", "model.fit", "train(", "predict("]:
        assert forbidden not in source


# --- the batch API shape -----------------------------------------------------


def test_a_batch_estimates_each_operation_and_counts_the_outcomes():
    response = estimate_operations(
        OperationTimeRequest(
            operations=[
                OperationTimeInput(
                    operation_id="OP-001",
                    operation_type="drilling",
                    dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
                ),
                OperationTimeInput(operation_id="OP-002", operation_type="drilling", dimensions={}),
            ]
        )
    )

    assert response.estimated_count == 1
    assert response.needs_input_count == 1
    assert [result.operation_id for result in response.estimates] == ["OP-001", "OP-002"]


def test_a_batch_warns_that_nothing_is_committed_or_quotable():
    response = estimate_operations(
        OperationTimeRequest(
            operations=[
                OperationTimeInput(
                    operation_type="drilling", dimensions={"diameter_mm": 10.0, "depth_mm": 20.0}
                )
            ]
        )
    )

    joined = " ".join(response.warnings)
    assert "does not adopt it" in joined
    assert "ILLUSTRATIVE PLACEHOLDER VALUES" in joined
    assert "capped at medium confidence" in joined


def test_a_batch_echoes_the_assumptions_it_used():
    custom = TimeAssumptions(drilling_feed_mm_per_rev=0.25, is_illustrative=False)
    response = estimate_operations(
        OperationTimeRequest(assumptions=custom, operations=[])
    )

    assert response.assumptions_used.drilling_feed_mm_per_rev == 0.25
    assert not any("ILLUSTRATIVE" in warning for warning in response.warnings)


def test_an_empty_batch_is_not_an_error():
    response = estimate_operations(OperationTimeRequest(operations=[]))

    assert response.estimates == []
    assert response.estimated_count == 0
