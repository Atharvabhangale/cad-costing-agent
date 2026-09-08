"""The plan meeting the costing pipeline, through the API and the database.

The one thing worth stating plainly: **a time the system estimated is not money
until a person commits to it.** Everything below is a way of checking that gap
has not quietly closed — that an untimed operation is omitted rather than
defaulted, that the engineer's value wins over the system's, and that both
survive into the stored estimate so a later reader can see which was used.

The existing costing formulas are not exercised here; they are unchanged, and
their own tests cover them. What is checked is which inputs reach them.
"""

from __future__ import annotations

import json
from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.features import recognize_features
from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import (
    ManufacturingPlan,
    build_plan,
    manual_operation,
)
from cad_costing_agent.operation_suggestions import suggest_operations
from tests import step_fixtures

client = TestClient(app)

FORM = {
    "material": "Steel",
    "source_unit": "mm",
    "part_id": "P-PLAN",
    "part_name": "Plate",
    "quantity": "10",
    "rate_set_id": "poc-default",
}


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


def cost(plan: ManufacturingPlan | None = None, **overrides):
    data = {**FORM, **overrides}
    if plan is not None:
        data["manufacturing_plan"] = plan.model_dump_json()
    return client.post(
        "/cost/cad-part",
        files={"file": ("plate.step", step(), "application/octet-stream")},
        data=data,
    )


def drilling(plan: ManufacturingPlan):
    return next(o for o in plan.ordered if o.system_suggested_operation == "drilling")


# --- an estimated time is not money ------------------------------------------


def test_a_plan_of_pure_estimates_costs_no_operations():
    """Every row has a time; none is committed. The operations total is zero."""
    response = cost(fresh_plan())

    assert response.status_code == 200
    part_cost = response.json()["part_cost"]
    assert part_cost["total_operation_cost"] == 0.0
    assert part_cost["operations"] == []
    assert part_cost["total_material_cost"] > 0


def test_accepting_an_estimate_is_what_makes_it_cost_something():
    plan = fresh_plan()
    before = cost(plan).json()["part_cost"]["total_part_cost"]

    operation = drilling(plan)
    accepted = plan.replacing(operation.accepted(use_estimated_time=True))
    after = cost(accepted).json()["part_cost"]

    assert after["total_part_cost"] > before
    assert [o["operation_id"] for o in after["operations"]] == [operation.operation_id]
    assert after["operations"][0]["cycle_time_minutes"] == operation.system_suggested_cycle_time


def test_a_user_edited_cycle_time_is_what_gets_costed_not_the_estimate():
    """The heart of it: the discarded system value must not reach the money."""
    plan = fresh_plan()
    operation = drilling(plan)
    assert operation.system_suggested_cycle_time != 3.2

    edited = plan.replacing(operation.with_times(cycle_time_minutes=3.2))
    costed = cost(edited).json()["part_cost"]["operations"][0]

    assert costed["cycle_time_minutes"] == 3.2


def test_setup_time_comes_from_the_setup_not_from_the_operation():
    """Since Step 21b a setup owns its time; an operation's own figure is a record.

    The operation still carries the per-process baseline the estimator produced,
    but it is not what gets charged — otherwise six holes would carry six setups,
    which is the error the setup model exists to remove.
    """
    plan = fresh_plan()
    operation = drilling(plan)
    plan = plan.replacing(operation.with_times(setup_time_minutes=6.0, cycle_time_minutes=3.2))

    # The setup's own time is uncommitted, so nothing is charged for it.
    costed = cost(plan).json()["part_cost"]["operations"][0]
    assert costed["setup_time_minutes"] == 0.0

    # Commit the setup's time, and that single figure is what appears.
    setup = plan.ordered_setups[0]
    plan = plan.replacing_setup(setup.with_setup_time(9.0))
    costed = cost(plan).json()["part_cost"]["operations"][0]
    assert costed["setup_time_minutes"] == 9.0


def test_a_user_edited_operation_is_costed_at_the_process_they_chose():
    plan = fresh_plan()
    operation = drilling(plan)

    changed = plan.replacing(
        operation.with_operation_type("milling").with_times(
            setup_time_minutes=8.0, cycle_time_minutes=2.0
        )
    )
    costed = cost(changed).json()["part_cost"]["operations"][0]

    assert costed["process_type"] == "milling"
    # And at the milling rate, not the drilling one.
    assert costed["machine_rate_per_hour"] == 1200.0


def test_an_operation_needing_input_cannot_silently_become_priced():
    """A changed process invalidates its estimate; nothing substitutes for it."""
    plan = fresh_plan()
    operation = drilling(plan)

    stale = plan.replacing(operation.accepted(use_estimated_time=True).with_operation_type("turning"))
    part_cost = cost(stale).json()["part_cost"]

    assert part_cost["operations"] == []
    assert part_cost["total_operation_cost"] == 0.0


def test_a_rejected_operation_is_not_costed():
    plan = fresh_plan()
    operation = drilling(plan)
    plan = plan.replacing(operation.accepted(use_estimated_time=True))
    with_it = cost(plan).json()["part_cost"]["total_operation_cost"]

    without = plan.replacing(plan.get(operation.operation_id).rejected())

    assert with_it > 0
    assert cost(without).json()["part_cost"]["total_operation_cost"] == 0.0


def test_a_manually_added_operation_is_costed_like_any_other():
    plan = fresh_plan().with_added(
        manual_operation(
            operation_id="OP-USER-1",
            sequence_position=99,
            operation_type="milling",
            operation_name="Deburr edges",
            setup_time_minutes=4.0,
            cycle_time_minutes=1.5,
        )
    )

    costed = cost(plan).json()["part_cost"]["operations"]

    assert [o["operation_id"] for o in costed] == ["OP-USER-1"]
    assert costed[0]["cycle_time_minutes"] == 1.5


# --- the existing paths are untouched ----------------------------------------


def test_costing_without_a_plan_behaves_exactly_as_before():
    requirements = [
        {
            "operation_id": "OP1",
            "operation_name": "Drill",
            "process_type": "drilling",
            "setup_time_minutes": 20,
            "cycle_time_minutes": 2,
            "quantity": 10,
        }
    ]
    response = cost(None, operation_requirements=json.dumps(requirements))

    assert response.status_code == 200
    part_cost = response.json()["part_cost"]
    assert len(part_cost["operations"]) == 1
    assert part_cost["operations"][0]["cycle_time_minutes"] == 2


def test_sending_both_a_plan_and_requirements_is_refused_as_ambiguous():
    response = cost(
        fresh_plan(),
        operation_requirements=json.dumps(
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
    )

    assert response.status_code == 400
    assert "not both" in response.json()["detail"]


def test_a_malformed_plan_is_a_400_not_a_crash():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("plate.step", step(), "application/octet-stream")},
        data={**FORM, "manufacturing_plan": "{not json"},
    )

    assert response.status_code == 400
    assert "valid JSON" in response.json()["detail"]


def test_the_material_cost_is_unaffected_by_the_plan():
    """A plan changes operations only. Mass and material cost come from geometry."""
    without = cost(None).json()["part_cost"]
    with_plan = cost(fresh_plan()).json()["part_cost"]

    assert with_plan["total_material_cost"] == without["total_material_cost"]


def test_the_rate_sets_are_not_modified_by_costing_a_plan():
    before = client.get("/rates/poc-default").json()

    cost(fresh_plan())

    assert client.get("/rates/poc-default").json() == before


def test_the_costing_formulas_are_unchanged():
    """The formulas are reported on every estimate; they must read as before."""
    part_cost = cost(fresh_plan()).json()["part_cost"]

    assert part_cost["formula"] == (
        "total_part_cost = total_material_cost + total_operation_cost + total_additional_cost"
    )
    assert "quantity" in part_cost["quantity_assumption"]


def test_the_plan_modules_are_not_imported_by_any_costing_module():
    """Structural: costing must not be able to reach the plan or the estimator."""
    from pathlib import Path

    import cad_costing_agent.cad_mesh as anchor

    directory = Path(anchor.__file__).parent
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
        for forbidden in ["manufacturing_plan", "operation_time", "operation_feedback"]:
            assert forbidden not in source, f"{module} imports {forbidden}"


# --- persistence -------------------------------------------------------------


def test_the_plan_and_its_feedback_are_persisted_with_the_estimate():
    plan = fresh_plan()
    plan = plan.replacing(drilling(plan).with_times(setup_time_minutes=6.0, cycle_time_minutes=3.2))

    estimate_id = cost(plan).json()["estimate_id"]
    stored = client.get(f"/estimates/{estimate_id}").json()
    snapshot = stored["plan_snapshot"]

    assert snapshot is not None
    assert len(snapshot["plan"]["operations"]) == len(plan.operations)
    assert len(snapshot["feedback"]) == len(plan.operations)
    assert snapshot["uncosted_operation_ids"]


def test_a_stored_estimate_can_reconstruct_the_whole_decision():
    """Feature, suggestion, estimate, edit, final value, sequence — all present."""
    plan = fresh_plan()
    operation = drilling(plan)
    plan = plan.replacing(
        operation.with_operation_type("milling").with_times(
            setup_time_minutes=7.0, cycle_time_minutes=2.5
        )
    )

    estimate_id = cost(plan).json()["estimate_id"]
    snapshot = client.get(f"/estimates/{estimate_id}").json()["plan_snapshot"]
    entry = next(
        f for f in snapshot["feedback"] if f["operation_id"] == operation.operation_id
    )

    assert entry["feature_id"] == operation.feature_ids[0]          # the CAD feature
    assert entry["suggested_operation"] == "drilling"                # what was suggested
    assert entry["suggested_cycle_time"] == operation.system_suggested_cycle_time  # and estimated
    assert entry["final_operation"] == "milling"                     # what was chosen
    assert entry["final_cycle_time"] == 2.5                          # and its time
    assert entry["operation_changed"] is True
    assert entry["cycle_time_changed"] is True
    assert entry["sequence_position"] >= 1                           # and the order
    assert entry["timestamp"]


def test_the_persisted_plan_records_which_operations_were_not_costed():
    plan = fresh_plan()
    plan = plan.replacing(drilling(plan).accepted(use_estimated_time=True))

    estimate_id = cost(plan).json()["estimate_id"]
    snapshot = client.get(f"/estimates/{estimate_id}").json()["plan_snapshot"]

    costed = {o["operation_id"] for o in cost(plan).json()["part_cost"]["operations"]}
    assert costed.isdisjoint(snapshot["uncosted_operation_ids"])
    assert "left out of the cost rather than defaulted" in snapshot["note"]


def test_editing_and_recosting_creates_a_new_estimate_and_leaves_the_old_one_alone():
    """A later edit must never rewrite what an earlier estimate said."""
    plan = fresh_plan()
    first_plan = plan.replacing(drilling(plan).accepted(use_estimated_time=True))
    first_id = cost(first_plan).json()["estimate_id"]
    first = client.get(f"/estimates/{first_id}").json()

    second_plan = plan.replacing(drilling(plan).with_times(cycle_time_minutes=12.0))
    second_id = cost(second_plan).json()["estimate_id"]

    assert second_id != first_id
    assert client.get(f"/estimates/{first_id}").json() == first
    assert (
        client.get(f"/estimates/{second_id}").json()["total_part_cost"]
        != first["total_part_cost"]
    )


def test_a_persisted_plan_survives_a_restart():
    plan = fresh_plan()
    plan = plan.replacing(drilling(plan).with_times(cycle_time_minutes=4.4))
    estimate_id = cost(plan).json()["estimate_id"]

    # A new client on the same database is what a restart looks like from here.
    reopened = TestClient(app)
    snapshot = reopened.get(f"/estimates/{estimate_id}").json()["plan_snapshot"]

    assert any(f["final_cycle_time"] == 4.4 for f in snapshot["feedback"])
    assert any(f["suggested_cycle_time"] is not None for f in snapshot["feedback"])


def test_an_estimate_made_without_a_plan_stores_none_rather_than_an_empty_one():
    estimate_id = cost(None).json()["estimate_id"]

    assert client.get(f"/estimates/{estimate_id}").json()["plan_snapshot"] is None


def test_the_sequence_is_persisted():
    plan = fresh_plan()
    reversed_ids = [o.operation_id for o in plan.ordered][::-1]
    plan = plan.reordered(reversed_ids)

    estimate_id = cost(plan).json()["estimate_id"]
    snapshot = client.get(f"/estimates/{estimate_id}").json()["plan_snapshot"]
    order = sorted(snapshot["plan"]["operations"], key=lambda o: o["sequence_position"])

    assert [o["operation_id"] for o in order] == reversed_ids


def test_the_stored_request_records_the_operations_that_were_actually_costed():
    plan = fresh_plan()
    operation = drilling(plan)
    plan = plan.replacing(operation.accepted(use_estimated_time=True))

    estimate_id = cost(plan).json()["estimate_id"]
    stored = client.get(f"/estimates/{estimate_id}").json()

    requirements = stored["request_snapshot"]["operation_requirements"]
    assert [r["operation_id"] for r in requirements] == [operation.operation_id]


# --- the estimate-time endpoint ----------------------------------------------


def test_the_estimate_time_endpoint_needs_no_cad_upload():
    """Recognition already ran; re-parsing a 200 MB export would be the cost."""
    response = client.post(
        "/operations/estimate-time",
        json={
            "units": "mm",
            "operations": [
                {
                    "operation_id": "OP-001",
                    "operation_type": "drilling",
                    "feature_id": "H-001",
                    "dimensions": {"diameter_mm": 10.0, "depth_mm": 20.0},
                    "confidence": "high",
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["estimated_count"] == 1
    estimate = body["estimates"][0]["estimate"]
    assert estimate["cycle_time_minutes"] > 0
    assert estimate["confidence"] == "medium"
    assert estimate["method"] == "drilling_time_baseline_v1"
    assert estimate["evidence"] and estimate["assumptions"]


def test_the_estimate_time_endpoint_is_deterministic():
    payload = {
        "units": "mm",
        "operations": [
            {"operation_type": "turning", "dimensions": {"diameter_mm": 40.0, "length_mm": 90.0}}
        ],
    }

    first = client.post("/operations/estimate-time", json=payload).json()
    second = client.post("/operations/estimate-time", json=payload).json()

    assert first == second


def test_the_estimate_time_endpoint_reports_what_it_could_not_estimate():
    response = client.post(
        "/operations/estimate-time",
        json={"units": "mm", "operations": [{"operation_type": "drilling", "dimensions": {}}]},
    )

    body = response.json()
    assert body["needs_input_count"] == 1
    assert body["estimates"][0]["estimate"]["time_status"] == "needs_user_input"
    assert body["estimates"][0]["estimate"]["cycle_time_minutes"] is None


def test_the_estimate_time_endpoint_persists_nothing():
    before = client.get("/estimates").json()["total"]

    client.post(
        "/operations/estimate-time",
        json={
            "units": "mm",
            "operations": [
                {"operation_type": "drilling", "dimensions": {"diameter_mm": 8.0, "depth_mm": 12.0}}
            ],
        },
    )

    assert client.get("/estimates").json()["total"] == before


def test_the_estimate_time_endpoint_exposes_no_cad_internals():
    response = client.post(
        "/operations/estimate-time",
        json={
            "units": "mm",
            "operations": [
                {"operation_type": "drilling", "dimensions": {"diameter_mm": 8.0, "depth_mm": 12.0}}
            ],
        },
    )

    for leaked in ["TopoDS", "OCP.", "gp_Pnt", "BRep_Tool"]:
        assert leaked not in response.text
