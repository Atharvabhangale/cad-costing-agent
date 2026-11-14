"""An external cylinder is turned only where the body turns about it.

The suggestions used to propose turning for every external cylinder while the
route recommendation, in the same response, said the same cylinders were
"treated as milled bosses" because the part was not rotational. Both now read
the one rotational test (`features.rotational`), run on the cylinder's own
solid, so they cannot say different things about one face:

* a cylinder on a body the test did not pass is a milling suggestion (a milled
  boss), with the test's own sentence as evidence and no borrowed turning time;
* a cylinder on a body of revolution is turned exactly as before;
* a file with several solids is tested solid by solid — a tube modelled beside a
  bracket stays turned, and the bracket's boss does not.
"""

from __future__ import annotations

import ast
import inspect
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
from cad_costing_agent.operation_time import TimeStatus, estimate_for_suggestion
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


def test_a_milled_boss_borrows_no_turning_time():
    """No milling time model sweeps a cylinder's flank, so the time is asked for."""
    _, suggestions, _ = analysed(BOSS)
    (boss,) = shaft_suggestions(suggestions)

    estimate = estimate_for_suggestion(boss)
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.cycle_time_minutes is None
    assert estimate.setup_time_minutes is None
    assert "turning" not in (estimate.method or "")


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
