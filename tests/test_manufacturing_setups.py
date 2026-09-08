"""Setups: the fix for charging setup time once per operation.

The mistake this step exists to remove is worth naming precisely. Six drilled
holes are six *operations* but usually one *setup*: the part is clamped once and
the spindle moves. Charging a setup per operation multiplied the setup cost by
six and produced a number no shop would recognise.

So the tests below are mostly arithmetic on that one point — one setup charges
once, six setups charge six times, and regrouping moves the cost by exactly the
setup time involved and leaves cycle time alone. The rest defend the same
invariants the plan has held since Step 20: the system's decision is never
overwritten, nothing is invented where a value is missing, and history does not
move when a later plan is edited.
"""

from __future__ import annotations

import json
from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.features import recognize_features
from cad_costing_agent.features.model import Confidence
from cad_costing_agent.main import STATIC_PATH, app
from cad_costing_agent.manufacturing_plan import (
    DEFAULT_SETUP_ID,
    ManufacturingPlan,
    ManufacturingSetup,
    build_plan,
    manual_operation,
)
from cad_costing_agent.operation_feedback import feedback_from_plan
from cad_costing_agent.operation_suggestions import SuggestionOrigin, UserAction, suggest_operations
from tests import step_fixtures

client = TestClient(app)

FORM = {
    "material": "Steel",
    "source_unit": "mm",
    "part_id": "P-SETUP",
    "part_name": "Plate",
    "quantity": "10",
    "rate_set_id": "poc-default",
}

# poc-default's drilling machine rate, used to check the setup arithmetic by hand.
DRILLING_MACHINE_RATE = 1800.0


@lru_cache(maxsize=None)
def step(name: str = "plate_with_four_holes") -> bytes:
    return getattr(step_fixtures, name)()


@lru_cache(maxsize=None)
def _built(name: str) -> str:
    recognition = recognize_features(read_step_shape(step(name)), filename=f"{name}.step")
    return build_plan(
        suggest_operations(recognition).suggestions,
        units=recognition.units,
        source_filename=f"{name}.step",
    ).model_dump_json()


def fresh_plan(name: str = "plate_with_four_holes") -> ManufacturingPlan:
    return ManufacturingPlan.model_validate_json(_built(name))


def drilling_only(setup_time: float | None = None) -> ManufacturingPlan:
    """A plan of nothing but the drilled holes, every one committed.

    Isolating one process makes the setup arithmetic checkable by hand against a
    single machine rate.
    """
    plan = fresh_plan()
    holes = [o for o in plan.ordered if o.system_suggested_operation == "drilling"]
    plan = plan.model_copy(
        update={
            "operations": [
                o.accepted(use_estimated_time=True)
                for o in plan.operations
                if o.operation_id in {h.operation_id for h in holes}
            ]
        }
    )
    if setup_time is not None:
        plan = plan.replacing_setup(plan.setup(DEFAULT_SETUP_ID).with_setup_time(setup_time))
    return plan


def cost(plan: ManufacturingPlan, **overrides):
    return client.post(
        "/cost/cad-part",
        files={"file": ("plate.step", step(), "application/octet-stream")},
        data={**FORM, **overrides, "manufacturing_plan": plan.model_dump_json()},
    )


def preview(plan: ManufacturingPlan, quantity: int = 10):
    response = client.post(
        f"/operations/plan-cost-preview?quantity={quantity}&rate_set_id=poc-default",
        json=json.loads(plan.model_dump_json()),
    )
    assert response.status_code == 200
    return response.json()


# --- the setup model ---------------------------------------------------------


def test_a_new_plan_has_exactly_one_setup_holding_everything():
    """The conservative default. The opposite default is the bug."""
    plan = fresh_plan()

    assert len(plan.setups) == 1
    setup = plan.ordered_setups[0]
    assert setup.setup_id == DEFAULT_SETUP_ID
    assert len(plan.operations_in(setup.setup_id)) == len(plan.active)
    assert all(o.system_setup_id == DEFAULT_SETUP_ID for o in plan.operations)


def test_the_default_grouping_is_marked_assumed_and_explains_itself():
    """Inferred fixture compatibility is not an engineering fact."""
    setup = fresh_plan().ordered_setups[0]

    assert setup.origin is SuggestionOrigin.SYSTEM
    assert setup.system_grouping == "system_assumed"
    assert setup.system_grouping_confidence is Confidence.LOW
    assert "nothing in the CAD proves" in setup.system_grouping_reason
    assert setup.system_grouping_assumptions
    assert any("single fixture" in line for line in setup.system_grouping_assumptions)


def test_the_setups_estimated_time_is_the_largest_baseline_never_the_sum():
    """Summing the operations' setups is exactly the error being removed."""
    plan = fresh_plan()
    setup = plan.ordered_setups[0]
    baselines = [
        o.system_suggested_setup_time
        for o in plan.operations
        if o.system_suggested_setup_time is not None
    ]

    assert setup.system_setup_time_minutes == max(baselines)
    assert setup.system_setup_time_minutes < sum(baselines)
    assert setup.system_setup_time_source == "largest_process_baseline"


def test_a_setups_time_is_a_proposal_until_someone_commits_to_it():
    setup = fresh_plan().ordered_setups[0]

    assert setup.system_setup_time_minutes is not None
    assert setup.setup_time_minutes is None  # nothing committed
    assert setup.charged_setup_time == 0.0
    assert setup.setup_time_source.value == "needs_input"


def test_accepting_or_entering_a_setup_time_commits_it():
    setup = fresh_plan().ordered_setups[0]

    accepted = setup.accepted(use_estimated_time=True)
    assert accepted.setup_time_minutes == setup.system_setup_time_minutes
    assert accepted.setup_time_source.value == "system"

    entered = setup.with_setup_time(17.0)
    assert entered.setup_time_minutes == 17.0
    assert entered.setup_time_source.value == "user"
    # And the system's figure is untouched.
    assert entered.system_setup_time_minutes == setup.system_setup_time_minutes


def test_a_user_setup_time_wins_over_an_accepted_estimate():
    setup = fresh_plan().ordered_setups[0].accepted(use_estimated_time=True).with_setup_time(21.0)

    assert setup.system_setup_time_accepted is True
    assert setup.setup_time_minutes == 21.0


def test_a_negative_setup_time_is_refused_and_zero_is_allowed():
    setup = fresh_plan().ordered_setups[0]

    assert setup.with_setup_time(0.0).setup_time_minutes == 0.0
    with pytest.raises(ValueError, match="negative"):
        setup.with_setup_time(-1.0)


def test_renaming_a_setup_records_it_as_an_edit():
    renamed = fresh_plan().ordered_setups[0].renamed("Main fixture")

    assert renamed.name == "Main fixture"
    assert renamed.user_action is UserAction.EDITED
    with pytest.raises(ValueError):
        renamed.renamed("   ")


# --- multiple setups ---------------------------------------------------------


def test_a_setup_can_be_added_and_is_marked_as_the_users():
    plan = fresh_plan()
    second = plan.with_setup(name="Side fixture")
    plan = plan.with_added_setup(second)

    assert len(plan.setups) == 2
    assert second.origin is SuggestionOrigin.USER
    assert second.system_grouping == "user_defined"
    # The system proposed nothing here, so it claims nothing.
    assert second.system_setup_time_minutes is None
    assert second.system_operation_ids == []


def test_setups_are_ordered_and_reorderable():
    plan = fresh_plan()
    plan = plan.with_added_setup(plan.with_setup(name="Second"))
    plan = plan.with_added_setup(plan.with_setup(name="Third"))
    assert [s.name for s in plan.ordered_setups] == ["Setup 1", "Second", "Third"]

    moved = plan.moved_setup(plan.ordered_setups[2].setup_id, -1)
    assert [s.name for s in moved.ordered_setups] == ["Setup 1", "Third", "Second"]
    assert [s.sequence_position for s in moved.ordered_setups] == [1, 2, 3]


def test_moving_a_setup_past_either_end_is_a_no_op():
    plan = fresh_plan().with_added_setup(fresh_plan().with_setup(name="Second"))
    first, last = plan.ordered_setups[0], plan.ordered_setups[-1]

    assert plan.moved_setup(first.setup_id, -1).ordered_setups == plan.ordered_setups
    assert plan.moved_setup(last.setup_id, 1).ordered_setups == plan.ordered_setups


def test_an_empty_setup_can_be_removed_but_a_populated_one_cannot():
    plan = fresh_plan()
    empty = plan.with_setup(name="Spare")
    plan = plan.with_added_setup(empty)

    assert len(plan.without_setup(empty.setup_id).setups) == 1
    with pytest.raises(ValueError, match="still holds"):
        plan.without_setup(DEFAULT_SETUP_ID)


def test_the_last_setup_cannot_be_removed():
    plan = fresh_plan()
    plan = plan.model_copy(update={"operations": []})

    with pytest.raises(ValueError, match="at least one setup"):
        plan.without_setup(DEFAULT_SETUP_ID)


# --- membership --------------------------------------------------------------


def test_moving_an_operation_between_setups_keeps_where_the_system_put_it():
    plan = fresh_plan()
    second = plan.with_setup(name="Side fixture")
    plan = plan.with_added_setup(second)
    operation = plan.ordered[0]

    plan = plan.with_operation_in_setup(operation.operation_id, second.setup_id)
    moved = plan.get(operation.operation_id)

    assert moved.system_setup_id == DEFAULT_SETUP_ID  # never overwritten
    assert moved.user_setup_id == second.setup_id
    assert moved.setup_id == second.setup_id
    assert moved.setup_was_changed is True
    assert moved.user_action is UserAction.EDITED


def test_moving_an_operation_back_is_no_longer_a_correction():
    plan = fresh_plan()
    second = plan.with_setup(name="Side")
    plan = plan.with_added_setup(second)
    operation = plan.ordered[0]

    plan = plan.with_operation_in_setup(operation.operation_id, second.setup_id)
    plan = plan.with_operation_in_setup(operation.operation_id, DEFAULT_SETUP_ID)

    back = plan.get(operation.operation_id)
    assert back.user_setup_id is None
    assert back.setup_was_changed is False


def test_moving_an_operation_into_an_unknown_setup_is_refused():
    plan = fresh_plan()
    with pytest.raises(ValueError, match="Unknown setup"):
        plan.with_operation_in_setup(plan.ordered[0].operation_id, "SETUP-NOPE")


def test_operations_are_numbered_within_their_own_setup():
    plan = fresh_plan()
    second = plan.with_setup(name="Side")
    plan = plan.with_added_setup(second)
    first_three = [o.operation_id for o in plan.ordered[:3]]

    for operation_id in first_three:
        plan = plan.with_operation_in_setup(operation_id, second.setup_id)

    assert [o.sequence_position for o in plan.operations_in(second.setup_id)] == [1, 2, 3]
    assert [o.sequence_position for o in plan.operations_in(DEFAULT_SETUP_ID)] == list(
        range(1, len(plan.operations_in(DEFAULT_SETUP_ID)) + 1)
    )


def test_reordering_stays_inside_a_setup():
    plan = fresh_plan()
    second = plan.with_setup(name="Side")
    plan = plan.with_added_setup(second)
    moved_out = plan.ordered[0].operation_id
    plan = plan.with_operation_in_setup(moved_out, second.setup_id)

    before = [o.operation_id for o in plan.operations_in(DEFAULT_SETUP_ID)]
    after = plan.moved(before[1], -1)

    assert [o.operation_id for o in after.operations_in(second.setup_id)] == [moved_out]
    assert [o.operation_id for o in after.operations_in(DEFAULT_SETUP_ID)][:2] == [
        before[1],
        before[0],
    ]


def test_a_plan_without_setups_is_read_as_one_setup():
    """Plans stored before setups existed still load, meaning what they meant."""
    plan = fresh_plan()
    legacy = json.loads(plan.model_dump_json())
    legacy.pop("setups")
    for operation in legacy["operations"]:
        operation["system_setup_id"] = None
        operation["user_setup_id"] = None

    restored = ManufacturingPlan.model_validate(legacy)

    assert len(restored.setups) == 1
    assert restored.ordered_setups[0].setup_id == DEFAULT_SETUP_ID
    assert len(restored.operations_in(DEFAULT_SETUP_ID)) == len(restored.active)


def test_serialisation_round_trips_and_is_deterministic():
    plan = fresh_plan()
    once = plan.model_dump_json()
    twice = ManufacturingPlan.model_validate_json(once).model_dump_json()

    assert once == twice


# --- the costing rule --------------------------------------------------------


def test_six_drilled_holes_in_one_setup_charge_one_setup():
    """The whole point. Six operations, one fixturing, one setup charge."""
    plan = drilling_only(setup_time=10.0)
    holes = plan.operations_in(DEFAULT_SETUP_ID)
    assert len(holes) >= 4

    requirements = plan.to_operation_requirements(10)

    assert len(requirements) == len(holes)
    assert sum(r.setup_time_minutes for r in requirements) == 10.0
    assert sorted({r.setup_time_minutes for r in requirements}) == [0.0, 10.0]


def test_the_same_holes_in_their_own_setups_charge_every_setup():
    plan = drilling_only(setup_time=10.0)
    holes = list(plan.operations_in(DEFAULT_SETUP_ID))

    for index, operation in enumerate(holes[1:], start=2):
        setup = plan.with_setup(name=f"Fixture {index}")
        plan = plan.with_added_setup(setup)
        plan = plan.with_operation_in_setup(operation.operation_id, setup.setup_id)
        plan = plan.replacing_setup(plan.setup(setup.setup_id).with_setup_time(10.0))

    requirements = plan.to_operation_requirements(10)

    assert len(plan.setups) == len(holes)
    assert sum(r.setup_time_minutes for r in requirements) == 10.0 * len(holes)


def test_cycle_time_is_charged_for_every_operation_however_they_are_grouped():
    one = drilling_only(setup_time=10.0)
    split = one
    moved = one.operations_in(DEFAULT_SETUP_ID)[0]
    second = split.with_setup(name="Side")
    split = split.with_added_setup(second).with_operation_in_setup(
        moved.operation_id, second.setup_id
    )

    assert one.time_summary(10).total_cycle_time_minutes == pytest.approx(
        split.time_summary(10).total_cycle_time_minutes
    )


def test_the_summary_keeps_setup_and_cycle_time_apart():
    plan = drilling_only(setup_time=10.0)

    summary = plan.time_summary(10)

    assert summary.setup_count == 1
    assert summary.total_setup_time_minutes == 10.0
    assert summary.total_cycle_time_minutes > 0
    assert summary.total_time_minutes == pytest.approx(
        summary.total_setup_time_minutes + summary.total_cycle_time_minutes
    )
    assert summary.setups[0].costable_operation_count == len(plan.operations_in(DEFAULT_SETUP_ID))


def test_a_setup_with_no_committed_time_charges_nothing_and_says_so():
    plan = drilling_only()  # setup time left uncommitted

    summary = plan.time_summary(10)

    assert summary.total_setup_time_minutes == 0.0
    assert summary.setups_needing_time == [DEFAULT_SETUP_ID]
    assert all(r.setup_time_minutes == 0.0 for r in plan.to_operation_requirements(10))


def test_a_setup_holding_no_costable_operation_charges_nothing():
    plan = drilling_only(setup_time=10.0)
    spare = plan.with_setup(name="Spare", setup_time_minutes=99.0)
    plan = plan.with_added_setup(spare)

    summary = plan.time_summary(10)

    assert summary.total_setup_time_minutes == 10.0  # not 109
    assert next(s for s in summary.setups if s.setup_id == spare.setup_id).charged_setup_time_minutes == 0.0


# --- through the API ---------------------------------------------------------


def test_moving_an_operation_to_a_new_setup_adds_exactly_that_setups_cost():
    """The arithmetic a shop would check: one more fixturing, one more setup."""
    plan = drilling_only(setup_time=10.0)
    before = preview(plan)

    moved = plan.operations_in(DEFAULT_SETUP_ID)[-1]
    second = plan.with_setup(name="Side fixture")
    plan = plan.with_added_setup(second).with_operation_in_setup(
        moved.operation_id, second.setup_id
    )
    plan = plan.replacing_setup(plan.setup(second.setup_id).with_setup_time(10.0))
    after = preview(plan)

    assert after["time"]["setup_count"] == 2
    assert after["time"]["total_setup_time_minutes"] == 20.0
    # Cycle time is untouched by regrouping.
    assert after["time"]["total_cycle_time_minutes"] == before["time"]["total_cycle_time_minutes"]
    assert after["run_cost"] == before["run_cost"]
    # And the extra cost is exactly ten minutes at the drilling machine rate.
    assert after["total_operation_cost"] - before["total_operation_cost"] == pytest.approx(
        DRILLING_MACHINE_RATE * 10.0 / 60.0, abs=0.01
    )


def test_reordering_operations_changes_no_cost():
    plan = drilling_only(setup_time=10.0)
    before = preview(plan)

    reordered = plan.moved(plan.operations_in(DEFAULT_SETUP_ID)[0].operation_id, 1)

    assert preview(reordered)["total_operation_cost"] == before["total_operation_cost"]


def test_reordering_setups_changes_no_cost():
    plan = drilling_only(setup_time=10.0)
    second = plan.with_setup(name="Side", setup_time_minutes=10.0)
    moved = plan.operations_in(DEFAULT_SETUP_ID)[-1]
    plan = plan.with_added_setup(second).with_operation_in_setup(moved.operation_id, second.setup_id)
    before = preview(plan)

    assert preview(plan.moved_setup(second.setup_id, -1))["total_operation_cost"] == (
        before["total_operation_cost"]
    )


def test_the_preview_splits_setup_cost_from_run_cost():
    plan = drilling_only(setup_time=10.0)

    body = preview(plan)

    assert body["setup_cost"] == pytest.approx(DRILLING_MACHINE_RATE * 10.0 / 60.0, abs=0.01)
    assert body["run_cost"] > 0
    assert body["total_operation_cost"] == pytest.approx(body["setup_cost"] + body["run_cost"])
    assert body["currency"] == "INR"


def test_the_preview_persists_nothing():
    before = client.get("/estimates").json()["total"]

    preview(drilling_only(setup_time=10.0))

    assert client.get("/estimates").json()["total"] == before


def test_an_estimate_charges_the_setup_once():
    plan = drilling_only(setup_time=10.0)

    part_cost = cost(plan).json()["part_cost"]
    charged = [o["setup_time_minutes"] for o in part_cost["operations"]]

    assert sum(charged) == 10.0
    assert len(charged) == len(plan.operations_in(DEFAULT_SETUP_ID))


def test_splitting_a_plan_raises_the_estimate_by_the_added_setup_only():
    plan = drilling_only(setup_time=10.0)
    before = cost(plan).json()["part_cost"]

    moved = plan.operations_in(DEFAULT_SETUP_ID)[-1]
    second = plan.with_setup(name="Side", setup_time_minutes=10.0)
    split = plan.with_added_setup(second).with_operation_in_setup(
        moved.operation_id, second.setup_id
    )
    after = cost(split).json()["part_cost"]

    assert after["total_part_cost"] - before["total_part_cost"] == pytest.approx(
        DRILLING_MACHINE_RATE * 10.0 / 60.0, abs=0.01
    )
    assert after["total_material_cost"] == before["total_material_cost"]


# --- feedback ----------------------------------------------------------------


def test_feedback_records_the_grouping_the_system_chose():
    plan = drilling_only(setup_time=10.0)

    entry = feedback_from_plan(plan)[0]

    assert entry.system_setup_id == DEFAULT_SETUP_ID
    assert entry.final_setup_id == DEFAULT_SETUP_ID
    assert entry.setup_changed is False
    assert entry.system_setup_grouping == "system_assumed"
    assert entry.setup_operation_count == len(plan.operations_in(DEFAULT_SETUP_ID))


def test_feedback_records_a_regrouping_without_losing_the_original():
    """'The system grouped H1-H4; the engineer moved H4 out' — the whole point."""
    plan = drilling_only(setup_time=10.0)
    moved = plan.operations_in(DEFAULT_SETUP_ID)[-1]
    second = plan.with_setup(name="Side fixture", setup_time_minutes=8.0)
    plan = plan.with_added_setup(second).with_operation_in_setup(
        moved.operation_id, second.setup_id
    )

    entry = next(f for f in feedback_from_plan(plan) if f.operation_id == moved.operation_id)

    assert entry.system_setup_id == DEFAULT_SETUP_ID
    assert entry.final_setup_id == second.setup_id
    assert entry.setup_changed is True
    assert entry.system_setup_grouping == "user_defined"  # the setup it ended in
    assert entry.final_setup_time_minutes == 8.0
    assert entry.setup_operation_count == 1


def test_feedback_records_a_setup_time_correction():
    plan = drilling_only()
    setup = plan.ordered_setups[0]
    plan = plan.replacing_setup(setup.with_setup_time(15.0))

    entry = feedback_from_plan(plan)[0]

    assert entry.system_setup_time_minutes == setup.system_setup_time_minutes
    assert entry.final_setup_time_minutes == 15.0
    assert entry.setup_time_corrected is True


def test_an_unchanged_setup_time_is_not_recorded_as_a_correction():
    plan = drilling_only()
    setup = plan.ordered_setups[0]
    plan = plan.replacing_setup(setup.accepted(use_estimated_time=True))

    assert feedback_from_plan(plan)[0].setup_time_corrected is False


# --- persistence -------------------------------------------------------------


def test_the_setup_structure_is_persisted_with_the_estimate():
    plan = drilling_only(setup_time=10.0)
    moved = plan.operations_in(DEFAULT_SETUP_ID)[-1]
    second = plan.with_setup(name="Side fixture", setup_time_minutes=8.0)
    plan = plan.with_added_setup(second).with_operation_in_setup(
        moved.operation_id, second.setup_id
    )

    estimate_id = cost(plan).json()["estimate_id"]
    snapshot = client.get(f"/estimates/{estimate_id}").json()["plan_snapshot"]
    setups = snapshot["plan"]["setups"]

    assert len(setups) == 2
    assert [s["name"] for s in sorted(setups, key=lambda s: s["sequence_position"])] == [
        "Setup 1",
        "Side fixture",
    ]
    stored = next(
        o for o in snapshot["plan"]["operations"] if o["operation_id"] == moved.operation_id
    )
    assert stored["system_setup_id"] == DEFAULT_SETUP_ID
    assert stored["user_setup_id"] == second.setup_id


def test_a_persisted_plan_survives_a_restart_with_its_grouping_intact():
    plan = drilling_only(setup_time=10.0)
    second = plan.with_setup(name="Side fixture", setup_time_minutes=8.0)
    moved = plan.operations_in(DEFAULT_SETUP_ID)[-1]
    plan = plan.with_added_setup(second).with_operation_in_setup(
        moved.operation_id, second.setup_id
    )
    estimate_id = cost(plan).json()["estimate_id"]

    reopened = TestClient(app)
    snapshot = reopened.get(f"/estimates/{estimate_id}").json()["plan_snapshot"]

    restored = ManufacturingPlan.model_validate(snapshot["plan"])
    assert len(restored.setups) == 2
    assert restored.get(moved.operation_id).user_setup_id == second.setup_id
    assert restored.setup(second.setup_id).setup_time_minutes == 8.0


def test_editing_a_plan_creates_a_new_estimate_and_leaves_the_old_one_alone():
    plan = drilling_only(setup_time=10.0)
    first_id = cost(plan).json()["estimate_id"]
    first = client.get(f"/estimates/{first_id}").json()

    second = plan.with_setup(name="Side", setup_time_minutes=8.0)
    moved = plan.operations_in(DEFAULT_SETUP_ID)[-1]
    split = plan.with_added_setup(second).with_operation_in_setup(
        moved.operation_id, second.setup_id
    )
    second_id = cost(split).json()["estimate_id"]

    assert second_id != first_id
    assert client.get(f"/estimates/{first_id}").json() == first


# --- nothing else changed ----------------------------------------------------


def test_manually_entered_operation_requirements_still_work():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("plate.step", step(), "application/octet-stream")},
        data={
            **FORM,
            "operation_requirements": json.dumps(
                [
                    {
                        "operation_id": "OP1",
                        "operation_name": "Drill",
                        "process_type": "drilling",
                        "setup_time_minutes": 20,
                        "cycle_time_minutes": 2,
                        "quantity": 10,
                    }
                ]
            ),
        },
    )

    assert response.status_code == 200
    assert response.json()["part_cost"]["operations"][0]["setup_time_minutes"] == 20


def test_a_hand_added_operation_joins_a_setup_and_is_costed():
    plan = drilling_only(setup_time=10.0)
    plan = plan.with_added(
        manual_operation(
            operation_id="OP-USER-1",
            sequence_position=99,
            operation_type="drilling",
            operation_name="Deburr",
            cycle_time_minutes=1.0,
            setup_id=DEFAULT_SETUP_ID,
        )
    )

    requirements = plan.to_operation_requirements(10)

    assert "OP-USER-1" in {r.operation_id for r in requirements}
    # Still one setup charge, however many operations joined it.
    assert sum(r.setup_time_minutes for r in requirements) == 10.0


def test_the_rate_sets_are_untouched():
    before = client.get("/rates/poc-default").json()

    cost(drilling_only(setup_time=10.0))

    assert client.get("/rates/poc-default").json() == before


def test_the_costing_formulas_are_unchanged():
    part_cost = cost(drilling_only(setup_time=10.0)).json()["part_cost"]

    assert part_cost["formula"] == (
        "total_part_cost = total_material_cost + total_operation_cost + total_additional_cost"
    )
    formulas = part_cost["operations"][0]["formulas"]
    assert "setup_cost = machine_rate_per_hour * (setup_time_minutes / 60)" in formulas


def test_no_costing_module_imports_the_setup_model():
    from pathlib import Path

    import cad_costing_agent.cad_mesh as anchor

    directory = Path(anchor.__file__).parent
    for module in ["costing.py", "operation_costing.py", "part_cost.py", "cad_part_cost.py"]:
        source = (directory / module).read_text(encoding="utf-8")
        assert "manufacturing_plan" not in source, module


# --- the UI ------------------------------------------------------------------


def test_the_plan_table_is_grouped_by_setup():
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    plan_js = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")

    assert 'id="add-setup"' in markup
    assert "setupHeaderRow" in plan_js
    assert "setup-row" in plan_js
    assert "op-setup-choice" in plan_js


def test_the_ui_offers_every_setup_edit():
    plan_js = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")

    for control in [
        "setup-name",     # rename
        "setup-time",     # edit setup time
        "setup-use-est",  # accept the system's estimate
        "setup-remove",   # remove an empty setup
        "setup-up",       # reorder setups
        "setup-down",
        "op-setup-choice",  # move an operation between setups
        "move-up",          # reorder within a setup
    ]:
        assert control in plan_js, control


def test_the_ui_distinguishes_system_from_user_grouping():
    plan_js = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")

    assert "System grouping" in plan_js
    assert "User grouping" in plan_js
    assert "system_grouping_confidence" in plan_js


def test_the_ui_shows_the_cost_impact_without_computing_it():
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    plan_js = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")

    assert 'id="plan-impact"' in markup
    assert "plan-cost-preview" in plan_js  # the backend prices it
    assert "Since your last change" in plan_js
    # The browser must not be doing the arithmetic itself.
    assert "machine_rate_per_hour" not in plan_js


def test_the_ui_says_a_setup_is_charged_once():
    markup = " ".join((STATIC_PATH / "index.html").read_text(encoding="utf-8").split())

    assert "charged <strong>once</strong>" in markup


def test_selecting_a_setup_points_at_all_its_features():
    plan_js = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")
    features_js = (STATIC_PATH / "features.js").read_text(encoding="utf-8")

    assert "selectFeatures" in plan_js
    assert "function selectFeatures" in features_js
