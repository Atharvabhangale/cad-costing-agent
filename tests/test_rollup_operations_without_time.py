"""An assembly roll-up is complete only when no planned operation is left out.

Before this, a manufactured component that had committed *some* time counted as
complete however many of its planned operations had none: on a real needle
roller bearing the roll-up read `is_complete: true` at 1,051.41 per assembly
while 39 planned operations were in no figure. The rule here is that an
operation the plan itself lists as left out for want of a committed time —
active, in the route, not as formed — makes the roll-up partial, with a reason
of its own, and that the two honest ways out are to type a time or to remove
the operation.

Everything below runs on the generated pump fixture. No count from a real file
is asserted, because which operations the system can time is expected to change.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.assembly_costing import (
    NO_COMMITTED_OPERATIONS,
    OPERATIONS_WITHOUT_TIME,
    ComponentEstimate,
    roll_up,
)
from cad_costing_agent.cad_import import decompose_assembly, detect_format
from cad_costing_agent.estimates import EstimateDetail
from cad_costing_agent.main import STATIC_PATH, app
from cad_costing_agent.manufacturing_plan import ManufacturingPlan
from cad_costing_agent.quotation import CUSTOMER, ENGINEERING, build_quotation
from tests import assembly_fixtures

client = TestClient(app)

# The pump's pins are turned, and only this rate set prices turning.
RATE_SET = "poc-default"

GOLDEN = Path(__file__).with_name("roll_up_without_operation_facts.golden.json")

NEW_LINE_KEYS = {"operations_without_time_count", "operations_without_time_ids"}
NEW_SUMMARY_KEYS = {"operations_without_time_count", "operations_without_time_definition_count"}


# --------------------------------------------------------------------- helpers


def pump() -> bytes:
    return assembly_fixtures.pump_assembly()


def assembly_of():
    data = pump()
    adapter, _ = detect_format("pump.step", data)
    return decompose_assembly(data, "pump.step", adapter)


def ids_by_name(assembly) -> dict[str, str]:
    return {definition.name: definition.definition_id for definition in assembly.definitions}


def hand_estimates(assembly) -> dict[str, ComponentEstimate]:
    """Per-part figures written out by hand: the roll-up never asks where one came from."""
    ids = ids_by_name(assembly)
    figures = {"HOUSING": (120.0, 80.0, 30.0, 10.0), "COVER": (40.0, 25.0, 12.0, 3.0),
               "PIN": (15.5, 6.25, 8.0, 1.25)}
    return {
        ids[name]: ComponentEstimate(
            definition_id=ids[name],
            estimate_id=f"EST-{name}",
            currency="INR",
            per_unit_cost=per_unit,
            material_per_unit=material,
            operation_per_unit=operation,
            additional_per_unit=additional,
            setup_per_unit=operation / 4,
            run_per_unit=operation * 3 / 4,
        )
        for name, (per_unit, material, operation, additional) in figures.items()
    }


def facts(assembly, *, without_time=None, uncommitted=()):
    """Plan facts as `main.py` hands them over, written out as plain dicts.

    `without_time` maps a component name to the operation ids its plan left
    out; `None` leaves the key off altogether — the facts a caller supplied
    before that key existed.
    """
    ids = ids_by_name(assembly)
    out = {}
    for name in ("HOUSING", "COVER", "PIN"):
        committed = name not in uncommitted
        entry = {
            "status": "generated",
            "operation_count": 4,
            "setup_count": 1,
            "setup_time_minutes": 10.0 if committed else 0.0,
            "cycle_time_minutes": 2.5 if committed else 0.0,
        }
        if without_time is not None:
            entry["uncosted_operation_ids"] = list(without_time.get(name, []))
        out[ids[name]] = entry
    return out


def golden_inputs() -> dict[str, dict]:
    """The calls whose output must not have moved. Also read by the script that
    wrote the golden file from the code as it stood before this rule."""
    assembly = assembly_of()
    return {
        "no_estimates": {"assembly": assembly, "estimates": {}},
        "no_plan_facts": {"assembly": assembly, "estimates": hand_estimates(assembly)},
        "no_plan_facts_quantity_10": {
            "assembly": assembly,
            "estimates": hand_estimates(assembly),
            "assembly_quantity": 10,
        },
        "plan_facts_without_the_operation_list": {
            "assembly": assembly,
            "estimates": hand_estimates(assembly),
            "plans": facts(assembly),
        },
        "plan_facts_without_the_operation_list_one_uncommitted": {
            "assembly": assembly,
            "estimates": hand_estimates(assembly),
            "plans": facts(assembly, uncommitted=("PIN",)),
        },
    }


def line(summary, name):
    return next(item for item in summary["lines"] if item["name"] == name)


def generated_plans() -> dict[str, dict]:
    """Every component's plan with every system estimate accepted."""
    return assembly_fixtures.committed_plans(client, rate_set_id=RATE_SET)


def plan_named(plans, name) -> dict:
    return plans[ids_by_name(assembly_of())[name]]


def unaccept(operation) -> str:
    """Take one operation back to 'estimated, not committed'. Returns its id."""
    operation["system_time_accepted"] = False
    operation["user_cycle_time"] = None
    return operation["operation_id"]


def cost_assembly(plans=None, **extra):
    data = {"material": "Steel", "source_unit": "mm", "rate_set_id": RATE_SET}
    data.update({key: str(value) for key, value in extra.items()})
    if plans is not None:
        data["component_plans"] = json.dumps(plans)
    response = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", pump(), "application/octet-stream")},
        data=data,
    )
    assert response.status_code == 200, response.text
    return response.json()


def quotation_of(estimate_id, view=CUSTOMER):
    response = client.get(f"/estimates/{estimate_id}/quotation", params={"view": view})
    assert response.status_code == 200, response.text
    return response.json()


def one_left_out():
    """A reviewed pump with one HOUSING operation left without a committed time."""
    plans = generated_plans()
    left_out = unaccept(plan_named(plans, "HOUSING")["operations"][1])
    return plans, left_out


# ------------------------------------------- no plan facts: nothing has moved


@pytest.mark.parametrize("case", sorted(golden_inputs()))
def test_a_roll_up_without_the_operation_fact_serializes_exactly_as_before(case):
    """`None` in, byte-identical out.

    The golden file was written by `roll_up` as it stood before this rule
    (commit 5c5fbd7), from these same inputs. A caller that passes no plan
    facts — or plan facts without the operation list — said nothing about
    which operations lack a time, so nothing about it may appear: not a key,
    not a null, not a zero, and not a changed sentence.
    """
    kwargs = golden_inputs()[case]
    assembly = kwargs.pop("assembly")
    estimates = kwargs.pop("estimates")

    now = roll_up(assembly, estimates, **kwargs).model_dump(mode="json")
    before = json.loads(GOLDEN.read_text(encoding="utf-8"))[case]

    assert json.dumps(now, sort_keys=True, ensure_ascii=False) == json.dumps(
        before, sort_keys=True, ensure_ascii=False
    )


def test_no_plan_facts_claims_nothing_and_is_not_made_incomplete():
    assembly = assembly_of()
    summary = roll_up(assembly, hand_estimates(assembly))

    assert summary.is_complete is True
    assert summary.operations_without_time_count is None
    assert summary.operations_without_time_definition_count is None
    dumped = summary.model_dump(mode="json")
    assert not NEW_SUMMARY_KEYS & set(dumped)
    for item in summary.lines:
        assert item.manufacturing_committed is None
        assert item.operations_without_time_count is None
    for item in dumped["lines"]:
        assert not NEW_LINE_KEYS & set(item)


def test_plan_facts_without_the_operation_list_keep_the_old_rule():
    """Minutes were supplied, the list was not: the fact is still unstated."""
    assembly = assembly_of()
    summary = roll_up(assembly, hand_estimates(assembly), plans=facts(assembly))

    assert summary.is_complete is True
    assert not NEW_SUMMARY_KEYS & set(summary.model_dump(mode="json"))


# -------------------------------------------------------- the rule, in roll_up


def test_an_operation_left_out_makes_the_roll_up_incomplete():
    assembly = assembly_of()
    summary = roll_up(
        assembly,
        hand_estimates(assembly),
        plans=facts(assembly, without_time={"HOUSING": ["HOUSING/OP-002", "HOUSING/OP-005"]}),
    )

    assert summary.is_complete is False
    assert summary.unplanned_definition_count == 0
    assert summary.needs_input_definition_count == 0
    assert summary.operations_without_time_definition_count == 1
    assert summary.operations_without_time_count == 2

    housing = next(item for item in summary.lines if item.name == "HOUSING")
    assert housing.costed is True
    assert housing.manufacturing_committed is True
    assert housing.reason == OPERATIONS_WITHOUT_TIME
    assert housing.operations_without_time_count == 2
    assert housing.operations_without_time_ids == ["HOUSING/OP-002", "HOUSING/OP-005"]
    assert "HOUSING/OP-002, HOUSING/OP-005" in housing.reason_detail

    cover = next(item for item in summary.lines if item.name == "COVER")
    assert cover.reason is None
    assert cover.operations_without_time_count == 0
    assert cover.operations_without_time_ids == []


def test_the_note_says_how_many_operations_in_how_many_components():
    assembly = assembly_of()
    summary = roll_up(
        assembly,
        hand_estimates(assembly),
        plans=facts(
            assembly,
            without_time={"HOUSING": ["HOUSING/OP-002", "HOUSING/OP-005"], "PIN": ["PIN/OP-003"]},
        ),
    )
    note = summary.completeness_note

    assert note.startswith("Partial assembly estimate — operations without a committed time.")
    assert "3 planned operation(s) in 2 manufactured component definition(s)" in note
    assert "HOUSING" in note and "PIN" in note
    assert "covers less work than the plans describe" in note
    assert "It is not a quotation" in note


def test_the_partial_total_is_still_reported_and_is_the_same_figure():
    """Calling it partial does not alter it: the figure is a true partial sum."""
    assembly = assembly_of()
    whole = roll_up(assembly, hand_estimates(assembly), plans=facts(assembly, without_time={}))
    partial = roll_up(
        assembly,
        hand_estimates(assembly),
        plans=facts(assembly, without_time={"COVER": ["COVER/OP-001"]}),
    )

    assert whole.is_complete is True
    assert partial.is_complete is False
    assert partial.total_cost is not None
    assert partial.total_cost == whole.total_cost
    assert partial.production_total_cost == whole.production_total_cost
    assert partial.operation_cost == whole.operation_cost


def test_a_stated_empty_list_is_complete_and_says_so():
    assembly = assembly_of()
    summary = roll_up(assembly, hand_estimates(assembly), plans=facts(assembly, without_time={}))

    assert summary.is_complete is True
    assert summary.operations_without_time_count == 0
    assert summary.operations_without_time_definition_count == 0
    assert summary.completeness_note.endswith(
        "No planned operation was left out for want of a committed time."
    )
    assert all(item.reason is None for item in summary.lines if not item.is_assembly)


def test_the_two_reasons_are_distinct_and_never_overlap():
    """Nothing committed at all is one reason; some committed, some left out is another."""
    assembly = assembly_of()
    summary = roll_up(
        assembly,
        hand_estimates(assembly),
        plans=facts(
            assembly,
            without_time={"HOUSING": ["HOUSING/OP-002"], "PIN": ["PIN/OP-001", "PIN/OP-002"]},
            uncommitted=("PIN",),
        ),
    )
    by_name = {item.name: item for item in summary.lines}

    assert NO_COMMITTED_OPERATIONS != OPERATIONS_WITHOUT_TIME
    assert by_name["PIN"].reason == NO_COMMITTED_OPERATIONS
    assert by_name["PIN"].manufacturing_committed is False
    assert by_name["HOUSING"].reason == OPERATIONS_WITHOUT_TIME
    assert by_name["COVER"].reason is None

    # Each component is counted under exactly one of the two.
    assert summary.unplanned_definition_count == 1
    assert summary.operations_without_time_definition_count == 1
    assert summary.operations_without_time_count == 1
    assert summary.is_complete is False
    assert "manufacturing plans incomplete" in summary.completeness_note
    assert "1 planned operation(s) in 1 manufactured component definition(s) (HOUSING)" in (
        summary.completeness_note
    )
    # The uncommitted component still carries the plain fact it was handed.
    assert by_name["PIN"].operations_without_time_ids == ["PIN/OP-001", "PIN/OP-002"]


def test_the_roll_up_still_imports_nothing_that_could_read_a_plan():
    """The new fact arrives as ids in a dict; `roll_up` cannot go and look."""
    import ast

    import cad_costing_agent.assembly_costing as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    package = {name for name in imported if name.startswith("cad_costing_agent")}

    assert package == {"cad_costing_agent.component_classification"}


# ------------------------------------------------- the rule, through the endpoint


def test_a_fully_timed_assembly_is_complete():
    summary = cost_assembly(plans=generated_plans())

    assert summary["is_complete"] is True
    assert summary["operations_without_time_count"] == 0
    assert summary["operations_without_time_definition_count"] == 0
    for name in ("HOUSING", "COVER", "PIN"):
        assert line(summary, name)["operations_without_time_ids"] == []
        assert line(summary, name)["reason"] is None


def test_one_operation_without_a_time_makes_it_partial_and_names_it():
    plans, left_out = one_left_out()
    whole = cost_assembly(plans=generated_plans())
    summary = cost_assembly(plans=plans)

    assert summary["is_complete"] is False
    assert summary["unplanned_definition_count"] == 0
    assert summary["operations_without_time_count"] == 1
    assert summary["operations_without_time_definition_count"] == 1
    housing = line(summary, "HOUSING")
    assert housing["reason"] == "operations_without_time"
    assert housing["manufacturing_committed"] is True
    assert housing["operations_without_time_ids"] == [left_out]
    assert left_out in housing["reason_detail"]
    assert summary["completeness_note"].startswith(
        "Partial assembly estimate — operations without a committed time."
    )
    # Still a figure, and a smaller one: the operation is in no number.
    assert summary["total_cost"] is not None
    assert summary["total_cost"] < whole["total_cost"]
    assert summary["total_cost"] == pytest.approx(
        sum(item["extended_cost"] for item in summary["lines"] if not item["is_assembly"])
    )


def test_what_the_roll_up_is_handed_is_exactly_what_the_plan_lists():
    """No second definition of 'left out': the ids are the plan's own."""
    plans, _ = one_left_out()
    unaccept(plan_named(plans, "PIN")["operations"][0])
    summary = cost_assembly(plans=plans)

    ids = ids_by_name(assembly_of())
    for name in ("HOUSING", "COVER", "PIN"):
        expected = [
            operation.operation_id
            for operation in ManufacturingPlan.model_validate(plans[ids[name]]).uncosted_operations()
        ]
        assert line(summary, name)["operations_without_time_ids"] == expected
    stored = client.get(f"/estimates/{summary['estimate_id']}").json()
    assert stored["plan_snapshot"]["uncosted_operation_ids"] == {
        ids[name]: line(summary, name)["operations_without_time_ids"]
        for name in ("HOUSING", "COVER", "PIN")
    }


def test_typing_a_time_completes_it():
    plans, left_out = one_left_out()
    operation = next(
        item for item in plan_named(plans, "HOUSING")["operations"]
        if item["operation_id"] == left_out
    )
    operation["user_cycle_time"] = 3.5
    summary = cost_assembly(plans=plans)

    assert summary["is_complete"] is True
    assert summary["operations_without_time_count"] == 0
    assert line(summary, "HOUSING")["reason"] is None


def test_removing_the_operation_completes_it():
    plans, left_out = one_left_out()
    partial = cost_assembly(plans=copy.deepcopy(plans))
    operation = next(
        item for item in plan_named(plans, "HOUSING")["operations"]
        if item["operation_id"] == left_out
    )
    operation["user_action"] = "rejected"
    summary = cost_assembly(plans=plans)

    assert partial["is_complete"] is False
    assert summary["is_complete"] is True
    assert summary["operations_without_time_count"] == 0
    assert line(summary, "HOUSING")["operations_without_time_ids"] == []
    # Removing it is a decision, not a price: the figure is the partial one, now whole.
    assert summary["total_cost"] == pytest.approx(partial["total_cost"])


def test_an_out_of_route_operation_does_not_count():
    """Outside the decided route is excluded on purpose, not missing a time."""
    plans = generated_plans()
    housing = plan_named(plans, "HOUSING")
    stray = housing["operations"][1]
    stray["user_operation_type"] = "drilling"
    stray["system_time_accepted"] = False
    stray["user_cycle_time"] = None
    housing["route_processes"] = ["milling"]

    validated = ManufacturingPlan.model_validate(housing)
    assert [o.operation_id for o in validated.out_of_route_operations()] == [stray["operation_id"]]
    assert validated.uncosted_operations() == []

    summary = cost_assembly(plans=plans)
    assert summary["is_complete"] is True
    assert line(summary, "HOUSING")["operations_without_time_ids"] == []

    # The same operation with no route decided *is* a gap.
    housing["route_processes"] = None
    again = cost_assembly(plans=plans)
    assert again["is_complete"] is False
    assert line(again, "HOUSING")["operations_without_time_ids"] == [stray["operation_id"]]


def test_an_as_formed_operation_does_not_count():
    """Left as formed is the engineer's decision that nothing is machined there."""
    plans, left_out = one_left_out()
    operation = next(
        item for item in plan_named(plans, "HOUSING")["operations"]
        if item["operation_id"] == left_out
    )
    housing = plan_named(plans, "HOUSING")
    # A decided cast-then-machine route is the only one that leaves anything as
    # formed; on it, the engineer says this face is not machined.
    housing["as_formed_feature_types"] = []
    operation["user_machined"] = False

    validated = ManufacturingPlan.model_validate(housing)
    assert [o.operation_id for o in validated.as_formed_operations()] == [left_out]
    assert validated.uncosted_operations() == []

    summary = cost_assembly(plans=plans)
    assert summary["is_complete"] is True
    assert summary["operations_without_time_count"] == 0

    # Said to be machined after all, it is a gap again.
    operation["user_machined"] = True
    again = cost_assembly(plans=plans)
    assert again["is_complete"] is False
    assert line(again, "HOUSING")["operations_without_time_ids"] == [left_out]


def test_a_component_with_nothing_committed_keeps_its_own_reason():
    plans, _ = one_left_out()
    ids = ids_by_name(assembly_of())
    # The pin's plan as generated: estimates present, nothing accepted.
    fresh = client.post(
        "/cad/assembly/plans",
        files={"file": ("pump.step", pump(), "application/octet-stream")},
        data={"rate_set_id": RATE_SET},
    ).json()["plan"]["component_plans"]
    plans[ids["PIN"]] = next(entry["plan"] for entry in fresh if entry["component_name"] == "PIN")

    summary = cost_assembly(plans=plans)

    assert line(summary, "PIN")["reason"] == "no_committed_operations"
    assert line(summary, "HOUSING")["reason"] == "operations_without_time"
    assert summary["unplanned_definition_count"] == 1
    assert summary["operations_without_time_definition_count"] == 1
    assert summary["is_complete"] is False


# ------------------------------------------------------- the shared test helper


def test_the_shared_helper_types_a_time_for_what_the_plan_leaves_out():
    plans, left_out = one_left_out()
    housing = assembly_fixtures.resolve_untimed_operations(plan_named(plans, "HOUSING"))
    touched = [
        operation for operation in housing["operations"]
        if operation["user_cycle_time"] is not None
    ]

    assert [operation["operation_id"] for operation in touched] == [left_out]
    assert touched[0]["user_cycle_time"] == assembly_fixtures.RESOLVED_CYCLE_MINUTES
    assert cost_assembly(plans=plans)["is_complete"] is True


def test_the_shared_helper_can_remove_them_instead():
    plans, left_out = one_left_out()
    housing = assembly_fixtures.resolve_untimed_operations(
        plan_named(plans, "HOUSING"), remove=True
    )
    removed = [
        operation["operation_id"] for operation in housing["operations"]
        if operation["user_action"] == "rejected"
    ]

    assert removed == [left_out]
    assert cost_assembly(plans=plans)["is_complete"] is True


# ---------------------------------------------------------------- the quotation


def test_the_customer_quotation_is_refused_with_a_blocker_of_its_own():
    plans, left_out = one_left_out()
    summary = cost_assembly(plans=plans)
    document = quotation_of(summary["estimate_id"])

    assert document["available"] is False
    codes = [blocker["code"] for blocker in document["blockers"]]
    assert codes == ["assembly_operations_without_time"]
    detail = document["blockers"][0]["detail"]
    assert "1 planned operation(s) in 1 manufactured component(s)" in detail
    assert "HOUSING" in detail and left_out in detail
    assert document["engineering"] is None


def test_the_engineering_view_is_still_given_and_names_the_operations():
    plans, left_out = one_left_out()
    second = unaccept(plan_named(plans, "PIN")["operations"][0])
    summary = cost_assembly(plans=plans)
    document = quotation_of(summary["estimate_id"], view=ENGINEERING)

    assert document["available"] is False
    assert document["engineering"] is not None
    listed = document["engineering"]["operations_without_time"]
    assert [(item["name"], item["operation_count"], item["operation_ids"]) for item in listed] == [
        ("HOUSING", 1, [left_out]),
        ("PIN", 1, [second]),
    ]
    assert all(item["definition_id"] and item["reason"] for item in listed)
    # The figures are not withheld from the person who has to fix it.
    assert document["totals"]["total"] is not None
    assert document["assembly"]["is_complete"] is False
    assert "operations without a committed time" in document["assembly"]["completeness_note"]
    housing = next(item for item in document["components"] if item["name"] == "HOUSING")
    assert left_out in housing["reason"]
    # The existing warning still lists every omitted id.
    warning = next(w for w in document["warnings"] if w["code"] == "partial_costing")
    assert left_out in warning["detail"] and second in warning["detail"]


def test_a_reviewed_assembly_is_quotable():
    summary = cost_assembly(plans=generated_plans())
    document = quotation_of(summary["estimate_id"], view=ENGINEERING)

    assert document["available"] is True
    assert document["blockers"] == []
    assert document["engineering"]["operations_without_time"] == []


def test_the_two_blockers_are_distinct():
    plans, _ = one_left_out()
    nothing_committed = cost_assembly()
    some_left_out = cost_assembly(plans=plans)

    first = [b["code"] for b in quotation_of(nothing_committed["estimate_id"])["blockers"]]
    second = [b["code"] for b in quotation_of(some_left_out["estimate_id"])["blockers"]]

    assert first == ["assembly_manufacturing_incomplete"]
    assert second == ["assembly_operations_without_time"]


# ---------------------------------------------------------- an older stored roll-up


def as_stored_before_this_rule(stored: dict) -> EstimateDetail:
    """The same estimate as an older build would have stored it.

    The four new keys are absent and `is_complete` is what the old rule said —
    true, because the component had committed some time. Nothing else differs.
    """
    result = copy.deepcopy(stored["result_snapshot"])
    for key in NEW_SUMMARY_KEYS:
        result.pop(key)
    for item in result["lines"]:
        for key in NEW_LINE_KEYS:
            item.pop(key, None)
        if item.get("reason") == "operations_without_time":
            item["reason"] = None
            item["reason_detail"] = ""
    result["is_complete"] = True
    return EstimateDetail(
        estimate_id=stored["estimate_id"],
        request_snapshot=stored["request_snapshot"],
        result_snapshot=result,
        rate_set_snapshot=stored.get("rate_set_snapshot"),
        plan_snapshot=stored.get("plan_snapshot"),
        time_profile_snapshot=stored.get("time_profile_snapshot"),
    )


def test_an_older_stored_roll_up_is_not_retroactively_refused():
    """Absent evidence is not evidence of a gap.

    A roll-up stored before the operation fact was recorded reads as it always
    did: quotable, with the `partial_costing` warning naming what its plan
    snapshot says was left out. It is not re-judged by a rule it predates.
    """
    plans, left_out = one_left_out()
    summary = cost_assembly(plans=plans)
    stored = client.get(f"/estimates/{summary['estimate_id']}").json()
    old = as_stored_before_this_rule(stored)

    document = build_quotation(old, view=ENGINEERING)

    assert document.available is True
    assert document.blockers == []
    assert document.assembly.is_complete is True
    assert document.engineering.operations_without_time == []
    warning = next(w for w in document.warnings if w.code == "partial_costing")
    assert left_out in warning.detail

    # And the same snapshot as stored today is refused.
    today = build_quotation(
        EstimateDetail(
            estimate_id=stored["estimate_id"],
            request_snapshot=stored["request_snapshot"],
            result_snapshot=stored["result_snapshot"],
            plan_snapshot=stored.get("plan_snapshot"),
        ),
        view=CUSTOMER,
    )
    assert [blocker.code for blocker in today.blockers] == ["assembly_operations_without_time"]


def test_the_stored_snapshot_carries_the_new_fact_additively():
    plans, left_out = one_left_out()
    summary = cost_assembly(plans=plans)
    result = client.get(f"/estimates/{summary['estimate_id']}").json()["result_snapshot"]

    assert result["operations_without_time_count"] == 1
    assert result["operations_without_time_definition_count"] == 1
    assert line(result, "HOUSING")["operations_without_time_ids"] == [left_out]
    assert result["is_complete"] is False
    # A roll-up costed with no plans at all stores none of it.
    bare = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", pump(), "application/octet-stream")},
        data={"material": "Steel", "source_unit": "mm", "rate_set_id": RATE_SET,
              "components": json.dumps([
                  {"definition_id": definition_id, "classification": "purchased",
                   "unit_cost": 5.0, "currency": "INR"}
                  for name, definition_id in ids_by_name(assembly_of()).items()
                  if name in ("HOUSING", "COVER", "PIN")
              ])},
    )
    assert bare.status_code == 200, bare.text
    assert not NEW_SUMMARY_KEYS & set(bare.json())


# ------------------------------------------- the readers of `is_complete`


def test_an_actual_is_not_compared_with_a_roll_up_that_leaves_operations_out():
    plans, _ = one_left_out()
    summary = cost_assembly(plans=plans)
    body = {"figure_kind": "actual", "source": "Client ERP actual", "cost_basis": "per_unit",
            "currency": "INR", "total_cost": 300.0}

    entry = client.post(f"/estimates/{summary['estimate_id']}/actuals", json=body)
    assert entry.status_code == 201, entry.text
    assert entry.json()["variance"]["status"] == "unavailable"
    assert entry.json()["variance"]["unavailable_code"] == "assembly_incomplete"

    whole = cost_assembly(plans=generated_plans())
    compared = client.post(f"/estimates/{whole['estimate_id']}/actuals", json=body)
    assert compared.json()["variance"]["status"] != "unavailable"


def test_the_cost_driver_table_reads_partial_and_gives_the_note():
    plans, _ = one_left_out()
    summary = cost_assembly(plans=plans)
    drivers = client.get(f"/estimates/{summary['estimate_id']}/cost-drivers").json()

    assert drivers["status"] == "partial"
    assert drivers["reason"] == summary["completeness_note"]

    whole = cost_assembly(plans=generated_plans())
    assert client.get(f"/estimates/{whole['estimate_id']}/cost-drivers").json()["status"] == (
        "available"
    )


# ------------------------------------------------------------------------- the UI


def _code(name: str) -> str:
    """A classic script with its comments removed: block comments, then line
    comments. The prose in these files describes what they do not do."""
    source = (STATIC_PATH / name).read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    return "\n".join(
        text for text in source.split("\n") if not text.strip().startswith("//")
    )


def _function(code: str, name: str) -> str:
    start = code.index(f"function {name}(")
    following = code.find("\nfunction ", start + 1)
    return code[start:] if following == -1 else code[start:following]


def test_the_roll_up_card_lists_the_components_from_the_servers_fields():
    body = _function(_code("assembly.js"), "componentsNeedingTimesMarkup")

    assert "summary.operations_without_time_count" in body
    assert "summary.operations_without_time_definition_count" in body
    assert "line.operations_without_time_count" in body
    assert "line.operations_without_time_ids" in body
    assert 'line.reason === "operations_without_time"' in body
    assert 'id="assembly-needs-times"' in body


def test_the_browser_counts_nothing_about_left_out_operations():
    """Every number shown is one the server sent. No length, no sum, no time test."""
    body = _function(_code("assembly.js"), "componentsNeedingTimesMarkup")

    assert ".length" not in body
    assert ".reduce(" not in body
    assert "+=" not in body
    assert re.search(r"[0-9a-z_)\]] [-+*/] [0-9a-z_(]", body) is None, "no arithmetic"
    for field in ("system_time_accepted", "user_cycle_time", "system_suggested_cycle_time",
                  "user_action", "isCostable", "finalCycle"):
        assert field not in body, field


def test_the_roll_up_card_marks_its_total_partial_from_is_complete():
    render = _function(_code("assembly.js"), "renderAssemblyCost")

    assert "summary.is_complete" in render
    assert 'id="assembly-total-partial"' in render
    assert "Assembly total (one assembly)${partialMark}" in render
    assert "assemblies${partialMark}" in render
    assert "${componentsNeedingTimesMarkup(summary)}" in render


def test_the_flow_router_labels_a_partial_figure_and_places_the_list():
    code = _code("estimateflow.js")
    label = _function(code, "partialLabel")
    figure = _function(code, "partialFigure")

    assert "target.incomplete" in label and "(partial)" in label
    assert "target.incomplete" in figure and "(partial)" in figure
    # Overview, the cost banner, the Quote screen and the context bar.
    assert code.count("partialLabel(") >= 7
    assert code.count("partialFigure(") == 3
    # The list is the roll-up card's, drawn by the file that owns it.
    quote = _function(code, "renderQuote")
    assert "componentsNeedingTimesMarkup(state.assemblyCost)" in quote
    assert "function componentsNeedingTimesMarkup(" not in code
    for forbidden in ("operations_without_time", ".reduce(", "system_time_accepted"):
        assert forbidden not in code, forbidden


def test_which_estimate_is_current_is_decided_exactly_as_before():
    """Only labels were added; the decision still reads the server's flag once."""
    decide = _function(_code("estimateflow.js"), "authoritativeEstimate")

    assert "incomplete: rollUp.is_complete === false," in decide
    assert "note: rollUp.completeness_note" in decide
    assert "partial" not in decide
    assert "operations_without_time" not in decide


def test_the_report_page_names_the_operations_in_the_engineering_view():
    body = _function(_code("quotation.js"), "renderOperationsWithoutTime")

    assert "quote.engineering.operations_without_time" in body
    assert "untimed.operation_count" in body
    assert "untimed.operation_ids" in body
    assert re.search(r"[0-9a-z_)\]] [-+*/] [0-9a-z_(]", body) is None, "no arithmetic"
    assert "${renderOperationsWithoutTime(quote)}" in _function(_code("quotation.js"), "renderBlocked")
