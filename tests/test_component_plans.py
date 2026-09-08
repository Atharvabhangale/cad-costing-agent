"""A manufacturing plan per component, not one flat plan per assembly.

The thing being defended here is that a plan stays a routing for **one thing
clamped in a fixture**. An assembly-wide plan would put a housing's facing
operation in the same setup as a shaft's turning operation, and since setup time
is charged once per setup the assembly would be under-costed by exactly the
setups nobody charged for.

So most of these tests are about separation and about what is *not* claimed:
that two components never share a setup, that an operation can always be traced
back to the component that caused it, that a bought component gets no machining
plan, that "not generated" is never zero operations, and that one definition
placed twelve times is planned once and multiplied afterwards.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.assembly_plan import (
    ASSEMBLY_PLAN_NOTE,
    BLOCKED,
    BOUGHT_NOT_MADE,
    DELIBERATELY_EXCLUDED,
    GENERATED,
    NEEDS_INPUT,
    NO_GEOMETRY,
    NOT_APPLICABLE,
    NOT_CLASSIFIED,
    NOT_GENERATED,
    STATUS_LABELS,
    SUB_ASSEMBLY,
    build_assembly_plan,
    status_for,
)
from cad_costing_agent.cad_import import decompose_assembly, detect_format
from cad_costing_agent.component_classification import (
    EXCLUDED,
    MANUFACTURED,
    PURCHASED,
    STANDARD,
    UNRESOLVED,
)
from cad_costing_agent.main import app
from tests import assembly_fixtures

client = TestClient(app)

STATIC = Path(__file__).resolve().parent.parent / "src" / "cad_costing_agent" / "static"
REAL_MODELS = Path(__file__).resolve().parent.parent / "cadmodels"

RATE_SET = "poc-machine-shop"


# --------------------------------------------------------------------- helpers


def pump() -> bytes:
    return assembly_fixtures.pump_assembly()


def assembly_of():
    data = pump()
    adapter, _ = detect_format("pump.step", data)
    return decompose_assembly(data, "pump.step", adapter)


def definition_ids():
    return {d.name: d.definition_id for d in assembly_of().definitions}


def build_plans(components=None, **extra):
    data = {"rate_set_id": RATE_SET}
    data.update({key: str(value) for key, value in extra.items()})
    if components is not None:
        data["components"] = json.dumps(components)
    response = client.post(
        "/cad/assembly/plans",
        files={"file": ("pump.step", pump(), "application/octet-stream")},
        data=data,
    )
    assert response.status_code == 200, response.text
    return response.json()


def component(result, name):
    return next(c for c in result["plan"]["component_plans"] if c["component_name"] == name)


def cost_assembly(components=None, plans=None, **extra):
    data = {"material": "Steel", "source_unit": "mm", "rate_set_id": RATE_SET}
    data.update({key: str(value) for key, value in extra.items()})
    if components is not None:
        data["components"] = json.dumps(components)
    if plans is not None:
        data["component_plans"] = json.dumps(plans)
    response = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", pump(), "application/octet-stream")},
        data=data,
    )
    assert response.status_code == 200, response.text
    return response.json()


# One implementation, shared with the other files that now need a fully
# reviewed assembly. Kept under this name so the tests below read unchanged.
commit_every_time = assembly_fixtures.commit_every_time


def line(summary, name):
    return next(item for item in summary["lines"] if item["name"] == name)


# --------------------------------------------------------------------- one plan each


def test_every_manufactured_component_gets_its_own_plan():
    result = build_plans()

    housing = component(result, "HOUSING")
    cover = component(result, "COVER")
    pin = component(result, "PIN")
    for entry in (housing, cover, pin):
        assert entry["classification"] == MANUFACTURED
        assert entry["plan"] is not None
        assert entry["status"] in (GENERATED, NEEDS_INPUT)

    # Three separate plans, not one shared object.
    assert len({id(entry["plan"]) for entry in (housing, cover, pin)}) == 3


def test_no_assembly_level_plan_exists():
    """There is no merged routing and no assembly sequence: nothing here knows
    what order components are made in."""
    result = build_plans()
    plan = result["plan"]

    assert "operations" not in plan
    assert "setups" not in plan
    assert "sequence" not in plan
    assert ASSEMBLY_PLAN_NOTE in plan["note"]
    assert "never share one" in plan["note"]


def test_two_components_never_share_a_setup():
    """A shared process type has never been evidence of shared fixturing in this
    codebase, and it is not evidence of it here. A housing and a pin both being
    milled does not put them in one clamping."""
    result = build_plans()
    setups_by_component = {
        entry["component_name"]: {s["setup_id"] for s in entry["plan"]["setups"]}
        for entry in result["plan"]["component_plans"]
        if entry["plan"]
    }

    all_ids = [sid for ids in setups_by_component.values() for sid in ids]
    assert len(all_ids) == len(set(all_ids)), setups_by_component
    for name, ids in setups_by_component.items():
        assert all(sid.startswith(f"{name}/") for sid in ids), (name, ids)


def test_a_sub_assembly_gets_no_plan_and_says_why():
    result = build_plans()
    entry = component(result, "COVER_ASSY")

    assert entry["plan"] is None
    assert entry["status"] == NOT_APPLICABLE
    assert entry["reason"] == SUB_ASSEMBLY
    assert "count every part inside it a second time" in entry["reason_detail"]


# --------------------------------------------------------------------- classification gate


@pytest.mark.parametrize(
    "classification, reason",
    [(PURCHASED, BOUGHT_NOT_MADE), (STANDARD, BOUGHT_NOT_MADE), (EXCLUDED, DELIBERATELY_EXCLUDED)],
)
def test_a_component_that_is_not_made_here_gets_no_machining_plan(classification, reason):
    ids = definition_ids()
    result = build_plans([{"definition_id": ids["PIN"], "user_classification": classification}])
    entry = component(result, "PIN")

    assert entry["plan"] is None
    assert entry["status"] == NOT_APPLICABLE
    assert entry["reason"] == reason
    assert entry["feature_count"] == 0
    assert entry["time_summary"] is None


def test_reclassifying_to_manufactured_produces_a_plan():
    """The one thing that turns a plan on, and it has to be explicit."""
    ids = definition_ids()
    bought = build_plans([{"definition_id": ids["PIN"], "user_classification": PURCHASED}])
    made = build_plans([{"definition_id": ids["PIN"], "user_classification": MANUFACTURED}])

    assert component(bought, "PIN")["plan"] is None
    assert component(made, "PIN")["plan"] is not None


def test_an_unresolved_component_is_blocked_with_its_reason():
    ids = definition_ids()
    result = build_plans([{"definition_id": ids["PIN"], "user_classification": UNRESOLVED}])
    entry = component(result, "PIN")

    assert entry["plan"] is None
    assert entry["status"] == BLOCKED
    assert entry["reason"] == NOT_CLASSIFIED
    assert "cannot be created until someone classifies it" in entry["reason_detail"]


def test_a_component_with_no_geometry_is_blocked_on_the_geometry():
    """The more actionable of its two problems is the one named."""
    assembly = assembly_of()
    housing = next(d for d in assembly.definitions if d.name == "HOUSING")
    absent = housing.model_copy(update={"geometry_status": "no_geometry_in_file"})

    assert status_for(absent, MANUFACTURED) == (BLOCKED, NO_GEOMETRY)
    assert status_for(absent, None) == (BLOCKED, NO_GEOMETRY)


def test_an_unknown_classification_refuses_to_plan_rather_than_falling_through():
    assembly = assembly_of()
    housing = next(d for d in assembly.definitions if d.name == "HOUSING")

    assert status_for(housing, "outsourced") == (BLOCKED, NOT_CLASSIFIED)


def test_not_generated_is_not_zero_operations():
    """A component nobody has planned and one that genuinely needs no work must
    never look the same, or the first gets costed as though it were the second."""
    assembly = assembly_of()
    plan = build_assembly_plan(
        assembly, {d.definition_id: MANUFACTURED for d in assembly.definitions}, {}
    )
    housing = plan.component(definition_ids()["HOUSING"])

    assert housing.status == NOT_GENERATED
    assert housing.plan is None  # not an empty plan
    assert housing.costable is False
    assert STATUS_LABELS[NOT_GENERATED] == "Not generated"


# --------------------------------------------------------------------- traceability


def test_an_operation_can_be_traced_back_to_its_component_and_feature():
    """assembly -> component -> feature -> operation -> setup, by stable id
    rather than by position in a list."""
    result = build_plans()
    housing = component(result, "HOUSING")
    plan = housing["plan"]

    for operation in plan["operations"]:
        assert operation["operation_id"].startswith("HOUSING/OP-")
        assert operation["system_setup_id"] == "HOUSING/SETUP-001"
        for feature_id in operation["feature_ids"]:
            assert feature_id.startswith("HOUSING/")
    assert {s["setup_id"] for s in plan["setups"]} == {"HOUSING/SETUP-001"}


def test_operation_ids_are_unique_across_the_whole_assembly():
    """Two components would otherwise both call their first operation OP-001."""
    result = build_plans()
    ids = [
        operation["operation_id"]
        for entry in result["plan"]["component_plans"]
        if entry["plan"]
        for operation in entry["plan"]["operations"]
    ]

    assert len(ids) == len(set(ids))
    assert len(ids) > 3  # more than one component contributed


def test_a_setups_membership_survives_the_id_namespacing():
    """The prefix has to reach `system_setup_id`, the setup's own id and its
    membership list together, or the plan's validator would not accept it."""
    result = build_plans()
    for entry in result["plan"]["component_plans"]:
        if not entry["plan"]:
            continue
        plan = entry["plan"]
        members = {o["operation_id"] for o in plan["operations"]}
        for setup in plan["setups"]:
            assert set(setup["system_operation_ids"]) <= members
            assert set(setup["system_operation_ids"]) == members


def test_features_are_recognized_per_component_not_on_the_aggregate():
    result = build_plans()
    features = result["features"]
    ids = definition_ids()

    assert set(features) == {ids["HOUSING"], ids["COVER"], ids["PIN"]}
    for definition_id, recognition in features.items():
        name = next(n for n, i in ids.items() if i == definition_id)
        assert recognition["component_id"] == name
        for feature in recognition["features"]:
            assert feature["feature_id"].startswith(f"{name}/")
            assert feature["component_id"] == name


def test_recognition_keeps_the_viewer_linkage_and_the_confidence():
    result = build_plans()
    recognition = result["features"][definition_ids()["HOUSING"]]

    assert recognition["representation"]
    for feature in recognition["features"]:
        assert feature["confidence"]
        assert feature["source_type"]
        assert "source_faces" in feature
        assert "source_facets" in feature
    # And the linkage reaches the planned operation.
    housing = component(result, "HOUSING")
    assert any(o["source_faces"] for o in housing["plan"]["operations"])


def test_suggestions_name_the_component_that_caused_them():
    result = build_plans()
    ids = definition_ids()

    assert set(result["suggestions"]) == {ids["HOUSING"], ids["COVER"], ids["PIN"]}
    for definition_id, suggested in result["suggestions"].items():
        name = next(n for n, i in ids.items() if i == definition_id)
        for suggestion in suggested["suggestions"]:
            assert suggestion["component_id"] == name
            assert suggestion["feature_id"].startswith(f"{name}/")


def test_no_component_plan_contains_another_components_operations():
    result = build_plans()

    for entry in result["plan"]["component_plans"]:
        if not entry["plan"]:
            continue
        name = entry["component_name"]
        for operation in entry["plan"]["operations"]:
            assert operation["operation_id"].startswith(f"{name}/")
            for feature_id in operation["feature_ids"]:
                assert feature_id.startswith(f"{name}/")


# --------------------------------------------------------------------- times


def test_each_component_gets_its_own_estimated_times():
    result = build_plans()
    housing = component(result, "HOUSING")

    for operation in housing["plan"]["operations"]:
        assert operation["system_time_method"] is not None
        assert operation["system_time_confidence"] is not None
    assert housing["plan"]["setups"][0]["system_setup_time_minutes"] is not None
    assert housing["plan"]["setups"][0]["system_setup_time_source"] == "largest_process_baseline"


def test_nothing_is_committed_by_generating_a_plan():
    """Estimating is not committing. The times exist; none of them counts yet."""
    result = build_plans()
    housing = component(result, "HOUSING")

    assert housing["status"] == NEEDS_INPUT
    assert housing["missing"]
    for operation in housing["plan"]["operations"]:
        assert operation["system_time_accepted"] is False
        assert operation["user_cycle_time"] is None


def test_the_missing_inputs_are_named_one_by_one():
    result = build_plans()
    housing = component(result, "HOUSING")

    assert any("has no committed cycle time" in item for item in housing["missing"])
    assert any("has no committed setup time" in item for item in housing["missing"])
    assert all("HOUSING/" in item for item in housing["missing"])


def test_setup_and_cycle_time_are_never_combined():
    result = build_plans()
    housing = component(result, "HOUSING")
    summary = housing["time_summary"]

    assert "total_setup_time_minutes" in summary
    assert "total_cycle_time_minutes" in summary
    assert summary["total_time_minutes"] == pytest.approx(
        summary["total_setup_time_minutes"] + summary["total_cycle_time_minutes"]
    )


def test_a_time_profile_is_recorded_against_every_component_plan():
    result = build_plans(time_profile_id="poc-baseline-time")
    plan = result["plan"]

    assert plan["time_profile_id"] == "poc-baseline-time"
    assert plan["time_profile_version"]
    for entry in plan["component_plans"]:
        if not entry["plan"]:
            continue
        assert entry["plan"]["time_profile_id"] == "poc-baseline-time"
        for operation in entry["plan"]["operations"]:
            if operation["system_suggested_cycle_time"] is not None:
                assert operation["system_time_profile_id"] == "poc-baseline-time"


# --------------------------------------------------------------------- costing


def test_a_component_is_costed_from_its_own_plan():
    ids = definition_ids()
    generated = build_plans()
    housing_plan = commit_every_time(component(generated, "HOUSING")["plan"])

    without = cost_assembly()
    with_plan = cost_assembly(plans={ids["HOUSING"]: housing_plan})

    before = line(without, "HOUSING")
    after = line(with_plan, "HOUSING")
    assert before["operation_extended"] == pytest.approx(0.0)
    assert after["operation_extended"] > 0
    assert after["setup_extended"] > 0
    assert after["run_extended"] > 0
    assert after["extended_cost"] > before["extended_cost"]


def test_setup_and_run_come_back_to_the_operation_cost():
    """A split of a figure the engine produced, not a second formula."""
    ids = definition_ids()
    generated = build_plans()
    plan = commit_every_time(component(generated, "HOUSING")["plan"])
    summary = cost_assembly(plans={ids["HOUSING"]: plan})
    row = line(summary, "HOUSING")

    assert row["setup_extended"] + row["run_extended"] == pytest.approx(
        row["operation_extended"], abs=0.02
    )
    assert summary["setup_cost"] + summary["run_cost"] == pytest.approx(
        summary["operation_cost"], abs=0.02
    )


def test_setup_is_charged_once_per_setup_not_once_per_operation():
    """The Step 21 error, at component level. The housing's plan has several
    operations in one setup, and the setup time must be charged once."""
    ids = definition_ids()
    generated = build_plans()
    plan = commit_every_time(component(generated, "HOUSING")["plan"])
    operation_count = len([o for o in plan["operations"]])
    setup_minutes = plan["setups"][0]["system_setup_time_minutes"]

    summary = cost_assembly(plans={ids["HOUSING"]: plan})
    row = line(summary, "HOUSING")

    assert operation_count > 1
    # Setup time is reported once, not once per operation.
    assert row["setup_time_minutes"] == pytest.approx(setup_minutes)
    assert row["setup_count"] == 1
    assert row["operation_count"] == operation_count


def test_the_roll_up_reports_the_plan_status_beside_the_cost():
    """So "not planned yet" is never read as "needs no machining"."""
    summary = cost_assembly()
    row = line(summary, "HOUSING")

    assert row["plan_status"] == NOT_GENERATED
    assert row["operation_count"] == 0
    assert row["setup_time_minutes"] is None
    assert row["operation_extended"] == pytest.approx(0.0)


def test_a_purchased_component_is_never_given_a_plan_by_the_cost_endpoint():
    ids = definition_ids()
    generated = build_plans([{"definition_id": ids["PIN"], "user_classification": MANUFACTURED}])
    pin_plan = commit_every_time(component(generated, "PIN")["plan"])

    response = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", pump(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "rate_set_id": RATE_SET,
            "components": json.dumps(
                [
                    {
                        "definition_id": ids["PIN"],
                        "user_classification": PURCHASED,
                        "unit_cost": 250.0,
                        "currency": "INR",
                    }
                ]
            ),
            "component_plans": json.dumps({ids["PIN"]: pin_plan}),
        },
    )

    assert response.status_code == 400
    assert "cannot carry a manufacturing plan" in response.json()["detail"]
    assert "Change its classification first" in response.json()["detail"]


def test_a_plan_for_a_component_that_is_not_there_is_refused():
    generated = build_plans()
    plan = component(generated, "HOUSING")["plan"]
    response = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", pump(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "rate_set_id": RATE_SET,
            "component_plans": json.dumps({"no-such-component": plan}),
        },
    )

    assert response.status_code == 400
    assert "no-such-component" in response.json()["detail"]


def test_malformed_plans_are_refused_rather_than_half_applied():
    response = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", pump(), "application/octet-stream")},
        data={"material": "Steel", "component_plans": "{not json"},
    )

    assert response.status_code == 400
    assert "not valid JSON" in response.json()["detail"]


# --------------------------------------------------------------------- quantity


def test_one_plan_per_definition_however_many_occurrences():
    """The housing is placed twice and planned once. Duplicating the plan would
    multiply its setup time by two, which is the Step 21 error again."""
    ids = definition_ids()
    generated = build_plans()
    housing = component(generated, "HOUSING")

    assert housing["quantity"] == 2
    assert len([e for e in generated["plan"]["component_plans"] if e["component_name"] == "HOUSING"]) == 1

    plan = commit_every_time(housing["plan"])
    summary = cost_assembly(plans={ids["HOUSING"]: plan})
    row = line(summary, "HOUSING")

    assert row["quantity"] == 2
    assert row["extended_cost"] == pytest.approx(row["per_unit_cost"] * 2)
    # And the setup time reported is the plan's own, not doubled.
    assert row["setup_time_minutes"] == pytest.approx(
        plan["setups"][0]["system_setup_time_minutes"]
    )


def test_occurrence_and_production_quantity_still_meet_exactly_once():
    ids = definition_ids()
    generated = build_plans()
    plan = commit_every_time(component(generated, "HOUSING")["plan"])

    one = cost_assembly(plans={ids["HOUSING"]: plan})
    ten = cost_assembly(plans={ids["HOUSING"]: plan}, assembly_quantity=10)

    row = line(ten, "HOUSING")
    assert row["quantity"] == 2
    assert row["assembly_quantity"] == 10
    assert row["total_quantity"] == 20
    assert ten["total_cost"] == pytest.approx(one["total_cost"])
    assert ten["production_total_cost"] == pytest.approx(one["total_cost"] * 10)


# --------------------------------------------------------------------- user edits


def test_a_user_time_wins_and_the_system_estimate_survives_beside_it():
    ids = definition_ids()
    generated = build_plans()
    plan = component(generated, "HOUSING")["plan"]
    first = plan["operations"][0]
    system_estimate = first["system_suggested_cycle_time"]
    first["user_cycle_time"] = 9.5
    plan["setups"][0]["user_setup_time_minutes"] = 12.0

    summary = cost_assembly(plans={ids["HOUSING"]: plan})
    row = line(summary, "HOUSING")

    assert row["operation_count"] >= 1
    assert row["setup_time_minutes"] == pytest.approx(12.0)
    assert row["cycle_time_minutes"] == pytest.approx(9.5)
    # The system's own prediction is untouched on the record.
    stored = client.get(f"/estimates/{summary_estimate_id()}").json()
    operations = stored["plan_snapshot"]["assembly_plan"]["component_plans"]
    housing = next(c for c in operations if c["component_name"] == "HOUSING")
    kept = next(o for o in housing["plan"]["operations"] if o["operation_id"] == first["operation_id"])
    assert kept["system_suggested_cycle_time"] == pytest.approx(system_estimate)
    assert kept["user_cycle_time"] == pytest.approx(9.5)


def summary_estimate_id():
    return client.get("/estimates?limit=1").json()["items"][0]["estimate_id"]


def test_changing_a_process_clears_the_accepted_system_time():
    """An estimate made for milling says nothing about turning — the Step 20
    rule, unchanged at component level."""
    ids = definition_ids()
    generated = build_plans()
    plan = component(generated, "HOUSING")["plan"]
    first = commit_every_time(plan)["operations"][0]
    first["user_operation_type"] = "drilling"

    summary = cost_assembly(plans={ids["HOUSING"]: plan})
    row = line(summary, "HOUSING")

    # That operation no longer carries a committed time, so it is left out.
    assert row["operation_count"] == len(plan["operations"])
    assert row["plan_status"] == NEEDS_INPUT


def test_a_rejected_operation_is_left_out_rather_than_zeroed():
    ids = definition_ids()
    generated = build_plans()
    plan = commit_every_time(component(generated, "HOUSING")["plan"])
    plan["operations"][0]["user_action"] = "rejected"

    summary = cost_assembly(plans={ids["HOUSING"]: plan})
    row = line(summary, "HOUSING")

    assert row["operation_count"] == len(plan["operations"]) - 1


def test_a_second_setup_is_charged_a_second_time():
    """Setup membership does change cost, and is meant to."""
    ids = definition_ids()
    generated = build_plans()
    plan = commit_every_time(component(generated, "HOUSING")["plan"])
    one_setup = cost_assembly(plans={ids["HOUSING"]: plan})

    # Split the operations across two setups of the component's own.
    plan["setups"].append(
        {
            "setup_id": "HOUSING/SETUP-002",
            "sequence_position": 2,
            "name": "Side fixture",
            "origin": "user",
            "user_setup_time_minutes": 8.0,
            "system_operation_ids": [],
        }
    )
    plan["operations"][-1]["user_setup_id"] = "HOUSING/SETUP-002"
    two_setups = cost_assembly(plans={ids["HOUSING"]: plan})

    assert line(two_setups, "HOUSING")["setup_count"] == 2
    assert line(two_setups, "HOUSING")["setup_extended"] > line(one_setup, "HOUSING")[
        "setup_extended"
    ]


# --------------------------------------------------------------------- feedback


def test_feedback_carries_the_component_that_produced_it():
    ids = definition_ids()
    generated = build_plans()
    plan = component(generated, "HOUSING")["plan"]
    plan["operations"][0]["user_cycle_time"] = 9.5
    # A process this rate set actually prices, so the correction reaches a cost
    # rather than being refused for want of a rate.
    plan["operations"][0]["user_operation_type"] = "drilling"
    cost_assembly(plans={ids["HOUSING"]: plan})

    stored = client.get(f"/estimates/{summary_estimate_id()}").json()
    feedback = stored["plan_snapshot"]["component_feedback"][ids["HOUSING"]]
    record = next(r for r in feedback if r["operation_id"] == plan["operations"][0]["operation_id"])

    assert record["component_id"] == ids["HOUSING"]
    assert record["component_name"] == "HOUSING"
    assert record["suggested_operation"] is not None
    assert record["final_operation"] == "drilling"
    assert record["operation_changed"] is True
    assert record["final_cycle_time"] == pytest.approx(9.5)
    assert record["cycle_time_changed"] is True


def test_feedback_is_kept_per_component_rather_than_pooled():
    ids = definition_ids()
    generated = build_plans()
    # The housing's times are committed; the pin's are left as generated,
    # because a pin is turned and this rate set prices no turning. Both still
    # have plans, so both still produce feedback — a correction is recorded
    # whether or not the operation reached a cost.
    plans = {
        ids["HOUSING"]: commit_every_time(component(generated, "HOUSING")["plan"]),
        ids["PIN"]: component(generated, "PIN")["plan"],
    }
    cost_assembly(plans=plans)

    stored = client.get(f"/estimates/{summary_estimate_id()}").json()
    feedback = stored["plan_snapshot"]["component_feedback"]

    assert set(feedback) == {ids["HOUSING"], ids["PIN"]}
    for definition_id, records in feedback.items():
        assert records
        assert all(r["component_id"] == definition_id for r in records)


def test_nothing_is_trained_on_the_component_feedback():
    """The suggestion and time rules are fixed. Running them again after a
    correction gives the same prediction."""
    ids = definition_ids()
    first = build_plans()
    plan = component(first, "HOUSING")["plan"]
    original = plan["operations"][0]["system_suggested_cycle_time"]
    plan["operations"][0]["user_cycle_time"] = 99.0
    cost_assembly(plans={ids["HOUSING"]: plan})

    again = build_plans()
    assert component(again, "HOUSING")["plan"]["operations"][0][
        "system_suggested_cycle_time"
    ] == pytest.approx(original)


# --------------------------------------------------------------------- persistence


def test_the_component_plans_travel_in_the_existing_plan_snapshot():
    ids = definition_ids()
    generated = build_plans()
    plan = commit_every_time(component(generated, "HOUSING")["plan"])
    cost_assembly(plans={ids["HOUSING"]: plan})

    stored = client.get(f"/estimates/{summary_estimate_id()}").json()
    snapshot = stored["plan_snapshot"]

    assert "assembly_plan" in snapshot
    housing = next(
        c
        for c in snapshot["assembly_plan"]["component_plans"]
        if c["component_name"] == "HOUSING"
    )
    assert housing["plan"]["operations"]
    assert housing["plan"]["setups"]
    assert housing["time_summary"]["total_setup_time_minutes"] > 0
    assert "never share one" in snapshot["note"]
    # And no second persistence system was created for it.
    assert set(snapshot) <= {
        "assembly_plan",
        "component_feedback",
        "uncosted_operation_ids",
        "note",
    }


def test_a_later_plan_edit_does_not_change_an_earlier_estimate():
    ids = definition_ids()
    generated = build_plans()
    plan = commit_every_time(component(generated, "HOUSING")["plan"])
    first_summary = cost_assembly(plans={ids["HOUSING"]: plan})
    first_id = summary_estimate_id()
    before = client.get(f"/estimates/{first_id}").json()

    # A different plan for the same component, costed again.
    edited = commit_every_time(component(build_plans(), "HOUSING")["plan"])
    edited["operations"][0]["user_cycle_time"] = 99.0
    cost_assembly(plans={ids["HOUSING"]: edited})

    after = client.get(f"/estimates/{first_id}").json()
    assert after["plan_snapshot"] == before["plan_snapshot"]
    assert after["result_snapshot"] == before["result_snapshot"]
    assert after["total_part_cost"] == pytest.approx(first_summary["production_total_cost"])


def test_the_time_profile_behind_the_plans_is_snapshotted():
    ids = definition_ids()
    generated = build_plans(time_profile_id="poc-baseline-time")
    plan = commit_every_time(component(generated, "HOUSING")["plan"])
    cost_assembly(
        plans={ids["HOUSING"]: plan},
        time_profile_id="poc-baseline-time",
    )

    stored = client.get(f"/estimates/{summary_estimate_id()}").json()
    assert "poc-baseline-time" in stored["time_profile_snapshot"]["profiles"]


# --------------------------------------------------------------------- determinism


def _without_timings(value):
    """Everything but the elapsed-time fields, which legitimately vary.

    The same exclusion every determinism test in this suite makes.
    """
    if isinstance(value, dict):
        return {
            key: _without_timings(item)
            for key, item in value.items()
            if key not in ("read_ms", "elapsed_ms", "detect_ms", "parse_ms", "recognition_ms")
        }
    if isinstance(value, list):
        return [_without_timings(item) for item in value]
    return value


def test_building_the_same_component_plans_twice_is_identical():
    first = build_plans()
    second = build_plans()

    assert _without_timings(first) == _without_timings(second)


def test_the_roll_up_serializes_deterministically_with_plans():
    """Same input, same figures — every time.

    `estimate_id` is excluded for the same reason `elapsed_ms` is: each call
    saves its own estimate, so the identifier is per-call by definition. Every
    costed figure beside it must still match exactly.
    """
    ids = definition_ids()
    plan = commit_every_time(component(build_plans(), "HOUSING")["plan"])

    first = cost_assembly(plans={ids["HOUSING"]: plan})
    second = cost_assembly(plans={ids["HOUSING"]: plan})

    assert first["estimate_id"] and second["estimate_id"]
    assert first["estimate_id"] != second["estimate_id"]
    assert {k: v for k, v in first.items() if k != "estimate_id"} == {
        k: v for k, v in second.items() if k != "estimate_id"
    }


# --------------------------------------------------------------------- boundaries


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


def test_the_component_planner_reaches_no_calculator_and_no_repository():
    """`manufacturing_plan` stays the sole bridge to money: this module plans,
    and emits requirements the unchanged costing consumes."""
    import cad_costing_agent.assembly_plan as module

    assert _package_imports_of(module) == {
        "cad_costing_agent.component_classification",
        "cad_costing_agent.manufacturing_plan",
    }


def test_the_costing_layers_still_cannot_reach_the_planner():
    """The boundary in the other direction, which is the one that matters: a
    costing module that could plan could invent an operation."""
    import cad_costing_agent.assembly_costing as module
    import cad_costing_agent.part_cost as part_cost

    assert "cad_costing_agent.assembly_plan" not in _package_imports_of(module)
    assert "cad_costing_agent.assembly_plan" not in _package_imports_of(part_cost)
    assert "cad_costing_agent.manufacturing_plan" not in _package_imports_of(module)


def test_the_planner_calculates_no_money():
    import ast
    import inspect

    import cad_costing_agent.assembly_plan as module

    tree = ast.parse(inspect.getsource(module))
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

    # Money as an identifier, not as a substring: `costable` is about whether an
    # operation has a committed *time*, and forbidding the letters "cost" would
    # ban it along with everything else.
    # Import lines are dropped first: the package is called `cad_costing_agent`,
    # so any substring test for money matches its own name. What this module may
    # import is asserted by the boundary test above.
    code = chr(10).join(
        stripped
        for stripped in code.splitlines()
        if not stripped.startswith(("import ", "from "))
    )

    for forbidden in (
        "_cost",
        "cost_",
        "currency",
        "rate_set",
        "rate_per_hour",
        "Money",
        "round_money",
        "price",
        "material",
    ):
        assert forbidden not in code, forbidden


# --------------------------------------------------------------------- untouched


def test_the_exploded_view_still_works_and_is_unaffected():
    response = client.post(
        "/cad/assembly?measure=true&with_mesh=true",
        files={"file": ("pump.step", pump(), "application/octet-stream")},
    )
    body = response.json()

    assert body["exploded_view"]["enabled"] is True
    assert body["exploded_view"]["group_count"] == 3
    serialized = json.dumps(body["exploded_view"])
    for word in ("plan", "setup", "operation"):
        assert word not in serialized


def test_a_single_part_is_still_planned_the_way_it_always_was():
    """No component apparatus is bolted onto a part."""
    from tests.step_fixtures import block_with_through_hole

    response = client.post(
        "/cad/operations/suggest",
        files={"file": ("hole.step", block_with_through_hole(), "application/octet-stream")},
    )

    assert response.status_code == 200
    for suggestion in response.json()["suggestions"]:
        assert suggestion["component_id"] is None
        assert "/" not in suggestion["feature_id"]


def test_a_part_file_is_refused_by_the_component_plan_endpoint_with_an_explanation():
    from tests.step_fixtures import plain_block

    response = client.post(
        "/cad/assembly/plans",
        files={"file": ("block.step", plain_block(), "application/octet-stream")},
    )

    assert response.status_code == 400
    assert "no component structure" in response.json()["detail"]
    assert "single part is planned through" in response.json()["detail"]


# --------------------------------------------------------------------- the browser


def test_the_assembly_panel_shows_a_plan_status_per_component():
    """Every status the backend can report has a label here, and the panel reads
    the status rather than deciding one — except that a classification changed
    since the plans were built makes a stale "generated" fall back, which is a
    reading of the current classification and not an opinion about the plan."""
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    assert "PLAN_STATUS_LABELS" in source
    assert "entry.status" in source
    for label in (
        "Not generated",
        "Generated",
        "Needs user input",
        "No manufacturing plan",
        "Cannot be planned yet",
    ):
        assert label in source, label


def test_every_backend_plan_status_has_a_label_in_the_browser():
    """A status with no label would render as a bare code on the panel."""
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")
    block = source[source.index("PLAN_STATUS_LABELS = {") : source.index("function componentPlanFor")]

    for status in (NOT_GENERATED, GENERATED, NEEDS_INPUT, NOT_APPLICABLE, BLOCKED):
        assert f"{status}:" in block, status
        assert STATUS_LABELS[status] in block, status


def test_the_browser_computes_no_component_plan_arithmetic():
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    for pattern in (
        r"total_setup_time_minutes\s*[-+*/]",
        r"total_cycle_time_minutes\s*[-+*/]",
        r"\.setup_extended\s*=(?!=)",
        r"\.run_extended\s*=(?!=)",
        r"\.operation_count\s*=(?!=)",
    ):
        assert not re.search(pattern, source), pattern


def test_the_component_plan_reuses_the_existing_operations_table():
    """Twelve editing operations already exist and are tested. Reimplementing
    them for components would be duplication and a place for the system/user
    semantics to drift."""
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    assert "renderPlan(" in source
    assert "state.plan" in source
    assert "state.planComponentId" in source


# --------------------------------------------------------------------- real file


@pytest.mark.skipif(
    not (REAL_MODELS / "Figure 3.stp").is_file(), reason="the real assembly is not present"
)
def test_the_real_assembly_reports_every_component_as_unplannable_with_a_reason():
    """36 part definitions, none of which the file carries geometry for. Every
    one is blocked on the geometry, and not one is planned with zero
    operations."""
    data = (REAL_MODELS / "Figure 3.stp").read_bytes()
    response = client.post(
        "/cad/assembly/plans",
        files={"file": ("Figure 3.stp", data, "application/octet-stream")},
    )

    assert response.status_code == 200
    plan = response.json()["plan"]
    parts = [c for c in plan["component_plans"] if not c["is_assembly"]]
    assert len(parts) == 36
    assert all(c["plan"] is None for c in parts)
    assert all(c["status"] == BLOCKED for c in parts)
    assert all(c["reason"] == NO_GEOMETRY for c in parts)
    assert all(c["operation_count"] if False else True for c in parts)


def test_a_plan_whose_process_the_rate_set_does_not_price_is_refused_with_the_reason():
    """A pin is turned, and this rate set prices only drilling and milling. The
    existing refusal surfaces at component level rather than the operation being
    silently dropped or costed at a rate nobody configured."""
    ids = definition_ids()
    generated = build_plans()
    pin = commit_every_time(component(generated, "PIN")["plan"])
    assert any(o["system_suggested_operation"] == "turning" for o in pin["operations"])

    response = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", pump(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "rate_set_id": RATE_SET,
            "component_plans": json.dumps({ids["PIN"]: pin}),
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "no rate for process type 'turning'" in detail
    assert "drilling, milling" in detail


def test_an_uncommitted_plan_reports_the_estimate_and_says_it_is_one():
    """Found in live verification: a freshly generated plan's *committed* time
    is a genuine 0.00 minutes, which reads exactly like "this component takes no
    time to make". The estimate is reported beside it, labelled."""
    result = build_plans()
    housing = component(result, "HOUSING")

    assert housing["committed_time_is_complete"] is False
    assert housing["time_summary"]["total_setup_time_minutes"] == pytest.approx(0.0)
    assert housing["estimated_setup_time_minutes"] > 0
    assert housing["estimated_cycle_time_minutes"] > 0


def test_a_committed_plan_reports_a_complete_committed_time():
    result = build_plans()
    plan = commit_every_time(component(result, "HOUSING")["plan"])
    ids = definition_ids()
    summary = cost_assembly(plans={ids["HOUSING"]: plan})
    row = line(summary, "HOUSING")

    assert row["plan_status"] == GENERATED
    assert row["setup_time_minutes"] > 0
    assert row["cycle_time_minutes"] > 0


def test_the_panel_never_prints_a_zero_committed_time_as_the_answer():
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    assert "function planMinutes(" in source
    assert "not committed" in source
    assert "min est." in source
    # The committed figure is shown only once the server has reported one on the
    # cost line; before that the estimate is shown, labelled as an estimate.
    assert "state.assemblyCost ? state.assemblyCost.lines : []" in source
    assert "estimated_setup_time_minutes" in source
    assert "estimated_cycle_time_minutes" in source


def test_the_refusal_reads_as_a_sentence():
    """Also found live: reusing a column label as a clause produced "Component
    'HOUSING' is no manufacturing plan and cannot carry a manufacturing plan"."""
    ids = definition_ids()
    generated = build_plans([{"definition_id": ids["PIN"], "user_classification": MANUFACTURED}])
    pin = commit_every_time(component(generated, "PIN")["plan"])

    response = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", pump(), "application/octet-stream")},
        data={
            "material": "Steel",
            "rate_set_id": RATE_SET,
            "components": json.dumps(
                [
                    {
                        "definition_id": ids["PIN"],
                        "user_classification": EXCLUDED,
                    }
                ]
            ),
            "component_plans": json.dumps({ids["PIN"]: pin}),
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "is no manufacturing plan" not in detail
    assert "deliberately excluded from the manufacturing cost" in detail
    assert detail.startswith("Component 'PIN' cannot carry a manufacturing plan:")


def test_an_assembly_quote_shows_the_setup_and_run_split():
    ids = definition_ids()
    plan = commit_every_time(component(build_plans(), "HOUSING")["plan"])
    cost_assembly(plans={ids["HOUSING"]: plan})
    estimate_id = summary_estimate_id()
    document = client.get(f"/estimates/{estimate_id}/quotation").json()

    breakdown = {item["category"]: item["amount"] for item in document["totals"]["breakdown"]}
    assert breakdown["operation_setup"] > 0
    assert breakdown["operation_run"] > 0
    assert breakdown["operation_setup"] + breakdown["operation_run"] == pytest.approx(
        breakdown["operations"], abs=0.02
    )


def test_the_panel_reads_the_plan_as_it_stands_rather_than_the_servers_snapshot():
    """Found in the browser: after accepting every estimate in the operations
    table, the component detail panel still listed the times as missing — the
    same staleness Step 26 hit with its "you confirmed it" label. The resolution
    rule is not reimplemented: `plan.js` owns `finalCycle` and `setupTime`, and
    `assembly.js` is loaded after it."""
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    assert "function livePlanGaps(" in source
    assert "function livePlanStatus(" in source
    # It calls plan.js's own helpers rather than restating the branches.
    assert "finalCycle(operation)" in source
    assert "setupTime(setup)" in source
    # Guarded forward, the way app.js guards its calls into impact.js.
    assert 'typeof finalCycle !== "function"' in source
    # And it never reads the stale server fields for the live view.
    assert "entry.committed_time_is_complete" not in source


def test_plan_js_still_owns_the_time_resolution_rule():
    """The three branches live in one place. A fourth copy would be the thing
    CLAUDE.md warns about."""
    source = (STATIC / "plan.js").read_text(encoding="utf-8")

    assert "function finalCycle(" in source
    assert "function setupTime(" in source
    assert "function timeSource(" in source

    assembly = (STATIC / "assembly.js").read_text(encoding="utf-8")
    # assembly.js must not restate them.
    assert "function finalCycle(" not in assembly
    assert "function setupTime(" not in assembly
    assert "system_time_accepted &&" not in assembly


def test_the_status_column_and_the_detail_panel_cannot_contradict_each_other():
    """Found in the browser: committing every time flipped the detail panel to
    "generated" while the table still read "needs user input", because the
    render hook refreshed only one of the two."""
    plan_source = (STATIC / "plan.js").read_text(encoding="utf-8")
    hook = plan_source[plan_source.index("state.planComponentId && typeof") :][:400]

    assert "renderComponentPlans()" in hook
    assert "renderComponentPlanDetail(state.planComponentId)" in hook

    assembly = (STATIC / "assembly.js").read_text(encoding="utf-8")
    # And the row's reason is shown only while the *live* status still needs it.
    assert 'status === "needs_user_input"' in assembly


def test_a_costed_but_uncommitted_plan_shows_the_estimate_not_a_zero():
    """The other direction of the same trap, found in the browser: a component
    whose plan exists but commits nothing is costed for material only, and the
    roll-up reports a genuine 0.00 minutes for it. Printed as "0 min" that reads
    as "this takes no time to make"."""
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    assert 'livePlanStatus(entry) === "generated" &&' in source


def test_the_roll_up_shows_the_setup_and_run_split():
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    assert "summary.setup_cost" in source
    assert "summary.run_cost" in source


def test_a_component_with_an_uncommitted_plan_is_costed_for_material_only():
    ids = definition_ids()
    generated = build_plans()
    # Sent as generated: a plan exists, nothing in it is committed.
    summary = cost_assembly(plans={ids["COVER"]: component(generated, "COVER")["plan"]})
    row = line(summary, "COVER")

    assert row["plan_status"] == NEEDS_INPUT
    assert row["operation_extended"] == pytest.approx(0.0)
    assert row["material_extended"] > 0
    assert row["setup_time_minutes"] == pytest.approx(0.0)
    assert row["cycle_time_minutes"] == pytest.approx(0.0)


def test_the_plan_table_fits_the_narrow_assembly_column():
    """Measured in the browser: eight columns overflowed the 486 px assembly
    panel by 111 px and pushed the whole page sideways. The same defect the
    classification table hit in Step 26, and the same fix — the counts and
    times go on a full-width sub-row."""
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")
    block = source[source.index("function renderComponentPlans(") : source.index("// What a component looks like")]

    assert "<th>Component</th><th>Classification</th><th>Plan</th></tr>" in block
    assert 'colspan="3"' in block
    assert 'colspan="8"' not in block


# ------------------------------------------- plans arrive with the assembly
#
# A manufactured component's cost is its plan's cost. Before this the plans were
# built only when someone pressed a button whose connection to "Cost every
# component" was nowhere stated, and skipping it produced a complete-looking
# assembly total of material and tooling with nothing for the making.


def test_decomposing_an_assembly_plans_its_manufactured_components():
    """The browser asks for the plans as part of decomposing.

    This is the server half of that: the same endpoint, called with the
    classifications decomposition just suggested, plans every manufactured
    component and nothing else.
    """
    result = build_plans()
    entries = {c["component_name"]: c for c in result["plan"]["component_plans"]}

    for name in ("HOUSING", "COVER", "PIN"):
        assert entries[name]["plan"] is not None, f"{name} is manufactured by default"
        assert entries[name]["plan"]["operations"], f"{name}'s plan has operations"
    assert entries["PUMP"]["plan"] is None, "a sub-assembly is not planned"


@pytest.mark.parametrize(
    "classification,reason",
    [("purchased", "bought"), ("standard", "bought"), ("excluded", "excluded")],
)
def test_only_manufactured_components_are_planned_automatically(classification, reason):
    """Whatever the trigger, eligibility is the classification's answer."""
    ids = definition_ids()
    result = build_plans(
        [{"definition_id": ids["HOUSING"], "user_classification": classification,
          "unit_cost": 10.0, "currency": "INR", "exclusion_reason": "test"}]
    )
    housing = component(result, "HOUSING")

    assert housing["plan"] is None
    assert housing["status"] == "not_applicable"
    assert reason in (housing["reason_detail"] or "").lower()


def test_an_unresolved_component_is_not_planned_automatically():
    ids = definition_ids()
    result = build_plans(
        [{"definition_id": ids["HOUSING"], "user_classification": "unresolved"}]
    )
    housing = component(result, "HOUSING")

    assert housing["plan"] is None
    assert housing["status"] == "blocked"


def test_generated_plans_carry_estimates_and_commit_nothing():
    """Automatic generation is not automatic acceptance.

    Every operation comes back with the system's estimated time and the accept
    flag clear, so the engineer's decision is still the thing that turns an
    estimate into a committed time.
    """
    result = build_plans()
    plan = component(result, "HOUSING")["plan"]

    assert plan["operations"], "there is something to review"
    for operation in plan["operations"]:
        assert operation["system_suggested_cycle_time"] is not None
        assert operation["system_time_accepted"] is False
        assert operation["user_cycle_time"] is None
        assert operation["user_setup_time"] is None
    for setup in plan["setups"]:
        assert setup["system_setup_time_minutes"] is not None
        assert setup["system_setup_time_accepted"] is False
        assert setup["user_setup_time_minutes"] is None


def test_generating_a_plan_does_not_change_the_cost():
    """The number moves when a person commits a time, not when a plan appears."""
    without = cost_assembly()
    with_plans = cost_assembly(
        plans={
            entry["definition_id"]: entry["plan"]
            for entry in build_plans()["plan"]["component_plans"]
            if entry["plan"]
        }
    )

    assert with_plans["operation_cost"] == 0.0
    assert with_plans["total_cost"] == pytest.approx(without["total_cost"])
    assert with_plans["is_complete"] is False
    assert with_plans["unplanned_definition_count"] == without["unplanned_definition_count"]


def test_a_correction_stays_beside_the_system_estimate():
    """An edited time never overwrites what the system proposed."""
    plans = {
        entry["definition_id"]: entry["plan"]
        for entry in build_plans()["plan"]["component_plans"]
        if entry["plan"]
    }
    definition_id_, plan = next(iter(plans.items()))
    proposed = plan["operations"][0]["system_suggested_cycle_time"]
    plan["operations"][0]["user_cycle_time"] = proposed + 5.0

    summary = cost_assembly(plans=plans, rate_set_id="poc-default")
    stored = client.get(f"/estimates/{summary['estimate_id']}").json()
    snapshot = stored["plan_snapshot"]["assembly_plan"]["component_plans"]
    edited = next(
        entry for entry in snapshot if entry["definition_id"] == definition_id_
    )["plan"]["operations"][0]

    assert edited["user_cycle_time"] == pytest.approx(proposed + 5.0)
    assert edited["system_suggested_cycle_time"] == pytest.approx(proposed)


# ---------------------------------------------- what "complete" now requires


def test_an_assembly_with_no_committed_times_is_incomplete():
    """Coverage complete, manufacturing absent. Those are different things."""
    summary = cost_assembly()

    assert summary["costed_definition_count"] == 3
    assert summary["needs_input_definition_count"] == 0, "every definition was accounted for"
    assert summary["unplanned_definition_count"] == 3
    assert summary["operation_cost"] == 0.0
    assert summary["is_complete"] is False
    assert "manufacturing plans incomplete" in summary["completeness_note"]
    for name in ("HOUSING", "COVER", "PIN"):
        assert line(summary, name)["manufacturing_committed"] is False
        assert line(summary, name)["reason"] == "no_committed_operations"


def test_the_partial_total_is_arithmetically_unchanged():
    """Saying it is partial must not alter it by a paisa.

    The material and the tooling are real; what is missing is the making. The
    figure a caller got before this distinction existed is the figure it gets
    now, and the roll-up is still the sum of its lines.
    """
    summary = cost_assembly()
    parts = [item for item in summary["lines"] if not item["is_assembly"]]

    assert summary["total_cost"] == pytest.approx(
        sum(item["extended_cost"] for item in parts)
    )
    assert summary["material_cost"] == pytest.approx(
        sum(item["material_extended"] for item in parts)
    )
    assert summary["additional_cost"] == pytest.approx(
        sum(item["extended_cost"] - item["material_extended"] - item["operation_extended"]
            for item in parts)
    )


def test_committing_the_times_completes_the_assembly():
    """The whole point of the workflow, end to end on the real pipeline."""
    plans = {
        entry["definition_id"]: commit_every_time(entry["plan"])
        for entry in build_plans()["plan"]["component_plans"]
        if entry["plan"]
    }
    partial = cost_assembly(rate_set_id="poc-default")
    complete = cost_assembly(plans=plans, rate_set_id="poc-default")

    assert complete["is_complete"] is True
    assert complete["unplanned_definition_count"] == 0
    assert complete["operation_cost"] > 0
    assert complete["setup_cost"] > 0
    assert complete["run_cost"] > 0
    assert complete["total_cost"] > partial["total_cost"]
    # The material never moved: same components, same geometry, same rates.
    assert complete["material_cost"] == pytest.approx(partial["material_cost"])
    for name in ("HOUSING", "COVER", "PIN"):
        assert line(complete, name)["manufacturing_committed"] is True


def test_a_setup_is_still_charged_once_per_component():
    """Committing times must not reintroduce setup-per-operation charging."""
    plans = {
        entry["definition_id"]: commit_every_time(entry["plan"])
        for entry in build_plans()["plan"]["component_plans"]
        if entry["plan"]
    }
    summary = cost_assembly(plans=plans, rate_set_id="poc-default")

    for name in ("HOUSING", "COVER", "PIN"):
        item = line(summary, name)
        plan = plans[item["definition_id"]]
        charged = item["setup_time_minutes"]
        # One setup's time, once — not once per operation in it.
        assert charged == pytest.approx(
            sum(setup["system_setup_time_minutes"] for setup in plan["setups"])
        )
        assert len(plan["operations"]) > len(plan["setups"])


def test_two_components_still_never_share_a_setup():
    plans = {
        entry["definition_id"]: entry["plan"]
        for entry in build_plans()["plan"]["component_plans"]
        if entry["plan"]
    }
    seen = {}
    for definition_id_, plan in plans.items():
        for setup in plan["setups"]:
            assert setup["setup_id"] not in seen, "a setup belongs to exactly one component"
            seen[setup["setup_id"]] = definition_id_
