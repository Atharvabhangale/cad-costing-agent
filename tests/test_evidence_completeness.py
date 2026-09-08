"""Whether the recorded evidence is complete, and honesty about what never can be.

Step 29's readiness report counted every empty field as a gap. Measuring the real
database in Step 30 showed two of its three headline gaps were not gaps: all 449
records "missing" a component identity came from single-part estimates, where a
component identity does not exist, and 302 of 308 "missing" paired cycle times
were operations nobody ever finished — which Step 29 itself requires to stay
unpaired.

So these tests are almost entirely about the difference between four things that
an empty field could mean, and about the one gap that is real: the browser's plan
builder was dropping the recognizer's feature confidence on its way to the
server, and nothing stored can recover it afterwards.

Nothing here learns, fits, backfills a guess, or turns "unknown" into
"probably X".
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent import feedback_analytics as fa
from cad_costing_agent.feedback_analytics import (
    EVIDENCE_KNOWN,
    EVIDENCE_NOT_APPLICABLE,
    EVIDENCE_UNRECORDED,
    EVIDENCE_UNRECOVERABLE,
    NOT_PREDICTED,
    PAIRED,
    UNFINISHED,
    UNKNOWN,
)
from cad_costing_agent.main import app
from tests import assembly_fixtures
from tests.step_fixtures import block_with_through_hole
from tests.test_feedback_analytics import FakeEstimate, raw, record

client = TestClient(app)

STATIC = Path(__file__).resolve().parent.parent / "src" / "cad_costing_agent" / "static"
REAL_MODELS = Path(__file__).resolve().parent.parent / "cadmodels"

RATE_SET = "poc-machine-shop"


def field_of(records, name, minimum=8):
    entries = {e.field: e for e in fa.evidence_fields(records, minimum)}
    return entries[name]


# ===================================================== feature confidence


def test_the_recognizer_genuinely_exposes_a_feature_confidence():
    """Not invented for this step: `OperationSuggestion.feature_confidence` has
    existed since Step 19 and comes from how completely the geometry satisfied
    the recognizer's rule."""
    from cad_costing_agent.features import recognize_features_from_bytes
    from cad_costing_agent.operation_suggestions import suggest_operations

    result = suggest_operations(
        recognize_features_from_bytes(block_with_through_hole(), "hole.step")
    )

    assert result.suggestions
    for suggestion in result.suggestions:
        assert suggestion.feature_confidence.value in ("high", "medium", "low")


def test_build_plan_carries_the_feature_confidence_into_the_plan():
    from cad_costing_agent.features import recognize_features_from_bytes
    from cad_costing_agent.manufacturing_plan import build_plan
    from cad_costing_agent.operation_suggestions import suggest_operations

    suggestions = suggest_operations(
        recognize_features_from_bytes(block_with_through_hole(), "hole.step")
    ).suggestions
    plan = build_plan(suggestions, source_filename="hole.step")

    assert plan.operations
    for operation in plan.operations:
        assert operation.system_feature_confidence is not None
        assert operation.system_source_type in ("brep", "faceted")


def test_the_browser_plan_builder_carries_it_too():
    """The real capture hole Step 30 found: this table builds the plan payload
    in the browser rather than calling `build_plan`, so a field it does not copy
    never reaches the server — and all 713 stored records were missing feature
    confidence for exactly that reason."""
    source = (STATIC / "plan.js").read_text(encoding="utf-8")

    assert "system_feature_confidence: suggestion.feature_confidence," in source
    assert "system_source_type: suggestion.source_type," in source
    # The display-only duplicate is gone: two sources of truth for one fact.
    # Anchored, because `system_source_type:` contains `_source_type:`.
    assert not re.search(r"^\s*_source_type:", source, flags=re.M)


def test_a_persisted_feature_confidence_is_reported_as_known():
    estimate = FakeEstimate(
        plan_snapshot={"feedback": [raw(feature_confidence="high")]}
    )
    records, _ = fa.extract_records(estimate)

    assert records[0].feature_confidence == "high"
    assert fa._classify_feature_confidence(records[0]) == EVIDENCE_KNOWN
    assert field_of(records, "feature_confidence", minimum=1).known == 1


def test_a_confidence_recorded_only_on_the_plan_is_recovered_from_it():
    estimate = FakeEstimate(
        plan_snapshot={
            "feedback": [raw(feature_confidence=None)],
            "plan": {
                "operations": [
                    {"operation_id": "OP-001", "system_feature_confidence": "medium"}
                ]
            },
        }
    )
    records, _ = fa.extract_records(estimate)

    assert records[0].feature_confidence == "medium"
    assert fa._classify_feature_confidence(records[0]) == EVIDENCE_KNOWN


def test_an_absent_feature_confidence_is_unrecoverable_not_a_backlog():
    """Nothing stored can bring it back: the CAD file is not kept and no
    snapshot holds the recognizer's output."""
    estimate = FakeEstimate(plan_snapshot={"feedback": [raw(feature_confidence=None)]})
    records, _ = fa.extract_records(estimate)
    field = field_of(records, "feature_confidence", minimum=1)

    assert records[0].feature_confidence == UNKNOWN
    assert fa._classify_feature_confidence(records[0]) == EVIDENCE_UNRECOVERABLE
    assert field.unrecoverable == 1
    assert field.unrecorded == 0
    assert field.recoverable is False
    assert "nothing stored can recover them" in field.note
    assert "never filled with a guess" in field.note


def test_a_hand_added_operation_has_no_feature_and_so_no_confidence():
    """Not applicable, not missing. An operation nobody suggested has no
    feature for a recognizer to have been confident about."""
    entry = record(
        system_operation=None, feature_confidence=UNKNOWN, corrections=["added"]
    )

    assert fa._classify_feature_confidence(entry) == EVIDENCE_NOT_APPLICABLE
    assert field_of([entry], "feature_confidence", minimum=1).not_applicable == 1


def test_confidence_is_never_manufactured_from_acceptance_or_rule_performance():
    """The prohibition, asserted on the source: nothing in the classifier may
    read a correction count, an acceptance rate or a rule's performance."""
    import ast
    import inspect

    source = inspect.getsource(fa._classify_feature_confidence)
    tree = ast.parse(source.strip())
    body = tree.body[0].body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body.pop(0)
    code = ast.unparse(tree)

    for forbidden in ("corrections", "accept", "rule", "rate", "count", "was_corrected"):
        assert forbidden not in code, forbidden
    # It reads exactly two things.
    assert "feature_confidence" in code
    assert "system_operation" in code


def test_a_new_estimate_through_the_plan_path_persists_the_confidence():
    """End to end, on a real file: the recognizer's answer reaches the stored
    snapshot and the analytics report it as known."""
    estimate_id = cost_a_part()
    stored = client.get(f"/estimates/{estimate_id}").json()

    operations = stored["plan_snapshot"]["plan"]["operations"]
    assert operations
    assert all(o["system_feature_confidence"] for o in operations)

    feedback = stored["plan_snapshot"]["feedback"]
    assert all(row["feature_confidence"] for row in feedback)

    body = client.get("/feedback/evidence?minimum_sample=1").json()
    field = next(
        f for f in body["readiness"]["evidence"] if f["field"] == "feature_confidence"
    )
    assert field["known"] == len(feedback)
    assert field["unrecoverable"] == 0
    assert field["percent_known"] == pytest.approx(1.0)


# ===================================================== component identity


def test_an_assembly_record_carries_its_component_definition():
    estimate = FakeEstimate(
        source="cost/assembly",
        plan_snapshot={
            "component_feedback": {
                "0:1:1:2": [raw(component_id="0:1:1:2", component_name="HOUSING")]
            }
        },
    )
    records, _ = fa.extract_records(estimate)

    assert records[0].component_id == "0:1:1:2"
    assert records[0].component_identity_applicable is True
    assert fa._classify_component_identity(records[0]) == EVIDENCE_KNOWN


def test_a_definition_id_is_recovered_from_the_snapshot_key_deterministically():
    """The only backfill Step 30 performs, and it is provable: the map is keyed
    by component definition, so the identity is in the persisted structure
    rather than inferred."""
    estimate = FakeEstimate(
        plan_snapshot={"component_feedback": {"0:1:1:9": [raw(component_id=None)]}}
    )
    records, _ = fa.extract_records(estimate)

    assert records[0].component_id == "0:1:1:9"
    assert fa._classify_component_identity(records[0]) == EVIDENCE_KNOWN


def cost_an_assembly_with_plans():
    """A real assembly estimate carrying component plans, so operation feedback
    with a component identity is actually recorded."""
    data = assembly_fixtures.pump_assembly()
    built = client.post(
        "/cad/assembly/plans",
        files={"file": ("pump.step", data, "application/octet-stream")},
        data={"rate_set_id": RATE_SET},
    )
    assert built.status_code == 200, built.text

    plans = {}
    for entry in built.json()["plan"]["component_plans"]:
        plan = entry["plan"]
        if not plan or entry["component_name"] != "HOUSING":
            continue
        for operation in plan["operations"]:
            if operation["system_suggested_cycle_time"] is not None:
                operation["system_time_accepted"] = True
        for setup in plan["setups"]:
            if setup["system_setup_time_minutes"] is not None:
                setup["system_setup_time_accepted"] = True
        plans[entry["definition_id"]] = plan

    response = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", data, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "rate_set_id": RATE_SET,
            "component_plans": json.dumps(plans),
        },
    )
    assert response.status_code == 200, response.text
    return data


def test_a_definition_is_never_confused_with_an_occurrence():
    """An assembly places one definition many times. The feedback map is keyed
    by *definition*, so twelve bolts' corrections belong to one definition and
    not to one placement — and no occurrence id ever appears as an identity."""
    data = cost_an_assembly_with_plans()

    from cad_costing_agent.cad_import import decompose_assembly, detect_format

    adapter, _ = detect_format("pump.step", data)
    assembly = decompose_assembly(data, "pump.step", adapter)
    definitions = {d.definition_id for d in assembly.definitions}
    occurrences = {o.occurrence_id for o in assembly.occurrences}

    records = client.get("/feedback/records?limit=500").json()["items"]
    identities = {r["component_id"] for r in records if r["component_id"]}
    assert identities
    assert identities <= definitions
    assert not (identities & (occurrences - definitions))


def test_a_standalone_part_is_not_missing_a_component_identity():
    """The measured finding: all 449 records Step 29 reported as a gap came
    from single-part estimates, and a standalone part has no component."""
    estimate = FakeEstimate(plan_snapshot={"feedback": [raw(component_id=None)]})
    records, _ = fa.extract_records(estimate)
    field = field_of(records, "component_identity", minimum=1)

    assert records[0].component_identity_applicable is False
    assert fa._classify_component_identity(records[0]) == EVIDENCE_NOT_APPLICABLE
    assert field.not_applicable == 1
    assert field.applicable == 0
    assert field.percent_known is None
    assert "No record in this slice can carry this field" in field.note


def test_an_assembly_record_with_no_recoverable_identity_stays_unresolved():
    """Ambiguity is left unresolved rather than guessed. An empty snapshot key
    gives nothing to recover from, and the name is not used as a substitute."""
    entry = record(
        component_id=None, component_name="HOUSING", component_identity_applicable=True
    )

    assert fa._classify_component_identity(entry) == EVIDENCE_UNRECORDED
    field = field_of([entry], "component_identity", minimum=1)
    assert field.unrecorded == 1
    assert field.known == 0


def test_a_display_name_is_never_used_as_an_identity():
    """A stable identifier exists, so the name must not stand in for it."""
    import ast
    import inspect

    source = inspect.getsource(fa._classify_component_identity)
    tree = ast.parse(source.strip())
    body = tree.body[0].body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body.pop(0)
    code = ast.unparse(tree)

    assert "component_name" not in code
    assert "component_id" in code


def test_missing_fields_no_longer_counts_a_part_as_missing_a_component():
    part = [record() for _ in range(5)]
    report = fa.readiness(part, minimum=1)

    assert "component_identity" not in report.missing_fields


# ===================================================== paired cycle time


def test_an_accepted_prediction_is_a_paired_observation():
    """The engineer committed the system's own figure, which is a decision. The
    chosen time is not manufactured — the plan records the acceptance and
    resolves the committed time from it."""
    entry = record(system_cycle_time=0.6, user_cycle_time=0.6)

    assert entry.time_pairing == PAIRED
    assert fa._classify_paired_time(entry) == EVIDENCE_KNOWN


def test_a_corrected_time_is_a_paired_observation():
    entry = record(system_cycle_time=0.6, user_cycle_time=1.2)

    assert entry.time_pairing == PAIRED
    assert fa._classify_paired_time(entry) == EVIDENCE_KNOWN


def test_an_unfinished_operation_is_not_paired_and_not_a_gap():
    """302 of the 308 Step 29 reported as missing. Step 29 requires them out of
    every time statistic, so their absence is correct."""
    entry = record(system_cycle_time=0.6, user_cycle_time=None, corrections=[])

    assert entry.time_pairing == UNFINISHED
    assert fa._classify_paired_time(entry) == EVIDENCE_NOT_APPLICABLE
    field = field_of([entry], "paired_cycle_time", minimum=1)
    assert field.not_applicable == 1
    assert field.unrecorded == 0
    assert field.unrecoverable == 0


def test_an_operation_with_no_prediction_is_not_paired():
    entry = record(system_cycle_time=None, user_cycle_time=5.0)

    assert entry.time_pairing == NOT_PREDICTED
    assert fa._classify_paired_time(entry) == EVIDENCE_NOT_APPLICABLE


def test_the_prediction_is_never_overwritten_by_the_correction():
    estimate_id = cost_a_part(cycle=9.5)
    stored = client.get(f"/estimates/{estimate_id}").json()
    feedback = stored["plan_snapshot"]["feedback"]

    corrected = next(r for r in feedback if r["final_cycle_time"] == pytest.approx(9.5))
    assert corrected["suggested_cycle_time"] is not None
    assert corrected["suggested_cycle_time"] != corrected["final_cycle_time"]
    assert corrected["cycle_time_changed"] is True

    # And both survive into the normalized record, separately.
    records = client.get("/feedback/records?limit=500").json()["items"]
    mine = next(
        r
        for r in records
        if r["estimate_id"] == estimate_id and r["user_cycle_time"] == pytest.approx(9.5)
    )
    assert mine["system_cycle_time"] != mine["user_cycle_time"]


def test_the_near_zero_handling_is_unchanged_from_step_29():
    records = [record(system_cycle_time=0.0008, user_cycle_time=0.5) for _ in range(5)] + [
        record(system_cycle_time=2.0, user_cycle_time=3.0) for _ in range(5)
    ]
    stats = fa.time_stats(records, 8)

    assert stats.near_zero_excluded == 5
    assert stats.samples == 10
    assert fa.RELATIVE_ERROR_FLOOR == 0.05
    # And all ten are still paired evidence.
    assert field_of(records, "paired_cycle_time", minimum=1).known == 10


def test_a_time_is_never_inferred_from_a_cost_or_a_rate():
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

    for forbidden in ("total_part_cost", "per_unit_cost", "machine_rate", "labor_rate"):
        assert forbidden not in code, forbidden


# ===================================================== evidence completeness


def test_the_four_states_are_exhaustive():
    """Every record lands in exactly one, and they sum to the total. That is
    what makes the report checkable."""
    records = (
        [record() for _ in range(4)]
        + [record(feature_confidence=UNKNOWN) for _ in range(3)]
        + [record(system_operation=None, feature_confidence=UNKNOWN) for _ in range(2)]
    )
    for field in fa.evidence_fields(records, 1):
        total = (
            field.known + field.not_applicable + field.unrecorded + field.unrecoverable
        )
        assert total == field.total == len(records), field.field
        assert field.applicable == field.total - field.not_applicable


def test_the_percentage_divides_by_what_could_exist():
    """Not by every record. Dividing by all of them is how Step 29 came to
    describe 449 single-part records as a component-identity gap."""
    records = [record(feature_confidence="high") for _ in range(6)] + [
        record(system_operation=None, feature_confidence=UNKNOWN) for _ in range(4)
    ]
    field = field_of(records, "feature_confidence", minimum=1)

    assert field.total == 10
    assert field.not_applicable == 4
    assert field.applicable == 6
    assert field.known == 6
    assert field.percent_known == pytest.approx(1.0)  # not 0.6


def test_every_field_states_its_denominator_and_its_source_of_truth():
    for field in fa.evidence_fields([record()], 1):
        assert field.basis, field.field
        assert field.source_of_truth, field.field
        assert field.label, field.field


def test_the_sample_guard_survives_on_every_evidence_field():
    """Step 29's guard is not removed, and it is judged against the known count
    — a field known on four records is not actionable however many records the
    field does not apply to."""
    records = [record() for _ in range(4)]
    for field in fa.evidence_fields(records, 8):
        assert field.guard.minimum_required == 8
        if field.applicable:
            assert field.guard.sample_count == field.known
            if field.known < 8:
                assert field.guard.is_actionable is False
                assert "not enough evidence for a reliable pattern" in field.note


def test_recoverable_and_permanent_gaps_are_counted_apart():
    records = [
        record(feature_confidence=UNKNOWN),  # unrecoverable
        record(
            rule_id=None, feature_confidence="high"
        ),  # unrecorded, the plan could still name it
    ]
    report = fa.readiness(records, minimum=1)
    by_field = {entry.field: entry for entry in report.evidence}

    # The property, rather than the coincidence that the two totals differ:
    # each count is attributable to a different field, and a field never
    # contributes to both.
    assert by_field["feature_confidence"].unrecoverable == 1
    assert by_field["feature_confidence"].unrecorded == 0
    assert by_field["rule_id"].unrecorded == 1
    assert by_field["rule_id"].unrecoverable == 0

    assert report.permanently_unknown == sum(
        entry.unrecoverable for entry in report.evidence
    )
    assert report.safe_backfill_available == sum(
        entry.unrecorded for entry in report.evidence
    )


def test_more_evidence_is_never_reported_as_a_model_being_ready():
    report = fa.readiness([record() for _ in range(1000)], minimum=8)

    assert "evidence-quality report, not a training system" in report.note
    assert "more evidence existing is not the same as a model being ready" in report.note
    assert "no model has been trained or is being prepared" in report.note


def test_readiness_never_converts_an_unknown_into_a_guess():
    """The core prohibition, asserted behaviourally: a record with an unknown
    field stays unknown however many neighbours have a value."""
    records = [record(feature_confidence="high") for _ in range(99)] + [
        record(feature_confidence=UNKNOWN)
    ]
    field = field_of(records, "feature_confidence", minimum=1)

    assert field.known == 99
    assert field.unrecoverable == 1
    assert field.percent_known == pytest.approx(0.99)
    assert records[-1].feature_confidence == UNKNOWN


# ===================================================== the endpoint


def cost_a_part(*, quantity=5, cycle=3.0, process="drilling"):
    """A real estimate through the plan path, exactly as Step 29's tests do."""
    from cad_costing_agent.manufacturing_plan import build_plan
    from cad_costing_agent.operation_suggestions import OperationSuggestionResult

    suggestions = client.post(
        "/cad/operations/suggest",
        files={"file": ("hole.step", block_with_through_hole(), "application/octet-stream")},
        data={"rate_set_id": RATE_SET},
    ).json()
    built = build_plan(
        OperationSuggestionResult.model_validate(suggestions).suggestions,
        source_filename="hole.step",
    )
    payload = json.loads(built.model_dump_json())
    for operation in payload["operations"]:
        if operation["system_suggested_cycle_time"] is not None:
            operation["system_time_accepted"] = True
    for setup in payload["setups"]:
        if setup["system_setup_time_minutes"] is not None:
            setup["system_setup_time_accepted"] = True
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


def test_the_evidence_endpoint_reports_every_field():
    cost_a_part()
    body = client.get("/feedback/evidence").json()

    fields = {f["field"] for f in body["readiness"]["evidence"]}
    assert fields == {
        "feature_confidence",
        "component_identity",
        "paired_cycle_time",
        "rule_id",
        "source_type",
        "time_profile_version",
    }
    assert body["disclaimer"] == fa.LEARNING_DISCLAIMER
    assert body["minimum_sample"] >= 1
    assert body["window"]["estimates_read"] >= 1


def test_the_evidence_endpoint_is_read_only():
    from cad_costing_agent.main import app as application

    route = next(
        r for r in application.routes if getattr(r, "path", "") == "/feedback/evidence"
    )
    assert set(route.methods) <= {"GET", "HEAD"}


def test_the_evidence_endpoint_honours_the_filters():
    cost_a_part()
    body = client.get("/feedback/evidence?source_type=brep&minimum_sample=2").json()

    assert body["filters"]["source_type"] == "brep"
    assert body["minimum_sample"] == 2
    assert body["readiness"]["total_records"] > 0

    empty = client.get("/feedback/evidence?component=NOTHING").json()
    assert empty["readiness"]["total_records"] == 0
    for field in empty["readiness"]["evidence"]:
        assert field["percent_known"] is None


def test_running_the_evidence_report_changes_nothing():
    cost_a_part()
    before = client.get("/estimates?limit=100").json()
    stored = [
        client.get(f"/estimates/{item['estimate_id']}").json()
        for item in before["items"]
    ]
    rates = client.get("/rates").json()
    profiles = client.get("/time-profiles").json()

    for _ in range(3):
        assert client.get("/feedback/evidence").status_code == 200

    assert client.get("/estimates?limit=100").json() == before
    assert [
        client.get(f"/estimates/{item['estimate_id']}").json() for item in before["items"]
    ] == stored
    assert client.get("/rates").json() == rates
    assert client.get("/time-profiles").json() == profiles


def test_publishing_a_rate_set_or_time_profile_cannot_alter_the_evidence_report():
    """Step 29's snapshot-integrity guarantee, asserted for the new report too."""
    cost_a_part()
    before = client.get("/feedback/evidence").json()

    published = client.post(
        f"/rates/{RATE_SET}/versions",
        json={
            "version": "95.0.0",
            "name": "Step 30 probe",
            "description": "Published to prove the evidence report cannot move.",
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

    existing = client.get("/time-profiles").json()[0]
    detail = client.get(f"/time-profiles/{existing['time_profile_id']}").json()
    bumped = {
        key: value
        for key, value in detail.items()
        if key not in ("time_profile_id", "created_at")
    }
    bumped["version"] = "95.0.0"
    bumped["name"] = "Step 30 probe"
    bumped["description"] = "Published to prove the evidence report cannot move."
    profile = client.post(
        f"/time-profiles/{existing['time_profile_id']}/versions", json=bumped
    )
    assert profile.status_code in (200, 201), profile.text

    assert client.get("/feedback/evidence").json() == before


def test_the_summary_still_matches_the_paginated_records():
    """Step 29's cross-check, re-asserted: the readiness total is the record
    count, and the correction counts agree."""
    cost_a_part()
    summary = client.get("/feedback/summary").json()
    page = client.get("/feedback/records?limit=500").json()

    assert page["total"] == summary["reviewed_operations"]
    assert summary["readiness"]["total_records"] == summary["reviewed_operations"]

    counted = {}
    for item in page["items"]:
        for correction in item["corrections"]:
            counted[correction] = counted.get(correction, 0) + 1
    if page["total"] <= 500:
        for name, count in counted.items():
            assert summary["correction_counts"][name] == count, name


def test_the_unfinished_exclusion_survives():
    """The Step 29 fix this step must not regress."""
    cost_a_part()
    summary = client.get("/feedback/summary").json()

    assert summary["acceptance"].get("basis")
    assert "never finished" in summary["acceptance"]["basis"]
    assert summary["acceptance"]["denominator"] <= summary["reviewed_operations"]
    assert (
        summary["acceptance"]["denominator"] + summary["not_finished"]
        == summary["reviewed_operations"]
    )


# ===================================================== the browser


def test_the_evidence_view_is_one_of_the_feedback_tabs():
    source = (STATIC / "feedback.js").read_text(encoding="utf-8")

    assert '["evidence", "Evidence"]' in source
    assert "evidence: renderEvidence," in source
    # Step 29's six sub-views are all still there.
    for view in (
        "summary",
        "operations",
        "times",
        "setups",
        "classifications",
        "confidence",
    ):
        assert f'["{view}"' in source


def test_the_browser_computes_no_evidence_arithmetic():
    """The server decides the counts, the percentages, the thresholds and the
    readiness. The browser formats them."""
    raw_source = (STATIC / "feedback.js").read_text(encoding="utf-8")
    source = re.sub(r"^\s*//.*$", "", raw_source, flags=re.M)

    for pattern in (
        r"\.known\s*=(?!=)",
        r"\.unrecoverable\s*=(?!=)",
        r"\.not_applicable\s*=(?!=)",
        r"\.applicable\s*=(?!=)",
        r"\.percent_known\s*=(?!=)",
        r"(?:known|applicable|unrecorded|unrecoverable|not_applicable)\s*[-+*/]",
        r"[-+*/]\s*(?:known|applicable|unrecorded|unrecoverable)\b",
    ):
        assert not re.search(pattern, source), pattern


def test_the_view_explains_not_applicable_and_permanent_separately():
    source = (STATIC / "feedback.js").read_text(encoding="utf-8")

    assert "Not applicable" in source
    assert "cannot exist for that" in source
    assert "Permanent" in source
    assert "nothing stored can recover it" in source
    assert "filled with" in source


def test_the_evidence_view_reads_only_fields_the_api_declares():
    from cad_costing_agent.feedback_analytics import DatasetReadiness, EvidenceField

    # Comments stripped: a field-name check that reads them matches the comment
    # explaining a name collision as though it were a collision. Seventh time a
    # structural grep in this project has matched its own explanation.
    source = re.sub(
        r"^\s*//.*$", "", (STATIC / "feedback.js").read_text(encoding="utf-8"), flags=re.M
    )
    for name, model in (("field", EvidenceField), ("readiness", DatasetReadiness)):
        declared = set(model.model_fields)
        for attribute in re.findall(rf"\b{name}\.([a-z_0-9]+)", source):
            assert attribute in declared, f"{name}.{attribute}"


def test_reset_still_clears_every_filter():
    """A Step 29 fix, re-asserted so the new view cannot regress it."""
    source = (STATIC / "feedback.js").read_text(encoding="utf-8")
    reset = source[source.index('$("feedback-reset")') :][:700]

    for field in ("f-component", "f-operation", "f-material", "f-profile", "f-minimum"):
        assert f'"{field}"' in reset, field
    assert "f-source" in reset
    assert "f-confidence" in reset
    assert "f-corrected" in reset


# ===================================================== nothing learns


def test_step_30_added_no_learning():
    """No model, no fitting, no automatic change to a rule, profile,
    classification or plan."""
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

    # Tokens that could only be code. "calibrat" and "significance" are
    # deliberately absent from this list: both appear in the module's own
    # user-facing notes *refusing* those claims, and a substring test would
    # match the sentence promising the thing is not done. Their semantics are
    # asserted directly by the calibration-note and sample-guard tests.
    for forbidden in (
        "train(",
        "fit(",
        "learn(",
        "predict(",
        "regress(",
        "coefficient",
        "p_value",
        "session",
        "Repository",
        "def save",
        "def update",
        "import numpy",
        "import sklearn",
        "import scipy",
    ):
        assert forbidden not in code, forbidden

    # And the refusals themselves are still stated, in the notes.
    assert "not evidence that the confidence values are calibrated" in fa.CALIBRATION_NOTE
    # Line-wrapped in the docstring, so the halves are checked apart.
    assert "not a significance" in fa.SampleGuard.__doc__
    assert "makes no distributional claims" in fa.SampleGuard.__doc__


def test_the_analytics_layer_still_imports_nothing_from_the_package():
    import ast

    tree = ast.parse(Path(fa.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not {name for name in imported if name.startswith("cad_costing_agent")}


# ===================================================== real data


@pytest.mark.skipif(
    not (REAL_MODELS / "model1_1.stp").is_file(), reason="the real part is not present"
)
def test_a_real_part_records_a_feature_confidence_on_every_operation():
    from cad_costing_agent.manufacturing_plan import build_plan
    from cad_costing_agent.operation_suggestions import OperationSuggestionResult

    data = (REAL_MODELS / "model1_1.stp").read_bytes()
    suggestions = client.post(
        "/cad/operations/suggest",
        files={"file": ("model1_1.stp", data, "application/octet-stream")},
        data={"rate_set_id": RATE_SET},
    ).json()
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

    body = client.get("/feedback/evidence?minimum_sample=5").json()
    field = next(
        f for f in body["readiness"]["evidence"] if f["field"] == "feature_confidence"
    )
    assert field["known"] >= 20
    assert field["unrecoverable"] == 0
    assert field["guard"]["is_actionable"] is True


def test_no_published_profile_is_not_a_lost_version():
    """Measured on the real database: all 205 records with a predicted time and
    no version had no profile id either — the plan ran under the built-in
    baseline, which has no published version. Reporting that as permanently
    unknown overstated the gap by 198 records."""
    baseline = record(
        system_cycle_time=0.6, time_profile_id=None, time_profile_version=None
    )

    assert fa._classify_time_profile_version(baseline) == EVIDENCE_NOT_APPLICABLE
    field = field_of([baseline], "time_profile_version", minimum=1)
    assert field.not_applicable == 1
    assert field.unrecoverable == 0
    assert "built-in baseline" in field.basis


def test_a_profile_id_with_no_version_is_a_genuine_loss():
    """The distinction the classifier exists to make. It does not occur in the
    current history, and would be reported honestly if it did."""
    lost = record(
        system_cycle_time=0.6, time_profile_id="pune-shop-time", time_profile_version=None
    )

    assert fa._classify_time_profile_version(lost) == EVIDENCE_UNRECOVERABLE


def test_a_recorded_profile_version_is_known():
    known = record(
        system_cycle_time=0.6, time_profile_id="pune-shop-time", time_profile_version="1.0.0"
    )

    assert fa._classify_time_profile_version(known) == EVIDENCE_KNOWN


def test_an_operation_with_no_prediction_has_no_profile_version_to_lose():
    entry = record(system_cycle_time=None, time_profile_id=None, time_profile_version=None)

    assert fa._classify_time_profile_version(entry) == EVIDENCE_NOT_APPLICABLE
