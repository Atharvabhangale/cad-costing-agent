"""The manufacturing plan, the feedback it produces, and what it lets be costed.

Three things are defended here, in ascending order of importance.

1. The plan is editable: processes, times, order, manual lines, removals.
2. **The system's proposal is never overwritten.** Every edit writes to the
   `user_*` side, so the pair *(predicted, chosen)* survives into the feedback
   record — which is the only reason a later learning step will have anything to
   learn from.
3. **An estimated time is not a committed one.** Generating an estimate does not
   adopt it, changing the process invalidates it, and an operation with no
   committed time is left out of the cost rather than defaulted. That gap is
   where a suggestion would quietly turn into money.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.features import recognize_features
from cad_costing_agent.features.model import Confidence
from cad_costing_agent.main import STATIC_PATH, app
from cad_costing_agent.manufacturing_plan import (
    NOT_COSTED_WITHOUT_TIME,
    ManufacturingPlan,
    PlannedOperation,
    TimeSource,
    build_plan,
    estimate_missing_times,
    manual_operation,
)
from cad_costing_agent.operation_feedback import feedback_from_plan
from cad_costing_agent.operation_suggestions import (
    SuggestionOrigin,
    UserAction,
    suggest_operations,
)
from cad_costing_agent.operation_time import TimeAssumptions, TimeStatus
from tests import step_fixtures

client = TestClient(app)


# --- helpers -----------------------------------------------------------------


def code_without_prose(module) -> str:
    """Module source with docstrings and comments stripped.

    A grep for a forbidden idea must not match the documentation that explains
    the idea is deliberately absent — `ast.unparse` drops comments outright and
    this drops the docstrings, leaving only what actually executes.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree).lower()


def plan_for(fixture: str = "plate_with_four_holes") -> ManufacturingPlan:
    data = getattr(step_fixtures, fixture)()
    recognition = recognize_features(read_step_shape(data), filename=f"{fixture}.step")
    return build_plan(
        suggest_operations(recognition).suggestions,
        units=recognition.units,
        source_filename=f"{fixture}.step",
    )


def drilling_operation(plan: ManufacturingPlan) -> PlannedOperation:
    return next(o for o in plan.ordered if o.system_suggested_operation == "drilling")


# --- building the plan -------------------------------------------------------


def test_a_plan_is_built_from_suggestions_with_a_time_for_each():
    plan = plan_for()

    assert plan.operations
    for operation in plan.operations:
        assert operation.origin is SuggestionOrigin.SYSTEM
        assert operation.system_suggested_operation
        assert operation.system_time_status is TimeStatus.ESTIMATED
        assert operation.system_suggested_cycle_time > 0


def test_estimating_a_time_does_not_commit_it():
    """The whole plan is costable-free until a person acts on it."""
    plan = plan_for()

    assert plan.to_operation_requirements(10) == []
    assert len(plan.uncosted_operations()) == len(plan.active)
    for operation in plan.operations:
        assert operation.system_time_accepted is False
        assert operation.time_source is TimeSource.NEEDS_INPUT
        assert operation.final_cycle_time is None
        assert operation.costable is False
        assert operation.not_costable_reason == NOT_COSTED_WITHOUT_TIME


def test_the_plan_starts_in_recognition_order_and_is_numbered_from_one():
    plan = plan_for()

    assert [o.sequence_position for o in plan.ordered] == list(range(1, len(plan.operations) + 1))


def test_feature_links_survive_into_the_plan():
    plan = plan_for()

    for operation in plan.operations:
        assert operation.feature_ids
        assert operation.feature_label
        # What the viewer would mark.
        assert operation.source_faces or operation.source_facets


# --- accepting ---------------------------------------------------------------


def test_accepting_the_estimate_commits_the_systems_time():
    plan = plan_for()
    operation = drilling_operation(plan).accepted(use_estimated_time=True)

    assert operation.user_action is UserAction.ACCEPTED
    assert operation.time_source is TimeSource.SYSTEM
    assert operation.final_cycle_time == operation.system_suggested_cycle_time
    assert operation.final_setup_time == operation.system_suggested_setup_time
    assert operation.costable is True


def test_accepting_without_using_the_estimate_still_commits_nothing():
    """Agreeing with the operation is not agreeing with its time."""
    operation = drilling_operation(plan_for()).accepted()

    assert operation.user_action is UserAction.ACCEPTED
    assert operation.time_source is TimeSource.NEEDS_INPUT
    assert operation.costable is False


# --- editing times -----------------------------------------------------------


def test_editing_a_time_never_overwrites_the_estimate():
    plan = plan_for()
    original = drilling_operation(plan)
    suggested_setup = original.system_suggested_setup_time
    suggested_cycle = original.system_suggested_cycle_time

    edited = original.with_times(setup_time_minutes=6.0, cycle_time_minutes=3.2)

    assert edited.system_suggested_setup_time == suggested_setup
    assert edited.system_suggested_cycle_time == suggested_cycle
    assert edited.user_setup_time == 6.0
    assert edited.user_cycle_time == 3.2
    assert edited.final_cycle_time == 3.2
    assert edited.time_source is TimeSource.USER
    assert edited.user_action is UserAction.EDITED


def test_a_user_time_wins_over_an_accepted_estimate():
    operation = (
        drilling_operation(plan_for())
        .accepted(use_estimated_time=True)
        .with_times(cycle_time_minutes=9.9)
    )

    assert operation.system_time_accepted is True  # the acceptance is still recorded
    assert operation.final_cycle_time == 9.9  # but the typed value is what counts
    assert operation.time_source is TimeSource.USER


def test_a_zero_setup_time_is_allowed_but_a_zero_cycle_time_is_not():
    operation = drilling_operation(plan_for())

    assert operation.with_times(setup_time_minutes=0.0).user_setup_time == 0.0
    with pytest.raises(ValueError, match="greater than zero"):
        operation.with_times(cycle_time_minutes=0.0)
    with pytest.raises(ValueError, match="negative"):
        operation.with_times(setup_time_minutes=-1.0)


def test_entering_exactly_the_estimated_time_is_an_acceptance_not_a_correction():
    operation = drilling_operation(plan_for())

    same = operation.with_times(
        setup_time_minutes=operation.system_suggested_setup_time,
        cycle_time_minutes=operation.system_suggested_cycle_time,
    )

    assert same.user_action is UserAction.ACCEPTED


# --- editing the operation ---------------------------------------------------


def test_changing_the_process_keeps_what_was_suggested():
    operation = drilling_operation(plan_for()).with_operation_type("milling")

    assert operation.system_suggested_operation == "drilling"
    assert operation.user_operation_type == "milling"
    assert operation.operation_type == "milling"
    assert operation.user_action is UserAction.EDITED


def test_changing_the_process_invalidates_a_time_estimated_for_the_old_one():
    """A milling time is not a turning time, however it was accepted."""
    operation = (
        drilling_operation(plan_for())
        .accepted(use_estimated_time=True)
        .with_operation_type("turning")
    )

    assert operation.system_suggested_cycle_time is not None  # still recorded
    assert operation.system_time_applies is False
    assert operation.final_cycle_time is None
    assert operation.costable is False
    assert "was for drilling" in operation.not_costable_reason
    assert "now turning" in operation.not_costable_reason


def test_a_changed_process_can_still_be_costed_with_an_entered_time():
    operation = (
        drilling_operation(plan_for())
        .with_operation_type("turning")
        .with_times(setup_time_minutes=12.0, cycle_time_minutes=4.0)
    )

    assert operation.costable is True
    assert operation.operation_type == "turning"
    assert operation.final_cycle_time == 4.0


def test_choosing_the_suggested_process_again_is_an_acceptance():
    operation = drilling_operation(plan_for()).with_operation_type("Drilling")

    assert operation.user_operation_type is None
    assert operation.user_action is UserAction.ACCEPTED


def test_an_empty_operation_type_is_refused():
    with pytest.raises(ValueError):
        drilling_operation(plan_for()).with_operation_type("   ")


# --- removing and adding -----------------------------------------------------


def test_rejecting_an_operation_keeps_it_as_a_record():
    plan = plan_for()
    operation = drilling_operation(plan).rejected()
    plan = plan.replacing(operation)

    assert operation.user_action is UserAction.REJECTED
    assert operation.system_suggested_operation == "drilling"  # still known
    assert operation not in plan.active
    assert plan.get(operation.operation_id) is not None  # but still in the plan
    assert operation.costable is False


def test_a_manual_operation_carries_no_system_prediction():
    """Inventing one would put a claim into the feedback the system never made."""
    operation = manual_operation(
        operation_id="OP-USER-1",
        sequence_position=1,
        operation_type="Deburring",
        operation_name="Deburr edges",
        setup_time_minutes=4.0,
        cycle_time_minutes=1.5,
    )

    assert operation.origin is SuggestionOrigin.USER
    assert operation.user_action is UserAction.ADDED
    assert operation.system_suggested_operation is None
    assert operation.system_suggested_cycle_time is None
    assert operation.operation_type == "deburring"
    assert operation.costable is True


def test_a_manual_operation_without_a_time_is_not_costed_either():
    operation = manual_operation(
        operation_id="OP-USER-1", sequence_position=1, operation_type="milling",
        operation_name="Face",
    )

    assert operation.costable is False
    assert operation.time_source is TimeSource.NEEDS_INPUT


def test_a_manual_operation_needs_a_process():
    with pytest.raises(ValueError):
        manual_operation(
            operation_id="X", sequence_position=1, operation_type="", operation_name="X"
        )


def test_adding_and_removing_keeps_the_sequence_contiguous():
    plan = plan_for()
    count = len(plan.operations)

    plan = plan.with_added(
        manual_operation(
            operation_id="OP-USER-1", sequence_position=99, operation_type="milling",
            operation_name="Deburr",
        )
    )
    assert [o.sequence_position for o in plan.ordered] == list(range(1, count + 2))

    plan = plan.without("OP-USER-1")
    assert [o.sequence_position for o in plan.ordered] == list(range(1, count + 1))


def test_removing_an_unknown_operation_is_an_error():
    with pytest.raises(ValueError, match="Unknown operation"):
        plan_for().without("OP-NOPE")


# --- sequence ----------------------------------------------------------------


def test_moving_an_operation_up_and_down_reorders_and_renumbers():
    plan = plan_for()
    ids = [o.operation_id for o in plan.ordered]

    moved = plan.moved(ids[2], -1)
    assert [o.operation_id for o in moved.ordered][:3] == [ids[0], ids[2], ids[1]]
    assert [o.sequence_position for o in moved.ordered] == list(range(1, len(ids) + 1))

    back = moved.moved(ids[2], 1)
    assert [o.operation_id for o in back.ordered] == ids


def test_moving_past_either_end_is_a_no_op_not_an_error():
    plan = plan_for()
    ids = [o.operation_id for o in plan.ordered]

    assert [o.operation_id for o in plan.moved(ids[0], -1).ordered] == ids
    assert [o.operation_id for o in plan.moved(ids[-1], 1).ordered] == ids


def test_the_whole_sequence_can_be_set_at_once():
    plan = plan_for()
    reversed_ids = [o.operation_id for o in plan.ordered][::-1]

    reordered = plan.reordered(reversed_ids)

    assert [o.operation_id for o in reordered.ordered] == reversed_ids
    assert [o.sequence_position for o in reordered.ordered] == list(range(1, len(reversed_ids) + 1))


def test_a_partial_reordering_is_refused():
    plan = plan_for()
    with pytest.raises(ValueError, match="exactly once"):
        plan.reordered([plan.ordered[0].operation_id])


def test_sequence_changes_no_cost():
    """Order is recorded; it is not an input to any number."""
    plan = plan_for()
    plan = ManufacturingPlan(
        operations=[o.accepted(use_estimated_time=True) for o in plan.operations],
        units=plan.units,
    )
    forward = plan.to_operation_requirements(10)
    backward = plan.reordered([o.operation_id for o in plan.ordered][::-1]).to_operation_requirements(10)

    assert {(r.process_type, r.cycle_time_minutes, r.setup_time_minutes) for r in forward} == {
        (r.process_type, r.cycle_time_minutes, r.setup_time_minutes) for r in backward
    }


def test_no_precedence_rule_is_enforced():
    """Engineers sequence the way their shop works; nothing here objects."""
    plan = plan_for()
    ids = [o.operation_id for o in plan.ordered][::-1]

    assert plan.reordered(ids) is not None  # any order at all is acceptable

    import cad_costing_agent.manufacturing_plan as module

    source = code_without_prose(module)
    for forbidden in ["must precede", "must come before", "precedence_rules", "valid_order"]:
        assert forbidden not in source


# --- what gets costed --------------------------------------------------------


def test_only_operations_with_a_committed_time_are_costed():
    plan = plan_for()
    accepted = drilling_operation(plan).accepted(use_estimated_time=True)
    plan = plan.replacing(accepted)

    requirements = plan.to_operation_requirements(10)

    assert [r.operation_id for r in requirements] == [accepted.operation_id]
    assert len(plan.uncosted_operations()) == len(plan.active) - 1


def test_an_uncosted_operation_is_not_defaulted_to_zero():
    plan = plan_for()

    for operation in plan.uncosted_operations():
        assert operation.final_cycle_time is None
        assert operation.final_setup_time is None
    assert plan.to_operation_requirements(10) == []


def test_a_rejected_operation_is_never_costed():
    plan = plan_for()
    operation = drilling_operation(plan).accepted(use_estimated_time=True).rejected()
    plan = plan.replacing(operation)

    assert operation.costable is False
    assert operation.operation_id not in {r.operation_id for r in plan.to_operation_requirements(10)}


def test_requirements_carry_the_part_quantity_and_no_rates():
    plan = plan_for()
    plan = plan.replacing(drilling_operation(plan).accepted(use_estimated_time=True))

    requirement = plan.to_operation_requirements(7)[0]

    assert requirement.quantity == 7
    assert not hasattr(requirement, "machine_rate_per_hour")


def test_requirements_come_out_in_sequence_order():
    plan = plan_for()
    plan = ManufacturingPlan(
        operations=[o.accepted(use_estimated_time=True) for o in plan.operations],
        units=plan.units,
    )
    order = [o.operation_id for o in plan.ordered][::-1]
    plan = plan.reordered(order)

    assert [r.operation_id for r in plan.to_operation_requirements(1)] == order


# --- filling gaps ------------------------------------------------------------


def test_estimating_missing_times_leaves_existing_estimates_alone():
    plan = plan_for()
    before = {o.operation_id: o.system_suggested_cycle_time for o in plan.operations}

    after = estimate_missing_times(plan)

    assert {o.operation_id: o.system_suggested_cycle_time for o in after.operations} == before


def test_estimating_missing_times_never_writes_over_a_changed_process():
    """Writing a fresh estimate into the system fields would corrupt the record."""
    plan = plan_for()
    changed = drilling_operation(plan).with_operation_type("turning")
    plan = plan.replacing(changed)

    after = estimate_missing_times(plan)
    operation = after.get(changed.operation_id)

    assert operation.system_suggested_operation == "drilling"
    assert operation.system_suggested_cycle_time == changed.system_suggested_cycle_time


# --- feedback ----------------------------------------------------------------


def test_feedback_records_one_row_per_operation_in_sequence_order():
    plan = plan_for()

    feedback = feedback_from_plan(plan)

    assert len(feedback) == len(plan.operations)
    assert [f.operation_id for f in feedback] == [o.operation_id for o in plan.ordered]
    assert [f.sequence_position for f in feedback] == [o.sequence_position for o in plan.ordered]


def test_feedback_preserves_the_original_prediction_beside_the_final_choice():
    plan = plan_for()
    original = drilling_operation(plan)
    plan = plan.replacing(
        original.with_operation_type("turning").with_times(
            setup_time_minutes=12.0, cycle_time_minutes=4.0
        )
    )

    entry = next(f for f in feedback_from_plan(plan) if f.operation_id == original.operation_id)

    assert entry.suggested_operation == "drilling"
    assert entry.suggested_cycle_time == original.system_suggested_cycle_time
    assert entry.final_operation == "turning"
    assert entry.final_cycle_time == 4.0
    assert entry.operation_changed is True
    assert entry.cycle_time_changed is True
    assert entry.user_action is UserAction.EDITED


def test_feedback_marks_an_unchanged_acceptance_as_unchanged():
    plan = plan_for()
    plan = plan.replacing(drilling_operation(plan).accepted(use_estimated_time=True))
    entry = next(f for f in feedback_from_plan(plan) if f.time_source is TimeSource.SYSTEM)

    assert entry.operation_changed is False
    assert entry.cycle_time_changed is False
    assert entry.setup_time_changed is False
    assert entry.costed is True


def test_a_hand_added_operation_is_not_recorded_as_a_correction():
    """There was no prediction, so there was nothing to correct."""
    plan = plan_for().with_added(
        manual_operation(
            operation_id="OP-USER-1", sequence_position=99, operation_type="milling",
            operation_name="Deburr", setup_time_minutes=2.0, cycle_time_minutes=1.0,
        )
    )

    entry = next(f for f in feedback_from_plan(plan) if f.operation_id == "OP-USER-1")

    assert entry.suggested_operation is None
    assert entry.suggested_cycle_time is None
    assert entry.operation_changed is False
    assert entry.cycle_time_changed is False
    assert entry.origin is SuggestionOrigin.USER
    assert entry.user_action is UserAction.ADDED


def test_a_removed_suggestion_is_still_recorded():
    """Dropping these would leave a dataset that only saw its own successes."""
    plan = plan_for()
    rejected = drilling_operation(plan).rejected()
    plan = plan.replacing(rejected)

    entry = next(f for f in feedback_from_plan(plan) if f.operation_id == rejected.operation_id)

    assert entry.user_action is UserAction.REJECTED
    assert entry.suggested_operation == "drilling"
    assert entry.costed is False


def test_feedback_timestamps_are_valid_and_pinnable():
    moment = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    feedback = feedback_from_plan(plan_for(), estimate_id="EST-1", at=moment)

    assert all(entry.timestamp == moment for entry in feedback)
    assert all(entry.estimate_id == "EST-1" for entry in feedback)
    # Without pinning it is still a real time.
    assert all(entry.timestamp is not None for entry in feedback_from_plan(plan_for()))


def test_feedback_is_a_projection_and_cannot_drift_from_the_plan():
    plan = plan_for()
    plan = plan.replacing(drilling_operation(plan).with_times(cycle_time_minutes=5.5))

    entry = next(f for f in feedback_from_plan(plan) if f.final_cycle_time == 5.5)
    operation = plan.get(entry.operation_id)

    assert entry.final_operation == operation.operation_type
    assert entry.final_setup_time == operation.final_setup_time
    assert entry.time_source is operation.time_source
    assert entry.costed is operation.costable


def test_no_training_or_adaptation_happens_anywhere():
    import cad_costing_agent.operation_feedback as module

    source = code_without_prose(module)
    for forbidden in ["sklearn", "torch", "fit(", "train(", "weights", "learn_from"]:
        assert forbidden not in source


def test_a_recorded_correction_does_not_change_a_later_prediction():
    """The estimator is fixed. Feedback is recorded, never fed back."""
    plan = plan_for()
    before = drilling_operation(plan).system_suggested_cycle_time

    corrected = plan.replacing(drilling_operation(plan).with_times(cycle_time_minutes=99.0))
    feedback_from_plan(corrected)

    assert drilling_operation(plan_for()).system_suggested_cycle_time == before


# --- the UI ------------------------------------------------------------------


def test_the_ui_workflow_is_split_into_ordered_sections():
    """The estimate is one workflow in sections, not nine cards on one page.

    Numbering the cards 1 to 9 down a single scrolling page was how this
    interface came to read as an engineering dashboard: every step, and every
    piece of evidence behind it, competed for the same screen. The steps are
    the same steps and each card is the same card — they are now grouped into
    Overview, CAD, Manufacturing, Cost, Assembly and Quote, in that order,
    which is the order the work happens in.
    """
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    sections = ["overview", "cad", "manufacturing", "cost", "assembly", "quote"]
    positions = [markup.index(f'data-flow="{section}"') for section in sections]
    assert positions == sorted(positions), "the sections are not offered in workflow order"
    for section in sections:
        assert f'data-pane="{section}"' in markup

    # Every step still has its card, and the time profile is still separate
    # from the rate set, which is what the numbering was really protecting.
    for heading in [
        "CAD file",
        "Part",
        "Rate set",
        "Time profile",
        "Recognized features",
        "Manufacturing plan",
        "Sequence",
        "Additional costs",
        "Calculate estimate",
    ]:
        assert heading in markup


def test_a_routing_row_is_one_line_with_the_detail_behind_a_control():
    """One operation, one line — and the explanation on request, not by default.

    Measured on a real 39-feature part with 31 operations: stacking the process
    select above the name input with a `<br>`, and the two move buttons under
    the sequence number, made every row 67px tall and the `#` column 126px
    wide — wider than Feature and Confidence together. The routing was 2,169px
    of table. The same controls on one line come to 35px a row.

    The explanation was always reachable by clicking the row, and nothing said
    so; `Details` is the same action with a label on it.
    """
    plan = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")

    # Process and name share one flex cell rather than being separated by a break.
    assert '<div class="op-cell">' in plan
    assert "<br><input class=\"op-name\"" not in plan

    # A labelled control for the evidence row, and the row click still works.
    assert 'class="secondary tiny op-details"' in plan
    assert 'aria-expanded="false"' in plan
    assert "function toggleEvidence" in plan
    assert plan.count("toggleEvidence(") >= 3  # defined, row click, button

    # The explanation itself is unchanged and still per operation.
    assert 'class="evidence-row hidden"' in plan
    assert "planExplanation(operation)" in plan


def test_the_manufacturing_section_leads_with_figures_not_prose():
    """The setup summary comes first; the policy and the repeats fold away.

    The charging rules, the numbered sequence and the per-feature suggestion
    breakdown are all still on the page in full. None of them is a figure, and
    all three were sitting between the reader and the ones that are.
    """
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    for toggle in ("plan-rules-toggle", "sequence-toggle", "suggestions-summary-toggle"):
        assert f'id="{toggle}"' in markup

    # Folded, not deleted: the rule this project keeps repeating is still there.
    assert "charged <strong>once</strong>" in markup
    assert 'id="sequence-list"' in markup
    assert 'id="suggestions-summary"' in markup

    # The backend-computed setup summary is not inside any disclosure.
    plan_card = markup[markup.index('id="plan-impact"') :][:400]
    assert "<details" not in plan_card


def test_the_plan_table_shows_source_and_confidence_per_row():
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    plan = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")

    for column in ["#", "Operation", "Feature", "Setup", "Cycle", "Source", "Confidence"]:
        assert f">{column}<" in markup or f'>{column}</th>' in markup
    assert "System estimate" in plan
    assert "User value" in plan
    assert "Needs input" in plan


def test_the_ui_can_reorder_operations():
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    plan = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")

    assert "move-up" in plan
    assert "move-down" in plan
    assert 'id="sequence-list"' in markup
    assert "sequence_position = position + 1" in plan


def test_the_ui_says_the_sequence_changes_no_cost():
    # Collapsed, because the sentence is wrapped across lines in the markup.
    markup = " ".join((STATIC_PATH / "index.html").read_text(encoding="utf-8").split())

    assert "changes no cost" in markup
    assert "no sequence-dependent time" in markup
    assert "no setup consolidation" in markup


def test_the_ui_sends_the_plan_rather_than_hand_built_requirements():
    app_js = (STATIC_PATH / "app.js").read_text(encoding="utf-8")

    assert 'body.append("manufacturing_plan"' in app_js
    assert "collectManufacturingPlan" in app_js


def test_the_plan_carries_the_feature_type_and_subtype_for_re_estimation():
    """Dimensions alone do not decide which formula applies.

    A drilling time needs the subtype to know whether the drill breaks through,
    and a milling time needs the type to tell a blend — followed along its
    length — from a face, which is swept over its area. Dropping them silently
    produced a shorter time for every through hole and no time at all for every
    fillet, which is why they are part of the operation rather than looked up.
    """
    plan = plan_for()

    for operation in plan.operations:
        assert operation.feature_type

    hole = drilling_operation(plan)
    assert hole.feature_subtype == "through"
    # The breakthrough allowance was applied, so the travel exceeds depth+approach.
    depth = hole.dimensions["depth_mm"]
    assert hole.system_suggested_cycle_time == pytest.approx(
        estimate_operation_time_for(hole), rel=1e-9
    )
    assert any(f"{depth:g} mm depth" in line for line in hole.system_time_evidence)
    assert any("breakthrough" in line for line in hole.system_time_evidence)


def estimate_operation_time_for(operation) -> float:
    from cad_costing_agent.operation_time import estimate_operation_time

    return estimate_operation_time(
        operation_type=operation.operation_type,
        feature_type=operation.feature_type,
        feature_subtype=operation.feature_subtype,
        dimensions=operation.dimensions,
    ).cycle_time_minutes


def test_a_fillet_in_the_plan_gets_the_blend_formula_not_an_area_one():
    plan = plan_for("block_with_fillet")
    fillet = next((o for o in plan.ordered if o.feature_type == "fillet"), None)

    if fillet is None:
        pytest.skip("this fixture produced no fillet suggestion")
    assert fillet.system_time_status is TimeStatus.ESTIMATED
    assert fillet.system_time_method == "milling_blend_finishing_time_baseline_v1"


def test_re_estimating_uses_the_same_formula_as_the_first_estimate():
    """`estimate_missing_times` must not silently pick a different branch."""
    plan = plan_for()
    hole = drilling_operation(plan)

    cleared = plan.replacing(
        hole.model_copy(
            update={
                "system_time_status": TimeStatus.NEEDS_USER_INPUT,
                "system_suggested_cycle_time": None,
                "system_time_method": None,
            }
        )
    )
    refilled = estimate_missing_times(cleared).get(hole.operation_id)

    assert refilled.system_time_method == hole.system_time_method
    assert refilled.system_suggested_cycle_time == hole.system_suggested_cycle_time
