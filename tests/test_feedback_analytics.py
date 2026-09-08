"""Reading the corrections back, without learning from them.

Two things are being defended here.

The first is **honesty about evidence**. A correction rate over four
observations is noise wearing a percentage sign, so every metric carries its
sample count and the threshold it was judged against, medians carry the headline
figures, outliers are reported rather than trimmed, and relative error refuses
to divide by a near-zero prediction. Most of these tests assert what the layer
*declines* to claim.

The second is that **nothing here learns**. Analytics is read-only by
construction: `feedback_analytics` imports nothing from the package at all, so
it cannot reach a rule, a profile or a repository to change one. That is
asserted structurally, and again behaviourally by running every report and
checking that every stored estimate, rate set and time profile is byte-identical
afterwards.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent import feedback_analytics as fa
from cad_costing_agent.feedback_analytics import (
    ACCEPTED,
    CLASSIFICATION_CHANGED,
    CYCLE_TIME_CHANGED,
    LEARNING_DISCLAIMER,
    OPERATION_CHANGED,
    OUTLIER_RATIO,
    REJECTED,
    RELATIVE_ERROR_FLOOR,
    SETUP_CHANGED,
    SETUP_TIME_CHANGED,
    UNKNOWN,
    FeedbackFilters,
    FeedbackRecord,
)
from cad_costing_agent.main import app
from tests import assembly_fixtures
from tests.step_fixtures import block_with_through_hole

client = TestClient(app)

STATIC = Path(__file__).resolve().parent.parent / "src" / "cad_costing_agent" / "static"
REAL_MODELS = Path(__file__).resolve().parent.parent / "cadmodels"

RATE_SET = "poc-machine-shop"


# --------------------------------------------------------------------- builders


def record(**overrides) -> FeedbackRecord:
    """A normalized record with plausible defaults, for pure statistics tests."""
    base = dict(
        estimate_id="E1",
        operation_id="OP-001",
        feature_id="H-001",
        system_operation="drilling",
        user_operation="drilling",
        system_cycle_time=1.0,
        user_cycle_time=1.0,
        rule_id="through_hole_to_drilling",
        source_type="brep",
        operation_confidence="high",
        time_confidence="high",
        feature_confidence="high",
        corrections=[ACCEPTED],
        costed=True,
    )
    base.update(overrides)
    return FeedbackRecord(**base)


def raw(**overrides) -> dict:
    """A persisted `OperationFeedback` row, as a snapshot holds it."""
    base = {
        "estimate_id": "E1",
        "operation_id": "OP-001",
        "feature_ids": ["H-001"],
        "feature_id": "H-001",
        "sequence_position": 1,
        "suggested_operation": "drilling",
        "final_operation": "drilling",
        "suggested_cycle_time": 0.6,
        "final_cycle_time": 0.6,
        "suggestion_confidence": "high",
        "time_confidence": "high",
        "rule_id": "through_hole_to_drilling",
        "source_type": "brep",
        "feature_confidence": "high",
        "user_action": "accepted",
        "origin": "system",
        "costed": True,
        "timestamp": "2026-09-01T10:00:00+00:00",
        "operation_changed": False,
        "cycle_time_changed": False,
        "setup_changed": False,
        "setup_time_corrected": False,
        "system_setup_id": "SETUP-001",
        "final_setup_id": "SETUP-001",
    }
    base.update(overrides)
    return base


class FakeEstimate:
    """The duck-typed shape `extract_records` reads. Nothing more is needed."""

    def __init__(self, **kwargs):
        self.estimate_id = kwargs.get("estimate_id", "E1")
        self.created_at = kwargs.get("created_at", datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.source = kwargs.get("source", "cost/cad-part")
        self.material = kwargs.get("material", "Steel")
        self.request_snapshot = kwargs.get("request_snapshot", {})
        self.result_snapshot = kwargs.get("result_snapshot", {})
        self.plan_snapshot = kwargs.get("plan_snapshot", {})
        self.rate_set_snapshot = kwargs.get("rate_set_snapshot", {})


# --------------------------------------------------------------------- reading


def test_a_part_estimates_flat_feedback_is_read():
    estimate = FakeEstimate(
        plan_snapshot={"feedback": [raw(), raw(operation_id="OP-002")]},
        request_snapshot={"material": "Steel", "rate_set_id": RATE_SET},
        rate_set_snapshot={"rate_set_id": RATE_SET, "version": "2.0.0"},
    )
    records, classifications = fa.extract_records(estimate)

    assert len(records) == 2
    assert classifications == []
    assert records[0].estimate_id == "E1"
    assert records[0].material == "Steel"
    assert records[0].rate_set_version == "2.0.0"
    assert records[0].source == "cost/cad-part"


def test_an_assembly_estimates_per_component_feedback_is_read():
    """Both plan-snapshot shapes, from the one source of truth. A second reader
    for the same corrections is exactly what this layer must not create."""
    estimate = FakeEstimate(
        source="cost/assembly",
        plan_snapshot={
            "component_feedback": {
                "0:1:1:2": [raw(component_id="0:1:1:2", component_name="HOUSING")],
                "0:1:1:5": [raw(operation_id="PIN/OP-001", component_name="PIN")],
            }
        },
        result_snapshot={
            "classification_feedback": [
                {
                    "definition_id": "0:1:1:4",
                    "component_name": "COVER",
                    "system_classification": "manufactured",
                    "user_classification": "purchased",
                    "final_classification": "purchased",
                    "classification_changed": True,
                    "quantity": 1,
                }
            ]
        },
    )
    records, classifications = fa.extract_records(estimate)

    assert {r.component_name for r in records} == {"HOUSING", "PIN"}
    assert len(classifications) == 1
    assert classifications[0].component_name == "COVER"
    assert classifications[0].changed is True


def test_a_component_id_is_filled_from_the_snapshot_key_when_the_row_lacks_one():
    """Records written before Step 28 carry no component id, but the assembly
    snapshot keys them by definition — so the identity is recoverable without
    inventing it."""
    estimate = FakeEstimate(
        plan_snapshot={"component_feedback": {"0:1:1:9": [raw(component_id=None)]}}
    )
    records, _ = fa.extract_records(estimate)

    assert records[0].component_id == "0:1:1:9"


def test_an_absent_field_is_unknown_rather_than_defaulted():
    """A default that reads like recorded data is the failure this layer exists
    to avoid."""
    estimate = FakeEstimate(
        plan_snapshot={
            "feedback": [
                {
                    "operation_id": "OP-001",
                    "sequence_position": 1,
                    "suggested_operation": "milling",
                }
            ]
        }
    )
    records, _ = fa.extract_records(estimate)
    entry = records[0]

    assert entry.operation_confidence == UNKNOWN
    assert entry.feature_confidence == UNKNOWN
    assert entry.time_confidence == UNKNOWN
    assert entry.source_type == UNKNOWN
    assert entry.rule_id is None
    assert entry.user_operation is None


def test_the_source_type_is_derived_from_the_viewer_linkage_when_absent():
    """Records written before `source_type` existed still carry the faces or
    facets the recognizer reported, which answers the same question. The
    derivation is flagged, not hidden."""
    brep = fa.normalize_operation_feedback(
        raw(source_type=None, source_faces=[3], source_facets=[])
    )
    faceted = fa.normalize_operation_feedback(
        raw(source_type=None, source_faces=[], source_facets=[1, 2, 3])
    )
    recorded = fa.normalize_operation_feedback(raw(source_type="brep"))

    assert (brep.source_type, brep.source_type_derived) == ("brep", True)
    assert (faceted.source_type, faceted.source_type_derived) == ("faceted", True)
    assert (recorded.source_type, recorded.source_type_derived) == ("brep", False)


def test_the_derivation_agrees_with_what_the_recognizer_actually_reports():
    """Proven against a real B-Rep and a real faceted model rather than
    assumed: the fallback is only defensible if it is right."""
    from tests.faceted_fixtures import faceted_plate_with_four_holes

    from cad_costing_agent.features import recognize_features_from_bytes
    from cad_costing_agent.operation_suggestions import suggest_operations

    for expected, data, name in (
        ("brep", block_with_through_hole(), "hole.step"),
        ("faceted", faceted_plate_with_four_holes(), "plate.step"),
    ):
        result = suggest_operations(recognize_features_from_bytes(data, name))
        for suggestion in result.suggestions:
            derived, _ = fa._source_type_of(
                {
                    "source_faces": list(suggestion.source_faces),
                    "source_facets": list(suggestion.source_facets),
                }
            )
            assert suggestion.source_type == expected
            assert derived == expected, (name, suggestion.suggestion_id)


# --------------------------------------------------------------------- corrections


def test_every_correction_type_is_recognized():
    cases = {
        OPERATION_CHANGED: raw(operation_changed=True, final_operation="turning"),
        SETUP_CHANGED: raw(setup_changed=True, final_setup_id="SETUP-002"),
        SETUP_TIME_CHANGED: raw(setup_time_corrected=True),
        CYCLE_TIME_CHANGED: raw(cycle_time_changed=True, final_cycle_time=1.2),
        ACCEPTED: raw(),
        REJECTED: raw(user_action="rejected"),
    }
    for expected, row in cases.items():
        assert expected in fa.normalize_operation_feedback(row).corrections, expected


def test_one_edit_that_moves_several_fields_is_not_counted_twice():
    """An engineer who changes a process almost always changes its time too.
    Counting that as two corrections would inflate every rate in the report, so
    each type is counted once against its own denominator."""
    entry = fa.normalize_operation_feedback(
        raw(
            operation_changed=True,
            cycle_time_changed=True,
            final_operation="turning",
            final_cycle_time=3.0,
        )
    )

    assert entry.corrections.count(OPERATION_CHANGED) == 1
    assert entry.corrections.count(CYCLE_TIME_CHANGED) == 1
    assert ACCEPTED not in entry.corrections


def test_an_operation_that_was_never_finished_is_neither_accepted_nor_corrected():
    """Only an operation that actually reached the cost counts as accepted. One
    still needing a time was not accepted; it was never finished."""
    entry = fa.normalize_operation_feedback(
        raw(costed=False, user_action="pending", final_cycle_time=None)
    )

    assert entry.corrections == []
    assert entry.was_corrected is False


def test_a_rejected_operation_counts_as_rejected_and_removed():
    entry = fa.normalize_operation_feedback(raw(user_action="rejected"))

    assert REJECTED in entry.corrections
    assert "removed" in entry.corrections
    assert ACCEPTED not in entry.corrections


def test_a_hand_added_operation_is_recorded_as_added():
    entry = fa.normalize_operation_feedback(
        raw(origin="user", user_action="added", suggested_operation=None)
    )

    assert "added" in entry.corrections


# --------------------------------------------------------------------- statistics


def test_acceptance_is_a_rate_with_its_own_numerator_and_denominator():
    records = [record() for _ in range(7)] + [
        record(corrections=[OPERATION_CHANGED], user_operation="turning") for _ in range(3)
    ]
    summary = fa.summarise(records, [], minimum=8)

    assert summary.acceptance.numerator == 7
    assert summary.acceptance.denominator == 10
    assert summary.acceptance.rate == pytest.approx(0.7)
    assert summary.acceptance.guard.sample_count == 10
    assert summary.acceptance.guard.is_actionable is True


def test_a_rate_with_no_denominator_is_null_rather_than_zero():
    """Zero would read as "nothing was accepted"; null reads as "nothing was
    reviewed", which is what it means."""
    summary = fa.summarise([], [], minimum=8)

    assert summary.acceptance.rate is None
    assert summary.acceptance.denominator == 0
    assert summary.reviewed_operations == 0


def test_medians_carry_the_headline_and_one_extreme_does_not_move_them():
    """One engineer typing 90 minutes where the system said 0.6 would otherwise
    define a shop's entire bias."""
    normal = [record(system_cycle_time=0.6, user_cycle_time=0.7) for _ in range(10)]
    with_outlier = normal + [record(system_cycle_time=0.6, user_cycle_time=90.0)]

    calm = fa.time_stats(normal, 8)
    skewed = fa.time_stats(with_outlier, 8)

    assert calm.user_median == pytest.approx(0.7)
    assert skewed.user_median == pytest.approx(0.7)  # unmoved
    assert skewed.user_mean > 8.0  # the mean is not
    assert skewed.outlier_count == 1


def test_relative_error_refuses_to_divide_by_a_near_zero_prediction():
    """A prediction of 0.0008 min corrected to 0.5 is a 62,400% error and says
    nothing about the estimator."""
    records = [record(system_cycle_time=0.0008, user_cycle_time=0.5) for _ in range(5)] + [
        record(system_cycle_time=2.0, user_cycle_time=3.0) for _ in range(5)
    ]
    stats = fa.time_stats(records, 8)

    assert stats.near_zero_excluded == 5
    assert stats.samples == 10  # still counted everywhere else
    assert stats.median_relative_error == pytest.approx(0.5)
    assert RELATIVE_ERROR_FLOOR == 0.05


def test_signed_error_says_which_way_the_system_is_wrong():
    under = [record(system_cycle_time=0.6, user_cycle_time=1.2) for _ in range(10)]
    over = [record(system_cycle_time=8.0, user_cycle_time=5.0) for _ in range(10)]

    assert fa.time_stats(under, 8).median_signed_error == pytest.approx(0.6)
    assert fa.time_stats(over, 8).median_signed_error == pytest.approx(-3.0)


def test_only_records_with_both_a_prediction_and_a_decision_enter_the_time_stats():
    """An operation the system never predicted a time for was entered, not
    corrected. Counting it would make the estimator look wrong far more often
    than it was."""
    records = [
        record(system_cycle_time=1.0, user_cycle_time=2.0),
        record(system_cycle_time=None, user_cycle_time=5.0),
        record(system_cycle_time=1.0, user_cycle_time=None),
    ]
    stats = fa.time_stats(records, 8)

    assert stats.samples == 1


# --------------------------------------------------------------------- bias


def test_a_directional_bias_is_claimed_only_above_the_threshold():
    few = [record(system_cycle_time=0.6, user_cycle_time=1.2) for _ in range(4)]
    many = [record(system_cycle_time=0.6, user_cycle_time=1.2) for _ in range(12)]

    small = fa.time_report(few, minimum=8).by_operation[0]
    large = fa.time_report(many, minimum=8).by_operation[0]

    assert small.direction == "insufficient_data"
    assert "not enough evidence for a reliable pattern" in small.statement
    assert large.direction == "under_estimates"
    assert "under-estimates" in large.statement


def test_an_over_estimating_process_is_named_as_such():
    records = [
        record(system_operation="milling", user_operation="milling",
               system_cycle_time=8.2, user_cycle_time=6.9)
        for _ in range(12)
    ]
    finding = fa.time_report(records, minimum=8).by_operation[0]

    assert finding.group == "milling"
    assert finding.direction == "over_estimates"
    assert finding.stats.median_ratio == pytest.approx(6.9 / 8.2, rel=1e-6)


def test_a_median_correction_smaller_than_the_floor_is_no_clear_bias():
    """Arithmetic noise on a two-decimal minute is not a direction."""
    records = [
        record(system_cycle_time=1.0, user_cycle_time=1.01) for _ in range(12)
    ]
    finding = fa.time_report(records, minimum=8).by_operation[0]

    assert finding.direction == "no_clear_bias"
    assert "no consistent direction" in finding.statement


def test_the_minimum_sample_is_configurable():
    records = [record(system_cycle_time=0.6, user_cycle_time=1.2) for _ in range(5)]

    assert fa.time_report(records, minimum=8).by_operation[0].direction == "insufficient_data"
    assert fa.time_report(records, minimum=3).by_operation[0].direction == "under_estimates"


# --------------------------------------------------------------------- confusion


def test_the_confusion_matrix_counts_system_against_engineer():
    records = (
        [record(system_operation="milling", user_operation="milling") for _ in range(32)]
        + [record(system_operation="milling", user_operation="turning") for _ in range(4)]
        + [record(system_operation="drilling", user_operation="drilling") for _ in range(51)]
        + [record(system_operation="drilling", user_operation="milling") for _ in range(3)]
    )
    report = fa.operation_report(records, minimum=8)
    cells = {(c.system_operation, c.user_operation): c for c in report.confusion}

    assert cells[("drilling", "drilling")].count == 51
    assert cells[("milling", "turning")].count == 4
    assert cells[("milling", "turning")].changed is True
    assert cells[("drilling", "drilling")].changed is False
    assert report.accuracy.numerator == 83
    assert report.accuracy.denominator == 90
    assert report.accuracy.rate == pytest.approx(83 / 90)


def test_a_confusion_cell_carries_examples_to_drill_into():
    records = [
        record(system_operation="milling", user_operation="turning", operation_id=f"OP-{i:03d}")
        for i in range(12)
    ]
    cell = fa.operation_report(records, minimum=8).confusion[0]

    assert cell.count == 12
    assert len(cell.example_operation_ids) == 5
    assert cell.example_operation_ids[0] == "OP-000"


def test_rule_performance_reports_suggestions_accepted_and_changed():
    records = (
        [record(rule_id="through_hole_to_drilling") for _ in range(86)]
        + [
            record(
                rule_id="through_hole_to_drilling",
                corrections=[OPERATION_CHANGED],
                user_operation="milling",
            )
            for _ in range(6)
        ]
        + [record(rule_id="shaft_to_turning") for _ in range(32)]
    )
    rules = {r.rule_id: r for r in fa.operation_report(records, minimum=8).rules}

    assert rules["through_hole_to_drilling"].suggestions == 92
    assert rules["through_hole_to_drilling"].accepted == 86
    assert rules["through_hole_to_drilling"].changed == 6
    assert rules["through_hole_to_drilling"].became == {"milling": 6}
    assert rules["shaft_to_turning"].suggestions == 32
    assert "unknown" not in rules


def test_a_record_with_no_rule_id_is_left_out_of_rule_performance():
    records = [record(rule_id=None) for _ in range(10)]

    assert fa.operation_report(records, minimum=8).rules == []


# --------------------------------------------------------------------- setups


def test_setup_corrections_are_reported_as_frequencies_not_errors():
    records = [record() for _ in range(72)] + [
        record(corrections=[SETUP_CHANGED], user_setup_id="SETUP-002") for _ in range(21)
    ]
    report = fa.setup_report(records, minimum=8)

    assert report.moved_between_setups.numerator == 21
    assert report.moved_between_setups.denominator == 93
    assert "correction frequencies, not error rates" in report.note
    assert "not the system being wrong" in report.note


def test_one_assumed_setup_becoming_several_is_counted_per_estimate():
    """"Split into more setups" is a property of a plan, not of an operation."""
    split = [
        record(estimate_id="E1", system_setup_id="SETUP-001", user_setup_id="SETUP-001"),
        record(estimate_id="E1", system_setup_id="SETUP-001", user_setup_id="SETUP-002"),
    ]
    kept = [
        record(estimate_id="E2", system_setup_id="SETUP-001", user_setup_id="SETUP-001"),
        record(estimate_id="E2", system_setup_id="SETUP-001", user_setup_id="SETUP-001"),
    ]
    report = fa.setup_report(split + kept, minimum=1)

    assert report.estimates_with_one_system_setup == 2
    assert report.estimates_split_into_more == 1
    assert report.split_rate.rate == pytest.approx(0.5)


def test_setup_time_changes_are_counted_separately_from_moves():
    records = [record(corrections=[SETUP_TIME_CHANGED]) for _ in range(9)]
    report = fa.setup_report(records, minimum=8)

    assert report.setup_time_changed.numerator == 9
    assert report.moved_between_setups.numerator == 0


# --------------------------------------------------------------------- confidence


def test_acceptance_is_grouped_by_confidence_band():
    records = (
        [record(operation_confidence="high") for _ in range(91)]
        + [
            record(operation_confidence="high", corrections=[OPERATION_CHANGED])
            for _ in range(9)
        ]
        + [record(operation_confidence="medium") for _ in range(74)]
        + [
            record(operation_confidence="medium", corrections=[OPERATION_CHANGED])
            for _ in range(26)
        ]
    )
    bands = {b.group: b for b in fa.confidence_report(records, minimum=8).by_operation_confidence}

    assert bands["high"].acceptance.rate == pytest.approx(0.91)
    assert bands["medium"].acceptance.rate == pytest.approx(0.74)
    assert list(bands) == ["high", "medium"]  # ordered high, medium, low, unknown


def test_the_confidence_report_refuses_to_claim_calibration():
    report = fa.confidence_report([record() for _ in range(10)], minimum=8)

    assert "not evidence that the confidence values are calibrated" in report.calibration_note
    assert "never a probability" in report.calibration_note
    assert report.disclaimer == LEARNING_DISCLAIMER


def test_brep_and_faceted_are_never_combined_silently():
    records = [record(source_type="brep") for _ in range(10)] + [
        record(source_type="faceted", corrections=[OPERATION_CHANGED]) for _ in range(10)
    ]
    groups = {g.group: g for g in fa.confidence_report(records, minimum=8).by_source_type}

    assert set(groups) == {"brep", "faceted"}
    assert groups["brep"].acceptance.rate == pytest.approx(1.0)
    assert groups["faceted"].acceptance.rate == pytest.approx(0.0)


def test_an_unknown_source_type_is_its_own_group():
    records = [record(source_type=UNKNOWN) for _ in range(5)]
    groups = {g.group: g for g in fa.confidence_report(records, minimum=8).by_source_type}

    assert UNKNOWN in groups
    assert groups[UNKNOWN].acceptance.guard.is_actionable is False


# --------------------------------------------------------------------- outliers


def test_outliers_are_listed_separately_and_still_counted():
    records = [record(system_cycle_time=0.6, user_cycle_time=0.7) for _ in range(10)] + [
        record(
            system_cycle_time=0.6,
            user_cycle_time=30.0,
            operation_id="OP-EXTREME",
            component_name="HOUSING",
        )
    ]
    report = fa.time_report(records, minimum=8)

    assert len(report.outliers) == 1
    assert report.outliers[0].operation_id == "OP-EXTREME"
    assert report.outliers[0].ratio == pytest.approx(50.0)
    assert report.overall.samples == 11  # not removed
    assert report.overall.outlier_count == 1
    assert f"{OUTLIER_RATIO:g}x" in report.outlier_note
    assert "deleting an order-of-magnitude miss" in report.outlier_note


def test_an_under_estimate_by_an_order_of_magnitude_is_also_an_outlier():
    records = [record(system_cycle_time=30.0, user_cycle_time=0.6) for _ in range(1)]
    report = fa.time_report(records, minimum=1)

    assert len(report.outliers) == 1
    assert report.outliers[0].ratio == pytest.approx(0.02)


# --------------------------------------------------------------------- classification


def test_classification_pairs_are_counted():
    entries = [
        fa.normalize_classification_feedback(
            {
                "definition_id": f"d{i}",
                "component_name": f"C{i}",
                "system_classification": "manufactured",
                "user_classification": "purchased",
                "final_classification": "purchased",
                "classification_changed": True,
            }
        )
        for i in range(8)
    ] + [
        fa.normalize_classification_feedback(
            {
                "definition_id": f"e{i}",
                "component_name": f"D{i}",
                "system_classification": "manufactured",
                "user_classification": "standard",
                "final_classification": "standard",
                "classification_changed": True,
            }
        )
        for i in range(3)
    ]
    report = fa.classification_report(entries, minimum=8)
    pairs = {(p.system_classification, p.user_classification): p for p in report.pairs}

    assert pairs[("manufactured", "purchased")].count == 8
    assert pairs[("manufactured", "standard")].count == 3
    assert report.changed.numerator == 11
    assert report.changed.denominator == 11
    assert len(pairs[("manufactured", "purchased")].examples) == 5


def test_the_classification_report_infers_no_truth_about_make_or_buy():
    report = fa.classification_report([], minimum=8)

    assert "No CAD format states make/buy" in report.note
    assert "not evidence about what the component truly is" in report.note


# --------------------------------------------------------------------- readiness


def test_dataset_readiness_counts_what_a_later_step_would_have():
    records = (
        [record() for _ in range(10)]
        + [record(feature_id=None, feature_ids=[]) for _ in range(3)]
        + [record(rule_id=None, source_type=UNKNOWN, feature_confidence=UNKNOWN) for _ in range(2)]
    )
    report = fa.readiness(records)

    assert report.total_records == 15
    assert report.with_feature_identity == 12
    assert report.with_rule_id == 13
    assert report.with_known_source_type == 13
    assert report.potential_training_rows == 12
    assert report.insufficient_rows == 3
    assert report.missing_fields["feature_identity"] == 3
    assert report.missing_fields["rule_id"] == 2
    assert "evidence-quality report, not a training system" in report.note
    assert "no model has been trained or is being prepared" in report.note
    # Step 30: an absence the field cannot have is not counted as a gap.
    assert "component_identity" not in report.missing_fields


def test_the_disclaimer_is_on_every_report():
    records = [record() for _ in range(5)]

    assert fa.summarise(records, [], minimum=8).disclaimer == LEARNING_DISCLAIMER
    assert fa.operation_report(records, minimum=8).disclaimer == LEARNING_DISCLAIMER
    assert fa.time_report(records, minimum=8).disclaimer == LEARNING_DISCLAIMER
    assert fa.confidence_report(records, minimum=8).disclaimer == LEARNING_DISCLAIMER
    assert "No model has been trained" in LEARNING_DISCLAIMER
    assert "analysis only" in LEARNING_DISCLAIMER


# --------------------------------------------------------------------- filtering


def test_every_documented_filter_selects():
    records = [
        record(component_name="HOUSING", user_operation="milling", source_type="brep",
               material="Steel", time_profile_version="1.0.0", rule_id="r1",
               feature_type="plane", rate_set_version="2.0.0", operation_confidence="high"),
        record(component_name="PIN", user_operation="turning", source_type="faceted",
               material="Aluminium", time_profile_version="2.0.0", rule_id="r2",
               feature_type="cylinder", rate_set_version="3.0.0", operation_confidence="low"),
    ]
    cases = {
        "component": ("HOUSING", 1),
        "operation_type": ("turning", 1),
        "feature_type": ("plane", 1),
        "material": ("Aluminium", 1),
        "source_type": ("brep", 1),
        "confidence": ("low", 1),
        "time_profile_version": ("2.0.0", 1),
        "rate_set_version": ("2.0.0", 1),
        "rule_id": ("r2", 1),
    }
    for field, (value, expected) in cases.items():
        filters = FeedbackFilters(**{field: value})
        kept = [r for r in records if fa.matches(r, filters)]
        assert len(kept) == expected, field


def test_filters_are_case_insensitive():
    records = [record(component_name="HOUSING")]

    assert fa.matches(records[0], FeedbackFilters(component="housing"))
    assert fa.matches(records[0], FeedbackFilters(component="Housing"))


def test_corrected_only_keeps_the_corrections():
    records = [record(), record(corrections=[OPERATION_CHANGED], user_operation="turning")]
    kept = [r for r in records if fa.matches(r, FeedbackFilters(corrected_only=True))]

    assert len(kept) == 1
    assert kept[0].was_corrected is True


def test_no_filter_keeps_everything():
    records = [record(), record(component_name=None, source_type=UNKNOWN)]

    assert all(fa.matches(r, FeedbackFilters()) for r in records)


# --------------------------------------------------------------------- determinism


def test_the_same_records_produce_the_same_report():
    records = [
        record(system_cycle_time=0.6 + i * 0.1, user_cycle_time=1.0 + i * 0.05) for i in range(20)
    ]

    first = fa.summarise(records, [], minimum=8).model_dump(mode="json")
    second = fa.summarise(records, [], minimum=8).model_dump(mode="json")
    assert first == second

    assert fa.time_report(records, minimum=8).model_dump(mode="json") == fa.time_report(
        records, minimum=8
    ).model_dump(mode="json")


def test_the_confusion_matrix_is_ordered_deterministically():
    records = (
        [record(system_operation="a", user_operation="b") for _ in range(3)]
        + [record(system_operation="c", user_operation="d") for _ in range(3)]
        + [record(system_operation="e", user_operation="f") for _ in range(9)]
    )
    order = [
        (c.system_operation, c.user_operation) for c in fa.operation_report(records, minimum=8).confusion
    ]

    assert order == [("e", "f"), ("a", "b"), ("c", "d")]


# --------------------------------------------------------------------- read-only


def _package_imports_of(module) -> set[str]:
    import ast

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return {name for name in imported if name.startswith("cad_costing_agent")}


def test_the_analytics_layer_imports_nothing_from_the_package():
    """The strongest possible read-only boundary: it cannot reach a rule, a
    profile, a rate set or a repository, so it cannot modify one."""
    assert _package_imports_of(fa) == set()


def test_nothing_in_the_package_treats_the_analytics_as_a_dependency():
    """Only `main.py` may serve it. A domain module importing it would be a
    path by which a report could influence a calculation."""
    import cad_costing_agent

    root = Path(cad_costing_agent.__file__).resolve().parent
    for path in root.rglob("*.py"):
        if path.name in ("main.py", "feedback_analytics.py"):
            continue
        assert "feedback_analytics" not in path.read_text(encoding="utf-8"), path.name


def test_the_analytics_layer_contains_no_write_operation():
    """Docstrings and comments are stripped first: this module explains at
    length that nothing here trains anything, and a grep for "train" otherwise
    matches the word "constraint" in the paragraph promising it."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(fa))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    code = ast.unparse(tree)

    for forbidden in (
        "def save",
        "def add",
        "def commit",
        "def update",
        "def delete",
        "session",
        "Session",
        "EstimateRow",
        "train(",
        "fit(",
        "learn(",
        "Repository",
    ):
        assert forbidden not in code, forbidden


def test_running_every_report_changes_nothing_that_was_stored():
    """The behavioural counterpart: ask every question, then check the whole
    database is byte-identical."""
    cost_a_part()
    before = {
        "estimates": client.get("/estimates?limit=100").json(),
        "rates": client.get("/rates").json(),
        "profiles": client.get("/time-profiles").json(),
    }
    stored_before = [
        client.get(f"/estimates/{item['estimate_id']}").json()
        for item in before["estimates"]["items"]
    ]

    for path in (
        "/feedback/summary",
        "/feedback/operations",
        "/feedback/times",
        "/feedback/setups",
        "/feedback/classifications",
        "/feedback/confidence",
        "/feedback/records",
    ):
        assert client.get(path).status_code == 200, path

    assert client.get("/estimates?limit=100").json() == before["estimates"]
    assert client.get("/rates").json() == before["rates"]
    assert client.get("/time-profiles").json() == before["profiles"]
    assert [
        client.get(f"/estimates/{item['estimate_id']}").json()
        for item in before["estimates"]["items"]
    ] == stored_before


def test_every_feedback_endpoint_is_a_get():
    """A reporting layer with a POST is a reporting layer somebody will write
    through."""
    from cad_costing_agent.main import app as application

    for route in application.routes:
        path = getattr(route, "path", "")
        if path.startswith("/feedback"):
            assert set(getattr(route, "methods", set())) <= {"GET", "HEAD"}, path


# --------------------------------------------------------------------- endpoints


def cost_a_part(*, quantity=5, cycle=3.0, process="drilling"):
    """A real estimate through the plan path, so real feedback is recorded."""
    suggestions = client.post(
        "/cad/operations/suggest",
        files={"file": ("hole.step", block_with_through_hole(), "application/octet-stream")},
        data={"rate_set_id": RATE_SET},
    ).json()

    plan = client.post(
        "/operations/plan-cost-preview",
        json={"suggestions": suggestions["suggestions"]},
    )
    # The preview endpoint's shape is not what we need; build the plan the way
    # the browser does instead — accept every estimate, then cost it.
    from cad_costing_agent.manufacturing_plan import build_plan
    from cad_costing_agent.operation_suggestions import OperationSuggestionResult

    parsed = OperationSuggestionResult.model_validate(suggestions)
    built = build_plan(parsed.suggestions, source_filename="hole.step")
    payload = json.loads(built.model_dump_json())
    for operation in payload["operations"]:
        if operation["system_suggested_cycle_time"] is not None:
            operation["system_time_accepted"] = True
    for setup in payload["setups"]:
        if setup["system_setup_time_minutes"] is not None:
            setup["system_setup_time_accepted"] = True
    # One deliberate correction, so there is something to report.
    payload["operations"][0]["user_cycle_time"] = cycle
    payload["operations"][0]["user_operation_type"] = process

    response = client.post(
        "/cost/cad-part",
        files={"file": ("hole.step", block_with_through_hole(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Bushing",
            "quantity": str(quantity),
            "rate_set_id": RATE_SET,
            "manufacturing_plan": json.dumps(payload),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["estimate_id"]


def test_the_summary_endpoint_reads_real_recorded_feedback():
    cost_a_part()
    body = client.get("/feedback/summary").json()

    assert body["reviewed_operations"] > 0
    assert body["window"]["estimates_read"] >= 1
    assert body["minimum_sample"] >= 1
    assert body["correction_counts"][CYCLE_TIME_CHANGED] >= 1
    assert body["disclaimer"] == LEARNING_DISCLAIMER
    assert body["readiness"]["total_records"] == body["reviewed_operations"]


def test_the_operations_endpoint_reports_a_real_confusion_cell_and_rule():
    cost_a_part(process="milling")
    body = client.get("/feedback/operations").json()

    changed = [c for c in body["confusion"] if c["changed"]]
    assert changed, body["confusion"]
    assert any(c["user_operation"] == "milling" for c in changed)
    assert body["rules"]
    assert body["rules"][0]["rule_id"]
    assert body["rules"][0]["suggestions"] > 0


def test_the_times_endpoint_reports_real_corrections():
    cost_a_part(cycle=9.0)
    body = client.get("/feedback/times").json()

    assert body["overall"]["samples"] > 0
    assert body["by_operation"]
    assert body["outlier_note"]


def test_the_setups_endpoint_reports_real_grouping_behaviour():
    cost_a_part()
    body = client.get("/feedback/setups").json()

    assert body["reviewed_operations"] > 0
    assert body["grouping_accepted"]["denominator"] > 0
    assert "not the system being wrong" in body["note"]


def test_the_classifications_endpoint_reads_assembly_feedback():
    from cad_costing_agent.cad_import import decompose_assembly, detect_format

    data = assembly_fixtures.pump_assembly()
    adapter, _ = detect_format("pump.step", data)
    ids = {d.name: d.definition_id for d in decompose_assembly(data, "pump.step", adapter).definitions}

    client.post(
        "/cost/assembly",
        files={"file": ("pump.step", data, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "rate_set_id": RATE_SET,
            "components": json.dumps(
                [
                    {
                        "definition_id": ids["COVER"],
                        "user_classification": "purchased",
                        "unit_cost": 250.0,
                        "currency": "INR",
                    }
                ]
            ),
        },
    )
    body = client.get("/feedback/classifications").json()

    assert body["reviewed"] >= 3
    pairs = {(p["system_classification"], p["user_classification"]): p for p in body["pairs"]}
    assert pairs[("manufactured", "purchased")]["count"] >= 1
    assert body["changed"]["numerator"] >= 1


def test_the_records_endpoint_paginates_and_drills_into_a_cell():
    cost_a_part(process="milling")
    everything = client.get("/feedback/records?limit=500").json()
    page = client.get("/feedback/records?limit=2&offset=0").json()

    assert page["total"] == everything["total"]
    assert len(page["items"]) <= 2
    assert page["limit"] == 2

    drill = client.get(
        "/feedback/records?system_operation=drilling&user_operation=milling"
    ).json()
    assert drill["total"] >= 1
    for item in drill["items"]:
        assert item["system_operation"] == "drilling"
        assert item["user_operation"] == "milling"


def test_a_filter_is_echoed_back_so_a_reader_knows_the_slice():
    cost_a_part()
    body = client.get("/feedback/summary?source_type=brep&material=Steel").json()

    assert body["filters"]["source_type"] == "brep"
    assert body["filters"]["material"] == "Steel"


def test_a_filter_that_matches_nothing_reports_no_data_rather_than_zero_rates():
    cost_a_part()
    body = client.get("/feedback/summary?component=NOTHING").json()

    assert body["reviewed_operations"] == 0
    assert body["acceptance"]["rate"] is None
    assert body["acceptance"]["guard"]["is_actionable"] is False


def test_the_window_says_how_much_history_it_read():
    cost_a_part()
    body = client.get("/feedback/summary?window=1").json()

    assert body["window"]["estimates_read"] == 1
    assert body["window"]["estimates_available"] >= 1
    assert body["window"]["note"]


def test_the_minimum_sample_can_be_overridden_per_request():
    cost_a_part()
    strict = client.get("/feedback/summary?minimum_sample=1000").json()
    lenient = client.get("/feedback/summary?minimum_sample=1").json()

    assert strict["acceptance"]["guard"]["is_actionable"] is False
    assert strict["minimum_sample"] == 1000
    assert lenient["acceptance"]["guard"]["is_actionable"] is True


def test_a_small_sample_is_labelled_on_a_real_response():
    cost_a_part()
    body = client.get("/feedback/summary?minimum_sample=1000").json()

    assert "not enough evidence for a reliable pattern" in body["acceptance"]["guard"]["note"]


# --------------------------------------------------------------------- history


def test_a_later_rate_set_version_does_not_change_historical_feedback():
    """The property every reporting layer in this project rests on: figures come
    from snapshots, so nothing published later can rewrite them."""
    cost_a_part()
    before = client.get("/feedback/summary").json()
    before_records = client.get("/feedback/records?limit=500").json()

    published = client.post(
        f"/rates/{RATE_SET}/versions",
        json={
            "version": "88.0.0",
            "name": "Much more expensive",
            "description": "Published after the feedback, to prove it changes nothing.",
            "is_illustrative": False,
            "currency": "INR",
            "materials": {
                "steel": {"name": "Steel", "cost_per_kg": 9999.0, "density_kg_m3": 7850.0}
            },
            "operations": {
                "drilling": {"machine_rate_per_hour": 9999.0, "labor_rate_per_hour": 9999.0},
                "milling": {"machine_rate_per_hour": 9999.0, "labor_rate_per_hour": 9999.0},
            },
            "additional_costs": {
                "overhead_rate_percent": 99.0,
                "scrap_rate_percent": 99.0,
                "tooling_cost_per_batch": 9999.0,
            },
        },
    )
    assert published.status_code in (200, 201), published.text

    assert client.get("/feedback/summary").json() == before
    assert client.get("/feedback/records?limit=500").json() == before_records


def test_a_historical_correction_keeps_what_the_system_said_then():
    estimate_id = cost_a_part(cycle=7.5, process="milling")
    records = client.get("/feedback/records?limit=500").json()["items"]
    mine = [r for r in records if r["estimate_id"] == estimate_id]

    corrected = next(r for r in mine if r["user_cycle_time"] == pytest.approx(7.5))
    assert corrected["system_operation"] == "drilling"
    assert corrected["user_operation"] == "milling"
    assert corrected["system_cycle_time"] is not None
    assert corrected["system_cycle_time"] != corrected["user_cycle_time"]
    assert corrected["rule_id"]
    assert corrected["source_type"] == "brep"
    assert corrected["operation_confidence"] in ("high", "medium", "low")


def test_a_new_estimate_does_not_alter_an_older_records_values():
    first = cost_a_part(cycle=4.0)
    before = [
        r
        for r in client.get("/feedback/records?limit=500").json()["items"]
        if r["estimate_id"] == first
    ]

    cost_a_part(cycle=40.0)

    after = [
        r
        for r in client.get("/feedback/records?limit=500").json()["items"]
        if r["estimate_id"] == first
    ]
    assert after == before


# --------------------------------------------------------------------- the browser


def test_the_feedback_page_is_served_and_linked():
    response = client.get("/ui/feedback.js")

    assert response.status_code == 200
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<script src="feedback.js"></script>' in html
    assert 'id="feedback-card"' in html or 'data-tab="feedback"' in html


def test_the_browser_performs_no_analytics_arithmetic():
    """Every figure on the page is one the server computed. A percentage
    invented in JavaScript beside a real one is indistinguishable from it — and
    this page is entirely percentages."""
    raw_source = (STATIC / "feedback.js").read_text(encoding="utf-8")
    # Comments stripped first. This file explains at length that it computes no
    # median, and the third time in this project a structural grep has matched
    # the paragraph promising the thing is absent.
    source = re.sub(r"^\s*//.*$", "", raw_source, flags=re.M)

    for forbidden in (
        "reduce(",
        "/ denominator",
        "* 100 /",
        "statistics",
        "a - b)",  # the sort-then-index median idiom
    ):
        assert forbidden not in source, forbidden
    for pattern in (
        r"\.rate\s*=(?!=)",
        r"\.system_median\s*=(?!=)",
        r"\.user_median\s*=(?!=)",
        r"\.median_ratio\s*=(?!=)",
        r"\.numerator\s*=(?!=)",
        r"\.denominator\s*=(?!=)",
        r"\.samples\s*=(?!=)",
        r"(?:numerator|denominator|samples|sample_count)\s*[-+*/]",
        r"[-+*/]\s*(?:numerator|denominator|sample_count)\b",
    ):
        assert not re.search(pattern, source), pattern


def test_the_page_shows_the_disclaimer_and_the_sample_guards():
    source = (STATIC / "feedback.js").read_text(encoding="utf-8")

    assert "disclaimer" in source
    assert "is_actionable" in source
    assert "minimum_required" in source
    assert "sample_count" in source


def test_the_page_reads_only_fields_the_api_declares():
    from cad_costing_agent.feedback_analytics import (
        CountedRate,
        FeedbackRecord as Record,
        TimeErrorStats,
    )

    # Comments stripped, for the same reason the arithmetic check strips them.
    source = re.sub(
        r"^\s*//.*$", "", (STATIC / "feedback.js").read_text(encoding="utf-8"), flags=re.M
    )
    for name, model in (
        ("rate", CountedRate),
        ("stats", TimeErrorStats),
        ("record", Record),
    ):
        declared = set(model.model_fields)
        for field in re.findall(rf"\b{name}\.([a-z_0-9]+)", source):
            assert field in declared, f"{name}.{field}"


# --------------------------------------------------------------------- real data


@pytest.mark.skipif(
    not (REAL_MODELS / "model1_1.stp").is_file(), reason="the real part is not present"
)
def test_a_real_part_produces_readable_feedback():
    data = (REAL_MODELS / "model1_1.stp").read_bytes()
    suggestions = client.post(
        "/cad/operations/suggest",
        files={"file": ("model1_1.stp", data, "application/octet-stream")},
        data={"rate_set_id": RATE_SET},
    ).json()

    from cad_costing_agent.manufacturing_plan import build_plan
    from cad_costing_agent.operation_suggestions import OperationSuggestionResult

    built = build_plan(
        OperationSuggestionResult.model_validate(suggestions).suggestions,
        source_filename="model1_1.stp",
    )
    payload = json.loads(built.model_dump_json())
    for operation in payload["operations"]:
        if operation["system_suggested_cycle_time"] is not None:
            operation["system_time_accepted"] = True
    for setup in payload["setups"]:
        if setup["system_setup_time_minutes"] is not None:
            setup["system_setup_time_accepted"] = True

    response = client.post(
        "/cost/cad-part",
        files={"file": ("model1_1.stp", data, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "M1",
            "part_name": "Bracket",
            "quantity": "10",
            "rate_set_id": RATE_SET,
            "manufacturing_plan": json.dumps(payload),
        },
    )
    assert response.status_code == 200, response.text

    body = client.get("/feedback/summary?minimum_sample=5").json()
    assert body["reviewed_operations"] >= 20
    assert body["acceptance"]["rate"] is not None
    operations = client.get("/feedback/operations?minimum_sample=5").json()
    assert operations["rules"]
    assert all(rule["rule_id"] for rule in operations["rules"])


def test_a_feedback_row_is_joined_to_the_plan_it_came_from():
    """Found in live verification: `rule_id`, `source_type` and `feature_type`
    were reported unknown for all 713 historical records, because the
    derivation read the *feedback row* — which never carried the viewer
    linkage. The plan in the same snapshot does, so joining by operation id
    recovers all three for every correction ever recorded."""
    estimate = FakeEstimate(
        plan_snapshot={
            "feedback": [raw(rule_id=None, source_type=None, operation_id="OP-001")],
            "plan": {
                "operations": [
                    {
                        "operation_id": "OP-001",
                        "system_suggestion_rule": "through_hole_to_drilling",
                        "feature_type": "hole",
                        "source_faces": [7],
                        "source_facets": [],
                    }
                ]
            },
        }
    )
    entry = fa.extract_records(estimate)[0][0]

    assert entry.rule_id == "through_hole_to_drilling"
    assert entry.feature_type == "hole"
    assert entry.source_type == "brep"
    assert entry.source_type_derived is True


def test_the_join_reaches_an_assemblys_per_component_plans_too():
    estimate = FakeEstimate(
        source="cost/assembly",
        plan_snapshot={
            "component_feedback": {
                "d1": [raw(rule_id=None, source_type=None, operation_id="HOUSING/OP-001")]
            },
            "assembly_plan": {
                "component_plans": [
                    {
                        "component_name": "HOUSING",
                        "plan": {
                            "operations": [
                                {
                                    "operation_id": "HOUSING/OP-001",
                                    "system_suggestion_rule": "significant_plane_to_milling",
                                    "source_facets": [1, 2, 3],
                                }
                            ]
                        },
                    }
                ]
            },
        },
    )
    entry = fa.extract_records(estimate)[0][0]

    assert entry.rule_id == "significant_plane_to_milling"
    assert entry.source_type == "faceted"


def test_the_join_never_overrides_what_the_feedback_row_recorded():
    """It fills absent fields only. A recorded fact outranks a reconstructed
    one, and the flag says which is which."""
    estimate = FakeEstimate(
        plan_snapshot={
            "feedback": [raw(rule_id="recorded_rule", source_type="faceted")],
            "plan": {
                "operations": [
                    {
                        "operation_id": "OP-001",
                        "system_suggestion_rule": "plan_rule",
                        "source_faces": [1],
                    }
                ]
            },
        }
    )
    entry = fa.extract_records(estimate)[0][0]

    assert entry.rule_id == "recorded_rule"
    assert entry.source_type == "faceted"
    assert entry.source_type_derived is False


def test_a_row_with_no_matching_plan_operation_stays_unknown():
    """No guessing: an operation the plan does not contain is left as it was
    recorded rather than matched to something adjacent."""
    estimate = FakeEstimate(
        plan_snapshot={
            "feedback": [raw(rule_id=None, source_type=None, operation_id="OP-999")],
            "plan": {"operations": [{"operation_id": "OP-001", "source_faces": [1]}]},
        }
    )
    entry = fa.extract_records(estimate)[0][0]

    assert entry.rule_id is None
    assert entry.source_type == UNKNOWN


def test_an_unfinished_operation_is_excluded_from_the_acceptance_denominator():
    """Found on this project's own history: 189 of 354 milling suggestions had
    no committed time — mostly because the rate set prices no rate for the
    process — so a rule with *one* correction read as 46% acceptance. Counting
    an unreviewed suggestion as a rejection says the opposite of what happened."""
    reviewed = [record() for _ in range(10)]
    unfinished = [
        record(corrections=[], costed=False, user_cycle_time=None) for _ in range(20)
    ]
    summary = fa.summarise(reviewed + unfinished, [], minimum=8)

    assert summary.reviewed_operations == 30
    assert summary.not_finished == 20
    assert summary.acceptance.numerator == 10
    assert summary.acceptance.denominator == 10  # not 30
    assert summary.acceptance.rate == pytest.approx(1.0)
    assert summary.acceptance.excluded == 20
    assert "never finished" in summary.acceptance.basis


def test_a_rule_with_one_correction_does_not_read_as_half_rejected():
    records = (
        [record(rule_id="significant_plane_to_milling") for _ in range(164)]
        + [
            record(
                rule_id="significant_plane_to_milling",
                corrections=[OPERATION_CHANGED],
                user_operation="drilling",
            )
        ]
        + [
            record(rule_id="significant_plane_to_milling", corrections=[], costed=False)
            for _ in range(189)
        ]
    )
    rule = fa.operation_report(records, minimum=8).rules[0]

    assert rule.suggestions == 354
    assert rule.accepted == 164
    assert rule.changed == 1
    assert rule.not_finished == 189
    assert rule.acceptance.denominator == 165
    assert rule.acceptance.rate == pytest.approx(164 / 165)


def test_a_grouped_acceptance_rate_uses_the_same_denominator():
    records = [record(source_type="brep") for _ in range(10)] + [
        record(source_type="brep", corrections=[], costed=False) for _ in range(5)
    ]
    group = fa.confidence_report(records, minimum=8).by_source_type[0]

    assert group.reviewed == 15
    assert group.not_finished == 5
    assert group.acceptance.denominator == 10
    assert group.acceptance.rate == pytest.approx(1.0)


def test_every_rate_states_the_population_it_describes():
    summary = fa.summarise([record() for _ in range(10)], [], minimum=8)

    assert summary.acceptance.basis
    assert "accepted, changed or rejected" in summary.acceptance.basis


def test_the_page_shows_what_the_acceptance_denominator_excluded():
    """An acceptance rate whose excluded population is invisible is the same
    trap in the browser as it was in the API: 91% over 411 resolved operations
    means something different from 91% over 713 reviewed ones."""
    source = (STATIC / "feedback.js").read_text(encoding="utf-8")

    assert "body.not_finished" in source
    assert "rule.not_finished" in source
    assert "group.not_finished" in source
    assert "rate.excluded" in source
    # The sentence is line-wrapped inside the template literal, so the test
    # asserts on a phrase that is not split rather than on the whole clause.
    assert "never finished" in source
    assert "rather than counted as rejections" in source


def test_reset_clears_the_sample_threshold_too():
    """Found in the browser: after raising the threshold to label everything as
    thin evidence, Reset left it in place — so every figure stayed dimmed with
    nothing on screen saying why."""
    source = (STATIC / "feedback.js").read_text(encoding="utf-8")
    reset = source[source.index('$("feedback-reset")') :][:600]

    assert '"f-minimum"' in reset


def test_the_filter_bar_wraps_rather_than_stacking():
    """`.row` stretches its children, so each filter took a whole line."""
    css = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert ".feedback-filters > * { flex: 0 0 auto; }" in css
    # The one that actually matters: `.row` is not a flex container, so
    # `flex-wrap` alone had nothing to wrap.
    assert "display: flex; flex-wrap: wrap" in css
