"""An external cylinder is turned only where the body turns about it.

The suggestions used to propose turning for every external cylinder while the
route recommendation, in the same response, said the same cylinders were
"treated as milled bosses" because the part was not rotational. Both now read
the one rotational test (`features.rotational`), run on the cylinder's own
solid, so they cannot say different things about one face:

* a cylinder on a body the test did not pass is a milling suggestion (a milled
  boss), with the test's own sentence as evidence and no borrowed turning time
  — its time is the boss-milling formula's (`boss_milling_time_v1`): the cutter
  centre's circle round the boss, once per axial pass, at the milling feed;
* a cylinder on a body of revolution is turned exactly as before;
* a file with several solids is tested solid by solid — a tube modelled beside a
  bracket stays turned, and the bracket's boss does not.
"""

from __future__ import annotations

import ast
import inspect
import math
import re
from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.features import recognize_features_from_bytes
from cad_costing_agent.features import rotational
from cad_costing_agent.features.model import (
    Confidence,
    Feature,
    FeatureRecognitionResult,
    FeatureType,
    Point3D,
)
from cad_costing_agent.main import app
from cad_costing_agent.operation_suggestions import (
    BOSS_MILLING_RULE,
    MILLING,
    TURNING,
    suggest_operations,
)
from cad_costing_agent import operation_time
from cad_costing_agent.manufacturing_plan import build_plan
from cad_costing_agent.operation_time import (
    DEFAULT_ASSUMPTIONS,
    TimeAssumptions,
    TimeStatus,
    estimate_for_suggestion,
    estimate_operation_time,
)
from cad_costing_agent.process_classification import classify_process
from tests import step_fixtures

client = TestClient(app)

BOSS = "plate_with_boss_and_bores"
TWO = "bracket_and_tube"
TURNED = ("stepped_shaft", "stepped_shaft_with_axial_hole", "stepped_shaft_with_cross_hole")
# Every analytic fixture with a recognizable feature set: the agreement test
# walks all of them, not only the ones written for this behaviour.
EVERY_FIXTURE = (
    "plain_block",
    "block_with_through_hole",
    "plate_with_four_holes",
    "block_with_blind_hole",
    "block_with_pocket",
    "block_with_slot",
    "plate_with_boss_and_bores",
    "block_with_counterbore",
    "block_with_countersunk_hole",
    "block_with_fillet",
    "block_with_chamfer",
    "two_solids",
    "rotated_block_with_pocket",
    "stepped_shaft",
    "stepped_shaft_with_axial_hole",
    "stepped_shaft_with_cross_hole",
    "bent_sheet_bracket",
    "flat_sheet_blank",
    "thin_plate_with_pocket",
    "tube_3mm_wall",
    "plate_2mm_with_6mm_boss",
    "bracket_and_tube",
)


A = DEFAULT_ASSUMPTIONS
BOSS_METHOD = "boss_milling_time_v1"


def hand_cycle(diameter: float, length: float, a: TimeAssumptions = A) -> float:
    """The boss formula written out independently of the module, unrounded."""
    rpm = 1000.0 * a.milling_cutting_speed_m_per_min / (math.pi * a.milling_cutter_diameter_mm)
    rpm = min(max(rpm, a.min_spindle_rpm), a.max_spindle_rpm)
    feed = a.milling_feed_per_tooth_mm * a.milling_teeth * rpm
    passes = math.ceil(length / a.milling_axial_depth_of_cut_mm)
    return math.pi * (diameter + a.milling_cutter_diameter_mm) * passes / feed


def boss_time(diameter=None, length=None, **kwargs):
    dimensions = {}
    if diameter is not None:
        dimensions["diameter_mm"] = diameter
    if length is not None:
        dimensions["length_mm"] = length
    return estimate_operation_time(
        operation_type="milling",
        feature_type="cylinder",
        feature_subtype="shaft",
        dimensions=dimensions,
        **kwargs,
    )


@lru_cache(maxsize=None)
def step(name: str) -> bytes:
    return getattr(step_fixtures, name)()


@lru_cache(maxsize=None)
def analysed(name: str):
    recognition = recognize_features_from_bytes(step(name), f"{name}.step")
    suggestions = suggest_operations(recognition)
    return recognition, suggestions, classify_process(recognition, suggestions)


def shaft_suggestions(suggestions):
    return [s for s in suggestions.suggestions if s.feature_subtype == "shaft"]


# --- a boss on a part that does not turn about it -----------------------------


def test_a_boss_on_a_non_rotational_part_is_a_milling_suggestion_with_its_reason():
    recognition, suggestions, _ = analysed(BOSS)

    (boss,) = shaft_suggestions(suggestions)
    assert boss.operation_type == MILLING
    assert boss.rule_id == BOSS_MILLING_RULE
    assert "not a body of revolution" in boss.reason
    assert "nothing to chuck" in boss.reason
    # The evidence is the rotational test's own sentence, not a paraphrase.
    detail = rotational.rotational_form(list(recognition.features)).detail
    assert f"rotational test: {detail}" in boss.evidence
    # Turning is still the engineer's to choose; it is not the proposal.
    assert boss.alternatives == [TURNING]
    assert boss.confidence is Confidence.MEDIUM
    assert boss.operation_name.startswith("Mill ")
    assert all(s.operation_type != TURNING for s in suggestions.suggestions)


def test_a_milled_boss_is_timed_by_the_boss_formula_and_borrows_no_turning_time():
    """The cutter profiles round the boss; the time is never a turning time."""
    _, suggestions, _ = analysed(BOSS)
    (boss,) = shaft_suggestions(suggestions)

    estimate = estimate_for_suggestion(boss)
    assert estimate.time_status is TimeStatus.ESTIMATED
    assert estimate.method == BOSS_METHOD
    assert "turning" not in (estimate.method or "")
    assert not any(key.startswith("turning_") for key in estimate.parameters)
    # Ø18 × 10 mm: π × (18 + 12) mm × ceil(10 / 2) passes at the milling feed.
    assert estimate.cycle_time_minutes == round(hand_cycle(18.0, 10.0), 2) == 0.69
    assert estimate.setup_time_minutes == A.milling_setup_minutes
    assert estimate.confidence is Confidence.MEDIUM


def test_a_cylinder_that_cannot_be_tested_is_not_proposed_for_turning():
    feature = Feature(
        feature_id="CYL-001",
        feature_type=FeatureType.CYLINDER,
        subtype="shaft",
        solid_index=0,
        source_type="brep",
        dimensions={"diameter_mm": 30.0, "length_mm": 80.0},
        location=Point3D.of(0.0, 0.0, 0.0),
        axis=None,
        source_faces=[3],
        confidence=Confidence.HIGH,
        detection_method="complete_cylindrical_surface",
        evidence=["a geometric condition that held"],
    )
    recognition = FeatureRecognitionResult(
        filename="synthetic.step",
        source_format="step",
        representation="brep",
        solid_count=1,
        face_count=3,
        edge_count=3,
        solids=[],
        features=[feature],
        feature_counts={},
    )

    (suggestion,) = suggest_operations(recognition).suggestions
    assert suggestion.operation_type == MILLING
    assert suggestion.rule_id == BOSS_MILLING_RULE
    assert "could not be tested" in suggestion.reason


# --- the boss-milling time ----------------------------------------------------


def test_the_boss_cycle_matches_the_formula_worked_out_by_hand(monkeypatch):
    """Ø60 × 25 mm with the baseline: 12 mm cutter, 4 × 0.08 mm, 80 m/min, 2 mm deep."""
    rpm = 1000.0 * 80.0 / (math.pi * 12.0)  # 2122.066 rpm, inside the 50–6000 clamp
    feed = 0.08 * 4 * rpm  # 679.061 mm/min
    path_per_pass = math.pi * (60.0 + 12.0)  # 226.195 mm
    passes = 13  # ceil(25 / 2)
    expected = path_per_pass * passes / feed  # 4.330289 min

    captured = {}
    original = operation_time._boss_milling

    def spy(dimensions, a):
        captured["outcome"] = original(dimensions, a)
        return captured["outcome"]

    monkeypatch.setattr(operation_time, "_boss_milling", spy)
    estimate = boss_time(60.0, 25.0)

    unrounded = captured["outcome"][0]
    assert unrounded == pytest.approx(expected, abs=1e-6)
    assert unrounded == pytest.approx(4.330289, abs=1e-6)
    assert unrounded == pytest.approx(hand_cycle(60.0, 25.0), abs=1e-9)
    assert estimate.cycle_time_minutes == 4.33
    assert estimate.setup_time_minutes == 10.0
    assert estimate.total_time_minutes == 14.33
    assert estimate.method == BOSS_METHOD


def test_a_boss_estimate_records_the_parameters_that_entered_it_and_only_those():
    estimate = boss_time(60.0, 25.0)

    assert estimate.parameters == {
        "milling_cutting_speed_m_per_min": 80.0,
        "milling_feed_per_tooth_mm": 0.08,
        "milling_teeth": 4.0,
        "milling_cutter_diameter_mm": 12.0,
        "milling_axial_depth_of_cut_mm": 2.0,
        "milling_setup_minutes": 10.0,
        "min_spindle_rpm": 50.0,
        "max_spindle_rpm": 6000.0,
        "resolved_spindle_rpm": 2122.066,
        "resolved_feed_rate_mm_per_min": 679.061,
        "resolved_path_per_pass_mm": 226.195,
        "passes": 13.0,
    }
    # No stepover and no approach entered this number, so neither is recorded.
    assert "milling_stepover_ratio" not in estimate.parameters
    assert "milling_approach_mm" not in estimate.parameters


def test_a_boss_estimate_states_what_it_leaves_out():
    text = " | ".join(boss_time(60.0, 25.0).assumptions)

    assert "one radial pass around the boss at full finishing engagement" in text
    assert "no roughing of the surrounding stock" in text
    assert "the CAD does not say what stock stood around this boss" in text
    assert "no approach or retract move and no tool change" in text


@pytest.mark.parametrize(
    "length, passes",
    [(2.0, 1), (6.0, 3), (6.0001, 4), (25.0, 13), (26.0, 13), (26.5, 14), (0.9, 1)],
)
def test_axial_passes_are_the_length_over_the_depth_rounded_up(length, passes):
    estimate = boss_time(18.0, length)

    assert estimate.parameters["passes"] == float(passes)
    feed = 0.08 * 4 * 1000.0 * 80.0 / (math.pi * 12.0)
    assert estimate.cycle_time_minutes == round(math.pi * 30.0 * passes / feed, 2)


def test_every_boss_parameter_changes_the_answer():
    base = boss_time(60.0, 25.0).cycle_time_minutes
    for field, value in [
        ("milling_cutting_speed_m_per_min", 160.0),
        ("milling_feed_per_tooth_mm", 0.16),
        ("milling_teeth", 2),
        ("milling_cutter_diameter_mm", 20.0),
        ("milling_axial_depth_of_cut_mm", 5.0),
    ]:
        changed = boss_time(60.0, 25.0, assumptions=TimeAssumptions(**{field: value}))
        assert changed.cycle_time_minutes != base, field
        assert changed.cycle_time_minutes == round(
            hand_cycle(60.0, 25.0, TimeAssumptions(**{field: value})), 2
        ), field
    # And the two the formula does not read leave it alone.
    for field, value in [("milling_stepover_ratio", 0.2), ("milling_approach_mm", 50.0)]:
        same = boss_time(60.0, 25.0, assumptions=TimeAssumptions(**{field: value}))
        assert same.cycle_time_minutes == base, field


@pytest.mark.parametrize(
    "diameter, length, missing",
    [
        (None, 25.0, ["diameter_mm"]),
        (60.0, None, ["length_mm"]),
        (None, None, ["diameter_mm", "length_mm"]),
        (0.0, 25.0, ["diameter_mm"]),
        (60.0, -1.0, ["length_mm"]),
    ],
)
def test_a_boss_without_its_diameter_or_length_is_asked_for_never_defaulted(
    diameter, length, missing
):
    estimate = boss_time(diameter, length)

    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.missing == missing
    assert estimate.cycle_time_minutes is None
    assert estimate.setup_time_minutes is None
    assert estimate.total_time_minutes is None
    for name in missing:
        assert name in estimate.reason
    # A boss never falls through to the area formula's question.
    assert "area" not in estimate.reason


def test_a_boss_in_unknown_units_is_refused_like_any_other_operation():
    estimate = boss_time(60.0, 25.0, units="as written in the file")
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.missing == ["units"]


# A fast, small cutter, so the cycle can be brought down to the reporting
# resolution: 6000 rpm (the clamp) × 4 teeth × 0.08 mm = 1920 mm/min.
FAST = TimeAssumptions(milling_cutter_diameter_mm=1.0)


def test_a_boss_cycle_that_would_be_reported_as_zero_is_not_an_estimate():
    # π × (2.0 + 1) mm / 1920 mm/min = 0.004909 min: reported as 0.00.
    estimate = boss_time(2.0, 1.0, assumptions=FAST)

    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.estimated is False
    assert estimate.cycle_time_minutes is None
    assert estimate.missing == ["cycle_time_minutes"]
    assert estimate.method == BOSS_METHOD
    unrounded = estimate.parameters["unrounded_cycle_time_minutes"]
    assert unrounded == pytest.approx(math.pi * 3.0 / 1920.0, abs=1e-9)
    assert unrounded == pytest.approx(0.004909, abs=1e-6)
    assert estimate.parameters["time_resolution_minutes"] == 0.01
    assert "rounds to 0" in estimate.reason


def test_just_above_the_zero_boundary_a_boss_is_an_ordinary_rounded_estimate():
    # π × (2.1 + 1) mm / 1920 mm/min = 0.005072 min: reported as 0.01.
    estimate = boss_time(2.1, 1.0, assumptions=FAST)

    assert estimate.time_status is TimeStatus.ESTIMATED
    assert estimate.cycle_time_minutes == 0.01
    assert "unrounded_cycle_time_minutes" not in estimate.parameters


def test_a_boss_time_is_medium_at_most_and_no_stronger_than_its_suggestion():
    assert boss_time(60.0, 25.0).confidence is Confidence.MEDIUM
    assert boss_time(60.0, 25.0, suggestion_confidence=Confidence.HIGH).confidence is (
        Confidence.MEDIUM
    )
    assert boss_time(60.0, 25.0, suggestion_confidence=Confidence.LOW).confidence is Confidence.LOW


@pytest.mark.parametrize(
    "kwargs",
    [
        # Not milling: an external cylinder that is turned keeps the turning formula.
        dict(operation_type="turning", feature_type="cylinder", feature_subtype="shaft"),
        dict(operation_type="drilling", feature_type="cylinder", feature_subtype="shaft"),
        # Milling, but not an external cylinder.
        dict(operation_type="milling", feature_type="cylinder", feature_subtype="bore"),
        dict(operation_type="milling", feature_type="cylinder", feature_subtype=None),
        dict(operation_type="milling", feature_type="hole", feature_subtype="shaft"),
        dict(operation_type="milling", feature_type=None, feature_subtype=None),
        dict(operation_type="milling", feature_type="plane", feature_subtype=None),
        dict(operation_type="milling", feature_type="pocket", feature_subtype=None),
        dict(operation_type="milling", feature_type="fillet", feature_subtype="convex"),
    ],
)
def test_the_boss_formula_is_reached_only_for_milling_an_external_cylinder(kwargs):
    dimensions = {
        "diameter_mm": 60.0,
        "length_mm": 25.0,
        "width_mm": 10.0,
        "depth_mm": 4.0,
        "area_mm2": 500.0,
    }
    estimate = estimate_operation_time(dimensions=dimensions, **kwargs)
    assert estimate.method != BOSS_METHOD


def test_the_boss_formula_is_chosen_by_the_feature_never_by_the_operation_name():
    """The estimator is not handed an operation name or a rule id at all."""
    parameters = inspect.signature(estimate_operation_time).parameters
    assert "operation_name" not in parameters
    assert "rule_id" not in parameters


def test_every_other_estimate_on_every_fixture_is_serialized_identically_without_the_boss_formula(
    monkeypatch,
):
    """Nothing but an external cylinder being milled ever reaches the new formula.

    Each suggestion is estimated under its own process and under each machining
    process an engineer could switch it to, twice: once normally, and once with
    the boss formula replaced by one that refuses to run. Every estimate that is
    not a milled external cylinder must serialize to the same bytes both ways.
    """

    def every_estimate():
        out = {}
        for name in EVERY_FIXTURE:
            recognition, suggestions, _ = analysed(name)
            for suggestion in suggestions.suggestions:
                for process in sorted({suggestion.operation_type, "milling", "turning", "drilling"}):
                    is_boss = (
                        process == "milling"
                        and suggestion.feature_type == "cylinder"
                        and suggestion.feature_subtype == "shaft"
                    )
                    if is_boss:
                        continue
                    out[(name, suggestion.feature_id, process)] = estimate_operation_time(
                        operation_type=process,
                        feature_type=suggestion.feature_type,
                        feature_subtype=suggestion.feature_subtype,
                        dimensions=suggestion.dimensions,
                        units=recognition.units,
                        suggestion_confidence=suggestion.confidence,
                    ).model_dump_json()
        return out

    with_formula = every_estimate()

    def refuse(*args, **kwargs):
        raise AssertionError("the boss formula was reached for something that is not a boss")

    monkeypatch.setattr(operation_time, "_boss_milling", refuse)
    without_formula = every_estimate()

    assert len(with_formula) > 400
    assert with_formula == without_formula
    assert not any(BOSS_METHOD in dumped for dumped in with_formula.values())


# (feature, method, cycle) for every milling estimate of two fixtures, written
# out: the face, pocket and blend formulas answer exactly as they did before a
# boss had a time.
EXPECTED_MILLING = {
    "block_with_pocket": [
        ("PKT-001", "milling_pocket_time_baseline_v1", 1.5),
        ("PL-001", "milling_facing_time_baseline_v1", 0.21),
        ("PL-002", "milling_facing_time_baseline_v1", 0.33),
        ("PL-003", "milling_facing_time_baseline_v1", 0.46),
        ("PL-004", "milling_facing_time_baseline_v1", 0.33),
        ("PL-005", "milling_facing_time_baseline_v1", 0.83),
        ("PL-006", "milling_facing_time_baseline_v1", 0.21),
    ],
    "block_with_fillet": [
        ("FIL-001", "milling_blend_finishing_time_baseline_v1", 0.04),
        ("PL-001", "milling_facing_time_baseline_v1", 0.16),
        ("PL-002", "milling_facing_time_baseline_v1", 0.5),
        ("PL-003", "milling_facing_time_baseline_v1", 0.25),
        ("PL-004", "milling_facing_time_baseline_v1", 0.5),
        ("PL-005", "milling_facing_time_baseline_v1", 0.24),
        ("PL-006", "milling_facing_time_baseline_v1", 0.17),
    ],
}


def _milling_shape(name):
    recognition, suggestions, _ = analysed(name)
    rows = []
    for suggestion in suggestions.suggestions:
        if suggestion.operation_type != MILLING:
            continue
        estimate = estimate_for_suggestion(suggestion, units=recognition.units)
        rows.append((suggestion.feature_id, estimate.method, estimate.cycle_time_minutes))
    return rows


@pytest.mark.parametrize("name", sorted(EXPECTED_MILLING))
def test_the_other_milling_formulas_give_the_times_they_gave_before(name):
    assert _milling_shape(name) == EXPECTED_MILLING[name]


def test_the_same_boss_gives_the_same_estimate_twice():
    first = boss_time(60.0, 25.0, suggestion_confidence=Confidence.MEDIUM)
    second = boss_time(60.0, 25.0, suggestion_confidence=Confidence.MEDIUM)
    assert first.model_dump_json() == second.model_dump_json()


# --- in the plan, and under a profile ------------------------------------------


def _boss_plan(profile=None):
    recognition, suggestions, _ = analysed(BOSS)
    plan = build_plan(
        list(suggestions.suggestions),
        units=recognition.units,
        profile=profile,
        source_filename=BOSS,
    )
    (operation,) = [o for o in plan.operations if o.system_suggestion_rule == BOSS_MILLING_RULE]
    return plan, operation


def test_a_boss_in_a_plan_carries_an_estimate_that_is_not_yet_committed():
    plan, boss = _boss_plan()

    assert boss.system_time_status is TimeStatus.ESTIMATED
    assert boss.system_time_method == BOSS_METHOD
    assert boss.system_suggested_cycle_time == 0.69
    # Generating the estimate accepts nothing: the boss is still not costed.
    assert boss.system_time_accepted is False
    assert plan.counts_toward_cost(boss) is False
    assert boss.operation_id in {o.operation_id for o in plan.uncosted_operations()}

    accepted = plan.replacing(boss.accepted(use_estimated_time=True))
    (now,) = [o for o in accepted.operations if o.operation_id == boss.operation_id]
    assert accepted.counts_toward_cost(now) is True


def test_a_boss_timed_under_a_profile_names_it_and_the_estimate_snapshots_its_parameters():
    from cad_costing_agent.time_profiles import TimeProfile

    published = client.post(
        "/time-profiles",
        json={
            "time_profile_id": "boss-shop-time",
            "name": "Boss Shop Machining",
            "version": "1.0.0",
            "description": "A deeper cut per pass than the baseline.",
            "is_illustrative": False,
            "assumptions": {"is_illustrative": False, "milling_axial_depth_of_cut_mm": 5.0},
        },
    )
    assert published.status_code in (200, 201), published.text
    profile = TimeProfile.model_validate(published.json())

    plan, boss = _boss_plan(profile)
    assert boss.system_time_profile_id == "boss-shop-time"
    assert boss.system_time_profile_version == "1.0.0"
    assert boss.system_time_parameters["milling_axial_depth_of_cut_mm"] == 5.0
    assert boss.system_time_parameters["passes"] == 2.0  # ceil(10 / 5)
    assert boss.system_suggested_cycle_time == round(
        hand_cycle(18.0, 10.0, TimeAssumptions(milling_axial_depth_of_cut_mm=5.0)), 2
    )

    plan = plan.replacing(boss.accepted(use_estimated_time=True))
    response = client.post(
        "/cost/cad-part",
        files={"file": (f"{BOSS}.step", step(BOSS), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-BOSS",
            "part_name": "Plate with boss",
            "quantity": "10",
            "rate_set_id": "poc-default",
            "manufacturing_plan": plan.model_dump_json(),
        },
    )
    assert response.status_code == 200, response.text
    stored = client.get(f"/estimates/{response.json()['estimate_id']}").json()
    snapshot = stored["time_profile_snapshot"]["profiles"]["boss-shop-time@1.0.0"]
    assert snapshot["assumptions"]["milling_axial_depth_of_cut_mm"] == 5.0
    assert snapshot["assumptions"]["milling_cutter_diameter_mm"] == 12.0


def test_the_seeded_baseline_is_unchanged_and_no_boss_parameter_was_added():
    """The boss formula reads the milling parameters a pocket is timed with."""
    fields = set(TimeAssumptions.model_fields)
    assert not {name for name in fields if "boss" in name}
    assert (
        A.milling_cutting_speed_m_per_min,
        A.milling_feed_per_tooth_mm,
        A.milling_teeth,
        A.milling_cutter_diameter_mm,
        A.milling_stepover_ratio,
        A.milling_axial_depth_of_cut_mm,
        A.milling_approach_mm,
        A.milling_setup_minutes,
    ) == (80.0, 0.08, 4, 12.0, 0.6, 2.0, 5.0, 10.0)
    seeded = client.get("/time-profiles/poc-baseline-time").json()
    assert seeded["assumptions"]["milling_axial_depth_of_cut_mm"] == 2.0
    assert seeded["assumptions"]["milling_cutter_diameter_mm"] == 12.0


# --- parts that are bodies of revolution: exactly as before -------------------

# (feature subtype or type, operation, rule, confidence) per suggestion, in
# order — written out, so a change to a turned part's proposal fails here by
# value rather than by a hash nobody can read.
EXPECTED_TURNED = {
    "stepped_shaft": [
        ("shaft", "turning", "external_cylinder_to_turning", "high"),
        ("shaft", "turning", "external_cylinder_to_turning", "high"),
        ("plane", "turning", "turned_part_face_to_facing", "medium"),
        ("plane", "turning", "turned_part_face_to_facing", "medium"),
        ("plane", "turning", "turned_part_face_to_facing", "medium"),
    ],
}


def _shape_of(suggestions):
    return [
        (s.feature_subtype or s.feature_type, s.operation_type, s.rule_id, s.confidence.value)
        for s in suggestions.suggestions
    ]


def test_a_stepped_shaft_is_proposed_exactly_as_before():
    _, suggestions, classification = analysed("stepped_shaft")
    assert _shape_of(suggestions) == EXPECTED_TURNED["stepped_shaft"]
    assert [stage.stage for stage in classification.system_route.stages] == ["turning"]


@pytest.mark.parametrize("name", TURNED)
def test_a_turned_part_keeps_every_cylinder_turned_with_its_old_words(name):
    _, suggestions, classification = analysed(name)

    shafts = shaft_suggestions(suggestions)
    assert shafts
    for shaft in shafts:
        assert shaft.operation_type == TURNING
        assert shaft.rule_id == "external_cylinder_to_turning"
        assert shaft.reason == (
            "external cylindrical surface detected, which is the form turning produces"
        )
        assert shaft.alternatives == [MILLING]
        assert shaft.confidence is Confidence.HIGH
        # Nothing about solids or the boss rule leaks into a turned part's evidence.
        assert not any("solid" in line for line in shaft.evidence)
    assert all(s.rule_id != BOSS_MILLING_RULE for s in suggestions.suggestions)
    assert classification.part_form == "rotational"
    assert classification.system_route.stages[0].stage == "turning"


# --- two solids in one file ---------------------------------------------------


def test_each_solid_is_tested_by_itself_and_the_file_as_a_whole_is_not_rotational():
    recognition, _, _ = analysed(TWO)

    assert recognition.solid_count == 2
    features = list(recognition.features)
    assert rotational.rotational_form(features).observed is False
    by_solid = rotational.rotational_form_by_solid(features)
    assert [by_solid[index].observed for index in sorted(by_solid)] == [False, True]


def test_the_bracket_boss_is_milled_and_the_tube_beside_it_stays_turned():
    recognition, suggestions, _ = analysed(TWO)
    solid_of = {f.feature_id: f.solid_index for f in recognition.features}

    shafts = {solid_of[s.feature_id]: s for s in shaft_suggestions(suggestions)}
    assert sorted(shafts) == [0, 1]

    boss, tube = shafts[0], shafts[1]
    assert (boss.operation_type, boss.rule_id) == (MILLING, BOSS_MILLING_RULE)
    assert any("solid 1 of 2" in line for line in boss.evidence)
    assert (tube.operation_type, tube.rule_id) == (TURNING, "external_cylinder_to_turning")

    # Turning is proposed on the tube's solid and nowhere else.
    turned_solids = {
        solid_of[s.feature_id] for s in suggestions.suggestions if s.operation_type == TURNING
    }
    assert turned_solids == {1}
    faced = [s for s in suggestions.suggestions if s.rule_id == "turned_part_face_to_facing"]
    assert faced
    assert all(any("solid 2 of 2" in line for line in s.evidence) for s in faced)


def test_the_route_of_a_two_solid_file_carries_both_stages_and_says_which_solid():
    _, suggestions, classification = analysed(TWO)
    stages = {stage.stage: stage for stage in classification.system_route.stages}

    assert "turning" in stages and "milling" in stages
    assert classification.part_form == "prismatic"
    assert any(
        "as a whole is not one body of revolution" in line and "solid 2 of 2" in line
        for line in stages["turning"].basis
    )
    (boss,) = [s for s in suggestions.suggestions if s.rule_id == BOSS_MILLING_RULE]
    assert any(
        "milled bosses" in line and boss.feature_id in line for line in stages["milling"].basis
    )
    # Two solids still lower the confidence and still ask whether they are joined.
    assert classification.system_confidence is Confidence.LOW


# --- the route and the operations say one thing -------------------------------

_FEATURE_ID = re.compile(r"[A-Z]+-[0-9]+")


@pytest.mark.parametrize("name", EVERY_FIXTURE)
def test_the_route_text_and_the_suggested_operations_never_disagree(name):
    """Fails if a route calls a feature a milled boss while it is suggested for turning."""
    _, suggestions, classification = analysed(name)
    route = classification.system_route
    proposed = {s.feature_id: s.operation_type for s in suggestions.suggestions}
    turned = {feature for feature, process in proposed.items() if process == TURNING}

    stages = {stage.stage: stage for stage in (route.stages if route else [])}
    for stage in stages.values():
        for line in stage.basis:
            if "milled boss" not in line:
                continue
            named = set(_FEATURE_ID.findall(line.split(":")[0]))
            assert named, line
            assert not (named & turned), (name, line)
            assert all(proposed.get(feature) == MILLING for feature in named), (name, line)

    # Nothing anywhere in the route still describes a turning suggestion it overrules.
    text = " ".join(line for stage in stages.values() for line in stage.basis)
    assert "suggested for turning" not in text

    # A machining route carries a turning stage exactly when turning is suggested.
    if "milling" in stages or "turning" in stages:
        assert ("turning" in stages) == bool(turned), (name, sorted(stages), sorted(turned))

    # And the boss rule is the only way a shaft becomes milling.
    for suggestion in shaft_suggestions(suggestions):
        assert (suggestion.operation_type == MILLING) == (
            suggestion.rule_id == BOSS_MILLING_RULE
        )


def test_the_suggest_endpoint_no_longer_contradicts_itself_about_a_boss():
    response = client.post(
        "/cad/operations/suggest",
        files={"file": (f"{BOSS}.step", step(BOSS), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert "turning" not in {s["operation_type"] for s in body["suggestions"]}
    route = body["process_classification"]["system_route"]
    assert [stage["stage"] for stage in route["stages"]] == ["milling", "drilling"]
    # Unreviewed: the route still regroups nothing.
    assert body["process_classification"]["plan_processes"] is None


# --- determinism --------------------------------------------------------------


@pytest.mark.parametrize("name", (BOSS, TWO, "stepped_shaft"))
def test_the_same_file_gives_the_same_suggestions_and_route_twice(name):
    def run():
        recognition = recognize_features_from_bytes(step(name), f"{name}.step")
        suggestions = suggest_operations(recognition)
        classification = classify_process(recognition, suggestions)
        return (
            suggestions.model_dump(mode="json", exclude={"elapsed_ms"}),
            classification.model_dump(mode="json"),
        )

    assert run() == run()


# --- structure ----------------------------------------------------------------


def test_the_per_solid_answer_is_the_one_rotational_test_and_measures_nothing_itself():
    """`rotational_form_by_solid` only chooses which features the single test sees."""
    tree = ast.parse(inspect.getsource(rotational.rotational_form_by_solid))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "rotational_form" in calls
    # No geometry of its own: no comparison, no arithmetic, no tolerance.
    assert not [node for node in ast.walk(tree) if isinstance(node, (ast.Compare, ast.BinOp))]
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "PARALLEL_TOLERANCE" not in names
    assert "AXIS_OFFSET_TOLERANCE_MM" not in names
