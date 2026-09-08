"""Feature → manufacturing-operation suggestions.

Two things are being defended, and the second matters more than the first.

1. The rules fire correctly and deterministically: a through hole suggests
   drilling, a shaft suggests turning, a pocket suggests milling, and a feature
   the geometry does not support is **declined with a reason** rather than
   guessed at.
2. A suggestion cannot become money. It carries no time and no rate, nothing in
   the costing path can even import it, and the one bridge into costing —
   `to_operation_requirement` — refuses rather than defaulting a missing time.
   That refusal is the difference between a proposal and a silent charge.

Most rules are exercised against synthetic `Feature` objects, because a rule is
a function of a feature and building a STEP file to reach one would test the
recognizer instead. The end-to-end tests then confirm the same rules fire on
geometry produced by the real recognizers, analytic and faceted.
"""

from __future__ import annotations

from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.features import recognize_features, recognize_features_from_bytes
from cad_costing_agent.features.model import (
    Confidence,
    Feature,
    FeatureRecognitionResult,
    FeatureType,
    Point3D,
)
from cad_costing_agent.main import STATIC_PATH, app
from cad_costing_agent.operation_suggestions import (
    DRILLING,
    MILLING,
    MIN_PLANE_AREA_SHARE,
    NOT_COSTED_REASON,
    TURNING,
    OperationSuggestion,
    SuggestionOrigin,
    UserAction,
    record_user_decision,
    suggest_operations,
    to_operation_requirement,
)
from tests import step_fixtures
from tests.faceted_fixtures import faceted_block_with_boss, faceted_plate_with_four_holes

client = TestClient(app)


# --- helpers -----------------------------------------------------------------


def make_feature(
    feature_id: str = "H-001",
    feature_type: FeatureType = FeatureType.HOLE,
    subtype: str | None = "through",
    dimensions: dict[str, float] | None = None,
    confidence: Confidence = Confidence.HIGH,
    source_type: str = "brep",
    solid_index: int = 0,
    source_faces: tuple[int, ...] = (7, 8),
    detection_method: str = "complete_internal_cylinder_classified_by_end_conditions",
) -> Feature:
    return Feature(
        feature_id=feature_id,
        feature_type=feature_type,
        subtype=subtype,
        solid_index=solid_index,
        source_type=source_type,
        dimensions=dimensions if dimensions is not None else {"diameter_mm": 10.0, "depth_mm": 20.0},
        location=Point3D.of(1.0, 2.0, 3.0),
        axis=Point3D.of(0.0, 0.0, 1.0),
        source_faces=list(source_faces),
        confidence=confidence,
        detection_method=detection_method,
        evidence=["a geometric condition that held"],
    )


def result_for(*features: Feature, representation: str = "brep") -> FeatureRecognitionResult:
    return FeatureRecognitionResult(
        filename="synthetic.step",
        source_format="step",
        representation=representation,
        solid_count=1,
        face_count=40,
        edge_count=90,
        solids=[],
        features=list(features),
        feature_counts={},
    )


def only_suggestion(*features: Feature, **kwargs) -> OperationSuggestion:
    suggestions = suggest_operations(result_for(*features, **kwargs)).suggestions
    assert len(suggestions) == 1, [s.suggestion_id for s in suggestions]
    return suggestions[0]


@lru_cache(maxsize=None)
def step(name: str) -> bytes:
    return getattr(step_fixtures, name)()


@lru_cache(maxsize=None)
def suggestions_for_step(name: str):
    shape = read_step_shape(step(name))
    return suggest_operations(recognize_features(shape, filename=f"{name}.step"))


# --- the rules: features that do map to an operation --------------------------


def test_a_through_hole_suggests_drilling():
    suggestion = only_suggestion(make_feature(subtype="through"))

    assert suggestion.operation_type == DRILLING
    assert suggestion.rule_id == "through_hole_to_drilling"
    assert "through-hole" in suggestion.reason


def test_a_blind_hole_suggests_drilling():
    suggestion = only_suggestion(make_feature(subtype="blind"))

    assert suggestion.operation_type == DRILLING
    assert suggestion.rule_id == "blind_hole_to_drilling"
    assert "closed end" in suggestion.reason


def test_a_counterbored_hole_suggests_drilling_and_offers_milling():
    suggestion = only_suggestion(
        make_feature(
            subtype="counterbore",
            dimensions={"diameter_mm": 6.0, "counterbore_diameter_mm": 12.0},
        )
    )

    assert suggestion.operation_type == DRILLING
    assert MILLING in suggestion.alternatives


def test_a_countersunk_hole_still_suggests_drilling():
    suggestion = only_suggestion(
        make_feature(subtype="through_countersunk", dimensions={"diameter_mm": 5.0})
    )

    assert suggestion.operation_type == DRILLING
    assert any("countersunk" in line for line in suggestion.evidence)


def test_an_external_cylinder_suggests_turning():
    suggestion = only_suggestion(
        make_feature(
            feature_id="CYL-001",
            feature_type=FeatureType.CYLINDER,
            subtype="shaft",
            dimensions={"diameter_mm": 30.0, "length_mm": 80.0},
        )
    )

    assert suggestion.operation_type == TURNING
    assert suggestion.rule_id == "external_cylinder_to_turning"
    # The geometry cannot tell a turned bar from a milled boss, so the other
    # option is offered rather than the choice being made silently.
    assert MILLING in suggestion.alternatives


def test_an_internal_cylinder_suggests_drilling_but_only_at_medium():
    """No hole rule claimed it, so its end conditions were never established."""
    suggestion = only_suggestion(
        make_feature(
            feature_id="CYL-002",
            feature_type=FeatureType.CYLINDER,
            subtype="bore",
            confidence=Confidence.HIGH,
            dimensions={"diameter_mm": 20.0, "length_mm": 40.0},
        )
    )

    assert suggestion.operation_type == DRILLING
    assert suggestion.confidence is Confidence.MEDIUM
    assert suggestion.feature_confidence is Confidence.HIGH


@pytest.mark.parametrize(
    "feature_type, rule",
    [(FeatureType.POCKET, "pocket_to_milling"), (FeatureType.SLOT, "slot_to_milling")],
)
def test_a_pocket_or_slot_suggests_milling(feature_type, rule):
    suggestion = only_suggestion(
        make_feature(
            feature_id="PKT-001",
            feature_type=feature_type,
            subtype="blind",
            dimensions={"length_mm": 40.0, "width_mm": 20.0, "depth_mm": 5.0},
        )
    )

    assert suggestion.operation_type == MILLING
    assert suggestion.rule_id == rule


def test_a_significant_planar_face_suggests_milling_at_medium_confidence():
    """Geometry proves a flat exists; it cannot prove material must come off it."""
    suggestion = only_suggestion(
        make_feature(
            feature_id="PL-001",
            feature_type=FeatureType.PLANE,
            subtype=None,
            confidence=Confidence.HIGH,
            dimensions={"area_mm2": 4000.0},
        )
    )

    assert suggestion.operation_type == MILLING
    assert suggestion.confidence is Confidence.MEDIUM
    assert any("process decision, not a geometric fact" in line for line in suggestion.evidence)


def test_a_confirmed_fillet_suggests_finishing_milling():
    suggestion = only_suggestion(
        make_feature(
            feature_id="FIL-001",
            feature_type=FeatureType.FILLET,
            subtype="concave",
            confidence=Confidence.HIGH,
            dimensions={"radius_mm": 3.0},
        )
    )

    assert suggestion.operation_type == MILLING
    assert suggestion.confidence is Confidence.MEDIUM
    assert any("cutter radius" in line for line in suggestion.evidence)


# --- the rules: features that must NOT map to an operation --------------------


@pytest.mark.parametrize(
    "subtype, rule",
    [("ambiguous", "ambiguous_hole_declined"), ("enclosed", "enclosed_hole_declined")],
)
def test_a_hole_whose_ends_are_not_established_is_declined(subtype, rule):
    result = suggest_operations(
        result_for(make_feature(subtype=subtype, confidence=Confidence.LOW))
    )

    assert result.suggestions == []
    assert len(result.declined) == 1
    assert result.declined[0].rule_id == rule
    assert result.declined[0].feature_id == "H-001"


def test_an_unconfirmed_blend_is_declined():
    """A `medium` fillet is one whose opposite tangent neighbour was never found."""
    result = suggest_operations(
        result_for(
            make_feature(
                feature_id="FIL-002",
                feature_type=FeatureType.FILLET,
                subtype="concave",
                confidence=Confidence.MEDIUM,
                dimensions={"radius_mm": 2.0},
            )
        )
    )

    assert result.suggestions == []
    assert result.declined[0].rule_id == "unconfirmed_fillet_declined"


def test_a_minor_planar_face_is_declined_against_the_largest_on_its_solid():
    big = make_feature(
        feature_id="PL-001",
        feature_type=FeatureType.PLANE,
        subtype=None,
        dimensions={"area_mm2": 1000.0},
    )
    small = make_feature(
        feature_id="PL-002",
        feature_type=FeatureType.PLANE,
        subtype=None,
        dimensions={"area_mm2": 1000.0 * MIN_PLANE_AREA_SHARE / 2},
    )

    result = suggest_operations(result_for(big, small))

    assert [s.feature_id for s in result.suggestions] == ["PL-001"]
    assert [d.feature_id for d in result.declined] == ["PL-002"]
    assert result.declined[0].rule_id == "minor_plane_declined"


def test_an_unknown_subtype_is_declined_rather_than_guessed():
    result = suggest_operations(result_for(make_feature(subtype="something_new")))

    assert result.suggestions == []
    assert result.declined[0].rule_id == "unknown_hole_subtype_declined"


def test_a_plane_with_no_measured_area_is_declined():
    result = suggest_operations(
        result_for(
            make_feature(
                feature_id="PL-001", feature_type=FeatureType.PLANE, subtype=None, dimensions={}
            )
        )
    )

    assert result.suggestions == []
    assert result.declined[0].rule_id == "plane_without_area_declined"


def test_every_declined_feature_carries_a_reason():
    features = [
        make_feature(feature_id="H-001", subtype="ambiguous", confidence=Confidence.LOW),
        make_feature(feature_id="H-002", subtype="enclosed", confidence=Confidence.LOW),
        make_feature(feature_id="H-003", subtype="mystery"),
    ]

    result = suggest_operations(result_for(*features))

    assert len(result.declined) == 3
    for declined in result.declined:
        assert declined.reason.strip()
        assert "no operation is suggested" in declined.reason.lower()


# --- linkage, confidence and source ------------------------------------------


def test_every_suggestion_points_back_at_the_feature_that_caused_it():
    """The design principle: no opaque list. Every row traces to a CAD feature."""
    result = suggestions_for_step("plate_with_four_holes")

    assert result.suggestions
    known = {feature.feature_id for feature in result.features}
    for suggestion in result.suggestions:
        assert suggestion.feature_id in known
        assert suggestion.feature_label
        assert suggestion.reason
        assert suggestion.evidence
        assert suggestion.rule_id
        assert suggestion.feature_detection_method


def test_a_suggestion_carries_the_cad_location_for_the_viewer():
    result = suggestions_for_step("plate_with_four_holes")
    drilling = [s for s in result.suggestions if s.operation_type == DRILLING]

    assert drilling
    for suggestion in drilling:
        assert suggestion.source_faces  # what a viewer would highlight
        assert any("CAD face(s)" in line for line in suggestion.evidence)


@pytest.mark.parametrize(
    "feature_confidence, expected",
    [
        (Confidence.HIGH, Confidence.HIGH),
        (Confidence.MEDIUM, Confidence.MEDIUM),
        (Confidence.LOW, Confidence.LOW),
    ],
)
def test_confidence_propagates_from_the_feature(feature_confidence, expected):
    suggestion = only_suggestion(make_feature(subtype="through", confidence=feature_confidence))

    assert suggestion.confidence is expected
    assert suggestion.feature_confidence is feature_confidence


def test_a_suggestion_is_never_more_confident_than_its_feature():
    """The rule ceiling can lower it; nothing may raise it."""
    for feature_type, subtype, dimensions in [
        (FeatureType.HOLE, "through", {"diameter_mm": 8.0}),
        (FeatureType.CYLINDER, "shaft", {"diameter_mm": 8.0}),
        (FeatureType.PLANE, None, {"area_mm2": 500.0}),
        (FeatureType.POCKET, "blind", {"depth_mm": 3.0}),
    ]:
        for confidence in Confidence:
            feature = make_feature(
                feature_type=feature_type,
                subtype=subtype,
                dimensions=dimensions,
                confidence=confidence,
            )
            for suggestion in suggest_operations(result_for(feature)).suggestions:
                assert suggestion.confidence.value in {c.value for c in Confidence}
                assert _rank(suggestion.confidence) <= _rank(confidence)


def _rank(confidence: Confidence) -> int:
    return {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}[confidence]


def test_a_faceted_feature_is_capped_at_medium_however_confident_the_fit():
    """A fitted feature is weaker evidence than an exact surface, and says so."""
    brep = only_suggestion(make_feature(source_type="brep", confidence=Confidence.HIGH))
    faceted = only_suggestion(make_feature(source_type="faceted", confidence=Confidence.HIGH))

    assert brep.confidence is Confidence.HIGH
    assert faceted.confidence is Confidence.MEDIUM
    assert faceted.operation_type == brep.operation_type
    assert any("reconstructed by fitting to polygons" in line for line in faceted.evidence)


def test_source_type_propagates_into_the_suggestion():
    assert only_suggestion(make_feature(source_type="brep")).source_type == "brep"
    assert only_suggestion(make_feature(source_type="faceted")).source_type == "faceted"


# --- determinism -------------------------------------------------------------


def test_repeated_runs_produce_identical_suggestions():
    recognition = result_for(
        make_feature(feature_id="H-001", subtype="through"),
        make_feature(feature_id="H-002", subtype="blind"),
        make_feature(
            feature_id="CYL-001", feature_type=FeatureType.CYLINDER, subtype="shaft",
            dimensions={"diameter_mm": 20.0},
        ),
    )

    first = suggest_operations(recognition).model_dump()
    second = suggest_operations(recognition).model_dump()
    first.pop("elapsed_ms")
    second.pop("elapsed_ms")

    assert first == second


def test_the_endpoint_is_deterministic_on_a_real_model():
    data = step("plate_with_four_holes")

    def once():
        response = client.post(
            "/cad/operations/suggest",
            files={"file": ("plate.step", data, "application/octet-stream")},
        )
        assert response.status_code == 200
        payload = response.json()
        payload.pop("elapsed_ms")
        return payload

    assert once() == once()


def test_suggestion_ids_are_stable_and_sequential():
    result = suggestions_for_step("plate_with_four_holes")

    assert [s.suggestion_id for s in result.suggestions] == [
        f"SUG-{index:03d}" for index in range(1, len(result.suggestions) + 1)
    ]


# --- no invented times or rates ----------------------------------------------


def test_no_suggestion_carries_a_time_or_a_rate():
    result = suggestions_for_step("block_with_pocket")

    assert result.suggestions
    for suggestion in result.suggestions:
        payload = suggestion.model_dump()
        for field in payload:
            assert "time" not in field, field
            assert "rate" not in field or field == "priced_by_rate_set", field
        assert suggestion.costable is False
        assert suggestion.not_costable_reason.startswith(NOT_COSTED_REASON)
        assert set(suggestion.missing_for_costing) == {
            "setup_time_minutes",
            "cycle_time_minutes",
        }


def test_the_result_says_plainly_that_nothing_is_costed():
    result = suggestions_for_step("block_with_pocket")

    assert result.costable is False
    assert "No suggestion here is costed" in result.costing_note
    assert any("does not estimate machining time" in warning for warning in result.warnings)


def test_a_suggestion_cannot_become_a_priced_operation_without_a_cycle_time():
    """The gate. Without this, a suggestion could quietly turn into money."""
    suggestion = only_suggestion(make_feature())

    for setup, cycle in [(None, None), (10.0, None), (10.0, 0.0), (10.0, -1.0)]:
        with pytest.raises(ValueError, match="cycle time"):
            to_operation_requirement(
                suggestion, setup_time_minutes=setup, cycle_time_minutes=cycle
            )


def test_a_suggestion_cannot_be_costed_without_a_setup_time_either():
    suggestion = only_suggestion(make_feature())

    with pytest.raises(ValueError, match="setup time"):
        to_operation_requirement(suggestion, setup_time_minutes=None, cycle_time_minutes=2.0)


def test_a_rejected_suggestion_cannot_be_costed_even_with_times():
    suggestion = record_user_decision(only_suggestion(make_feature()), UserAction.REJECTED)

    with pytest.raises(ValueError, match="rejected"):
        to_operation_requirement(suggestion, setup_time_minutes=5.0, cycle_time_minutes=2.0)


def test_with_times_supplied_a_suggestion_becomes_a_normal_requirement():
    suggestion = only_suggestion(make_feature())

    requirement = to_operation_requirement(
        suggestion, setup_time_minutes=15.0, cycle_time_minutes=2.5, quantity=10
    )

    assert requirement.process_type == DRILLING
    assert requirement.setup_time_minutes == 15.0
    assert requirement.cycle_time_minutes == 2.5
    assert requirement.quantity == 10
    # No rate anywhere: rates stay with the rate set, as for a hand-entered op.
    assert not hasattr(requirement, "machine_rate_per_hour")


def test_a_zero_setup_time_is_allowed_because_it_was_stated():
    suggestion = only_suggestion(make_feature())

    requirement = to_operation_requirement(
        suggestion, setup_time_minutes=0.0, cycle_time_minutes=1.0
    )

    assert requirement.setup_time_minutes == 0.0


# --- user review: accept, edit, remove, add ----------------------------------


def test_accepting_a_suggestion_records_the_action():
    accepted = record_user_decision(only_suggestion(make_feature()), UserAction.ACCEPTED)

    assert accepted.user_action is UserAction.ACCEPTED
    assert accepted.user_operation_type is None
    assert accepted.effective_operation_type == DRILLING


def test_editing_a_suggestion_never_overwrites_what_was_suggested():
    """The pair (suggested, chosen) is the entire value of a correction."""
    suggestion = only_suggestion(make_feature())

    edited = record_user_decision(suggestion, UserAction.EDITED, operation_type="milling")

    assert edited.operation_type == DRILLING  # the system's proposal survives
    assert edited.user_operation_type == MILLING  # the engineer's choice
    assert edited.user_action is UserAction.EDITED
    assert edited.effective_operation_type == MILLING


def test_an_edit_back_to_the_suggested_process_is_an_acceptance():
    """Recording it as a correction would poison a later learning step."""
    suggestion = only_suggestion(make_feature())

    same = record_user_decision(suggestion, UserAction.EDITED, operation_type="Drilling")

    assert same.user_action is UserAction.ACCEPTED
    assert same.user_operation_type is None


def test_an_edited_suggestion_is_costed_as_the_user_chose():
    suggestion = record_user_decision(
        only_suggestion(make_feature()), UserAction.EDITED, operation_type="milling"
    )

    requirement = to_operation_requirement(
        suggestion, setup_time_minutes=5.0, cycle_time_minutes=1.0
    )

    assert requirement.process_type == MILLING


def test_removing_a_suggestion_records_a_rejection_without_losing_it():
    rejected = record_user_decision(only_suggestion(make_feature()), UserAction.REJECTED)

    assert rejected.user_action is UserAction.REJECTED
    assert rejected.operation_type == DRILLING
    assert rejected.feature_id == "H-001"


def test_an_edit_needs_the_chosen_operation_type():
    with pytest.raises(ValueError, match="operation type"):
        record_user_decision(only_suggestion(make_feature()), UserAction.EDITED)


def test_a_manually_added_operation_is_representable_and_marked_as_the_users():
    """Nothing about the model prevents recording an operation nobody suggested."""
    manual = OperationSuggestion(
        suggestion_id="SUG-USER-1",
        feature_id="",
        feature_type="",
        feature_label="added by the estimator",
        feature_confidence=Confidence.LOW,
        feature_detection_method="none",
        solid_index=0,
        operation_type=MILLING,
        operation_name="Face the top",
        confidence=Confidence.LOW,
        source_type="brep",
        rule_id="user_added",
        reason="entered by the estimator, not suggested by any rule",
        origin=SuggestionOrigin.USER,
        user_action=UserAction.ADDED,
    )

    assert manual.origin is SuggestionOrigin.USER
    assert manual.user_action is UserAction.ADDED
    requirement = to_operation_requirement(
        manual, setup_time_minutes=10.0, cycle_time_minutes=3.0
    )
    assert requirement.process_type == MILLING


def test_the_review_fields_default_to_untouched():
    suggestion = only_suggestion(make_feature())

    assert suggestion.origin is SuggestionOrigin.SYSTEM
    assert suggestion.user_action is UserAction.PENDING
    assert suggestion.user_operation_type is None
    assert suggestion.editable is True


# --- rate sets ---------------------------------------------------------------


def test_a_process_the_rate_set_does_not_price_is_reported_not_hidden():
    """poc-machine-shop prices milling and drilling but not turning."""
    result = suggest_operations(
        result_for(
            make_feature(
                feature_id="CYL-001",
                feature_type=FeatureType.CYLINDER,
                subtype="shaft",
                dimensions={"diameter_mm": 20.0},
            )
        ),
        priced_processes=["milling", "drilling"],
    )

    suggestion = result.suggestions[0]
    assert suggestion.operation_type == TURNING
    assert suggestion.priced_by_rate_set is False
    assert "prices no rate for: turning" in " ".join(result.warnings)


def test_no_rate_set_means_no_claim_either_way():
    suggestion = only_suggestion(make_feature())

    assert suggestion.priced_by_rate_set is None


def test_the_rate_set_never_changes_what_is_suggested():
    recognition = result_for(
        make_feature(
            feature_id="CYL-001",
            feature_type=FeatureType.CYLINDER,
            subtype="shaft",
            dimensions={"diameter_mm": 20.0},
        )
    )

    without = suggest_operations(recognition)
    with_partial = suggest_operations(recognition, priced_processes=["milling"])

    assert [s.operation_type for s in without.suggestions] == [
        s.operation_type for s in with_partial.suggestions
    ]
    assert [s.confidence for s in without.suggestions] == [
        s.confidence for s in with_partial.suggestions
    ]


# --- the API -----------------------------------------------------------------


def test_the_endpoint_returns_features_suggestions_summary_and_warnings():
    response = client.post(
        "/cad/operations/suggest",
        files={"file": ("plate.step", step("plate_with_four_holes"), "application/octet-stream")},
    )

    assert response.status_code == 200
    payload = response.json()
    for key in ["features", "suggestions", "summary", "warnings", "declined"]:
        assert key in payload
    assert payload["costable"] is False
    assert payload["feature_count"] == len(payload["features"])


def test_the_endpoint_exposes_no_internal_topology_classes():
    """The response is the application vocabulary, not OCP objects."""
    response = client.post(
        "/cad/operations/suggest",
        files={"file": ("plate.step", step("plate_with_four_holes"), "application/octet-stream")},
    )

    body = response.text
    for leaked in ["TopoDS", "OCP.", "Bnd_Box", "TopAbs", "BRep_Tool", "gp_Pnt"]:
        assert leaked not in body


def test_the_endpoint_reports_which_processes_the_rate_set_prices():
    response = client.post(
        "/cad/operations/suggest",
        files={"file": ("plate.step", step("plate_with_four_holes"), "application/octet-stream")},
        data={"rate_set_id": "poc-default"},
    )

    payload = response.json()
    assert payload["rate_set_id"] == "poc-default"
    assert payload["rate_set_version"] == "1.0.0"
    assert set(payload["priced_processes"]) == {"milling", "drilling", "turning"}


def test_an_unknown_rate_set_is_a_404_not_a_silent_default():
    response = client.post(
        "/cad/operations/suggest",
        files={"file": ("plate.step", step("plate_with_four_holes"), "application/octet-stream")},
        data={"rate_set_id": "no-such-rate-set"},
    )

    assert response.status_code == 404


def test_an_stl_is_refused_with_an_explanation():
    """Suggestions come from features, and recognition is STEP-only."""
    import trimesh

    data = trimesh.creation.box(extents=(10.0, 10.0, 10.0)).export(file_type="stl")

    response = client.post(
        "/cad/operations/suggest",
        files={"file": ("box.stl", data, "application/octet-stream")},
    )

    assert response.status_code == 400
    assert "STEP" in response.json()["detail"]


# --- end to end on real recognizer output ------------------------------------


def test_a_plate_with_four_holes_suggests_four_drilling_operations():
    result = suggestions_for_step("plate_with_four_holes")

    drilling = [s for s in result.suggestions if s.operation_type == DRILLING]
    assert len(drilling) == 4
    assert all(s.confidence is Confidence.HIGH for s in drilling)
    assert all(s.feature_type == "hole" for s in drilling)


def test_a_blind_hole_model_suggests_drilling():
    result = suggestions_for_step("block_with_blind_hole")

    drilling = [s for s in result.suggestions if s.operation_type == DRILLING]
    assert len(drilling) == 1
    assert drilling[0].feature_subtype == "blind"


def test_a_pocket_model_suggests_milling_for_the_pocket():
    result = suggestions_for_step("block_with_pocket")

    pocket = [s for s in result.suggestions if s.feature_type == "pocket"]
    assert len(pocket) == 1
    assert pocket[0].operation_type == MILLING


def test_a_boss_model_suggests_turning_for_its_external_cylinder():
    result = suggestions_for_step("plate_with_boss_and_bores")

    turning = [s for s in result.suggestions if s.operation_type == TURNING]
    assert turning, [s.feature_label for s in result.suggestions]
    assert all(s.feature_subtype == "shaft" for s in turning)


def test_a_plain_block_suggests_only_facing_and_never_a_hole():
    result = suggestions_for_step("plain_block")

    assert {s.operation_type for s in result.suggestions} == {MILLING}
    assert all(s.feature_type == "plane" for s in result.suggestions)


def test_a_faceted_model_produces_suggestions_capped_at_medium():
    recognition = recognize_features_from_bytes(
        faceted_plate_with_four_holes(), "faceted_plate.step"
    )
    result = suggest_operations(recognition)

    assert result.representation == "faceted"
    assert result.suggestions
    assert all(s.source_type == "faceted" for s in result.suggestions)
    assert all(s.confidence is not Confidence.HIGH for s in result.suggestions)
    assert any("faceted export" in warning for warning in result.warnings)


def test_a_faceted_boss_still_suggests_turning():
    recognition = recognize_features_from_bytes(faceted_block_with_boss(), "faceted_boss.step")
    result = suggest_operations(recognition)

    turning = [s for s in result.suggestions if s.operation_type == TURNING]
    assert turning
    assert all(s.source_type == "faceted" for s in turning)
    assert all(s.confidence is Confidence.MEDIUM for s in turning)


# --- the quarantine: costing and rate sets are untouched ---------------------


def test_the_costing_path_cannot_reach_the_suggestion_layer():
    """Structural: no costing module may import this, in either direction."""
    from pathlib import Path

    directory = Path(app.__module__ and __import__(
        "cad_costing_agent.cad_mesh", fromlist=["__file__"]
    ).__file__).parent

    for module in [
        "costing.py",
        "operation_costing.py",
        "additional_costs.py",
        "part_cost.py",
        "cad_part_cost.py",
        "cad_mass.py",
        "cad_geometry.py",
        "rate_sets.py",
    ]:
        source = (directory / module).read_text(encoding="utf-8")
        assert "operation_suggestions" not in source, module


def test_the_suggestion_layer_imports_no_costing_arithmetic():
    from pathlib import Path

    import cad_costing_agent.operation_suggestions as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in [
        "from cad_costing_agent.costing",
        "from cad_costing_agent.operation_costing",
        "from cad_costing_agent.part_cost",
        "from cad_costing_agent.cad_part_cost",
        "calculate_part_cost",
        "calculate_operation_cost",
    ]:
        assert forbidden not in source, forbidden


def test_suggesting_operations_does_not_change_an_estimate():
    """Same file, same price, whether or not suggestions were requested."""
    data = step("plate_with_four_holes")
    form = {
        "material": "Steel",
        "source_unit": "mm",
        "part_id": "P-SUGGEST",
        "part_name": "Plate",
        "quantity": "10",
        "rate_set_id": "poc-default",
    }

    def price() -> float:
        response = client.post(
            "/cost/cad-part",
            files={"file": ("plate.step", data, "application/octet-stream")},
            data=form,
        )
        assert response.status_code == 200
        return response.json()["part_cost"]["total_part_cost"]

    before = price()
    suggested = client.post(
        "/cad/operations/suggest",
        files={"file": ("plate.step", data, "application/octet-stream")},
        data={"rate_set_id": "poc-default"},
    )
    assert suggested.status_code == 200
    assert suggested.json()["suggestions"]

    assert price() == before


def test_suggesting_operations_persists_nothing():
    before = client.get("/estimates").json()["total"]

    client.post(
        "/cad/operations/suggest",
        files={"file": ("plate.step", step("plate_with_four_holes"), "application/octet-stream")},
    )

    assert client.get("/estimates").json()["total"] == before


def test_the_rate_sets_are_not_modified_by_suggesting():
    before = client.get("/rates/poc-default").json()

    client.post(
        "/cad/operations/suggest",
        files={"file": ("plate.step", step("plate_with_four_holes"), "application/octet-stream")},
        data={"rate_set_id": "poc-default"},
    )

    assert client.get("/rates/poc-default").json() == before


def test_the_viewer_is_independent_of_suggestions():
    """A mesh is built the same whether or not operations were suggested."""
    data = step("plate_with_four_holes")

    def mesh():
        response = client.post(
            "/cad/mesh", files={"file": ("plate.step", data, "application/octet-stream")}
        )
        assert response.status_code == 200
        return response.json()

    before = mesh()
    client.post(
        "/cad/operations/suggest",
        files={"file": ("plate.step", data, "application/octet-stream")},
    )
    assert mesh() == before


def test_feature_recognition_itself_is_unchanged_by_this_step():
    """`/cad/features` must return exactly what it did before suggestions existed."""
    data = step("plate_with_four_holes")

    features = client.post(
        "/cad/features", files={"file": ("plate.step", data, "application/octet-stream")}
    ).json()
    suggested = client.post(
        "/cad/operations/suggest",
        files={"file": ("plate.step", data, "application/octet-stream")},
    ).json()

    assert suggested["features"] == features["features"]
    # No operation vocabulary leaked into the feature model.
    for feature in features["features"]:
        assert "operation" not in " ".join(feature.keys())


# --- the UI ------------------------------------------------------------------


def test_the_ui_shows_the_suggestions_as_an_editable_manufacturing_plan():
    """Step 20 moved the operations into the plan; the panel now explains them."""
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    assert "Recognized features" in markup
    assert "Manufacturing plan" in markup
    assert "Operation suggestions" in markup  # the why, beside the plan
    assert 'id="ops-body"' in markup
    assert 'src="plan.js"' in markup
    assert 'src="suggestions.js"' in markup
    # The plan is one table, not two: the review table is gone.
    assert 'id="suggestions-body"' not in markup


def test_the_ui_offers_accept_edit_and_remove_plus_manual_entry():
    plan = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    assert "use-est" in plan  # accept the system's estimate
    assert "op-process" in plan  # change the process
    assert "remove-op" in plan  # remove the operation
    assert 'id="add-op"' in markup  # and still add one by hand
    assert 'id="use-all-estimates"' in markup


def test_the_ui_never_prefills_an_estimated_time_into_a_committed_value():
    """The estimate may be shown as a placeholder, never as the input's value."""
    plan = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")
    cell = plan[plan.index("function timeCell") : plan.index("function sourceCell")]

    # The value is the user's entry or empty; the system's number is a hint only.
    assert 'const value = userValue != null ? String(userValue) : "";' in cell
    assert "placeholder=" in cell
    assert "est." in cell
    # Nothing writes the system estimate into `value`.
    assert "value=\"${escapeHtml(value)}\"" in cell
    assert "systemValue" in cell.split("const placeholder")[1].split("const value")[0]


def test_the_ui_excludes_untimed_operations_from_the_estimate():
    plan = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    # A row is costable only with a committed positive cycle time.
    assert "finalCycle(operation) > 0" in plan
    assert "ops-uncosted" in markup
    assert "not costed" in plan
    assert "Nothing is assumed." in plan


def test_the_ui_keeps_the_system_suggestion_when_the_user_edits():
    plan = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")

    assert "user_operation_type" in plan
    assert "was ${escapeHtml(operation.system_suggested_operation)}" in plan
    # Changing the process clears the acceptance of a time estimated for the old one.
    assert "operation.system_time_accepted = false;" in plan


def test_selecting_an_operation_points_at_the_feature_that_caused_it():
    plan = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")

    assert "feature-link" in plan
    assert "selectFeature" in plan


def test_the_viewer_can_mark_a_selected_feature():
    viewer = (STATIC_PATH / "viewer.js").read_text(encoding="utf-8")
    features = (STATIC_PATH / "features.js").read_text(encoding="utf-8")

    assert "markFeature" in viewer
    assert "clearMarker" in viewer
    assert "selectFeature" in features
    assert "CadViewer.markFeature" in features


def test_the_suggestions_panel_keeps_the_declined_features_and_the_why():
    """What is not a plan row stays here: the summary, the refusals, the caveats."""
    script = (STATIC_PATH / "suggestions.js").read_text(encoding="utf-8")

    assert "declined-list" in script
    assert "suggestions-summary" in script
    assert "buildPlanFromSuggestions" in script  # the operations go to the plan


def test_the_feature_marker_uses_the_backends_own_coordinates():
    """The marker points at a feature; it must not compute where that is.

    `test_cad_viewer.py` already asserts the viewer carries no costing
    arithmetic. What is new here is that the marker reads the backend's
    reported location and axis verbatim, with no scaling or re-derivation.
    """
    viewer = (STATIC_PATH / "viewer.js").read_text(encoding="utf-8")
    marker = viewer[viewer.index("function markFeature") :]
    marker = marker[: marker.index(chr(10) + "}")]

    assert "feature.location.x" in marker
    assert "feature.axis.x" in marker
    # No unit conversion or rescaling of the feature's own coordinates.
    for forbidden in ["* 1000", "/ 1000", "25.4", "convert", "toMillim"]:
        assert forbidden not in marker
