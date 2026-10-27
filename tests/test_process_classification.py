"""Manufacturing family and route: recommended from evidence, decided by a person.

What is checked here is the shape of the honesty, not a list of thresholds:

- a recommendation names the features it stands on and never claims `high`;
- a shape another family could also produce is a recommendation with
  alternatives, not a finding;
- thin evidence is an explicit `unknown`, and there is nothing to accept;
- the engineer's answer sits beside the recommendation, never over it;
- a route the existing costing cannot price is refused, not priced as machining;
- and nothing about existing costing changes when no classification is sent.

The fixtures are the generated STEP parts in `tests/step_fixtures.py`. Each is
chosen so its expected family follows from what the part plainly is (a pocketed
block, a stepped shaft, a 2 mm bent bracket), not from where a constant sits.
"""

from __future__ import annotations

import ast
import json
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.features import recognize_features_from_bytes
from cad_costing_agent.features.model import Confidence
from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import ManufacturingPlan, build_plan
from cad_costing_agent.operation_suggestions import UserAction, suggest_operations
from cad_costing_agent.process_classification import (
    ROUTE_CATALOG,
    ManufacturingFamily,
    ProcessClassification,
    classify_process,
    plan_route_differences,
    record_route_decision,
)
from tests import step_fixtures
from tests.step_fixtures import PROCESS_FIXTURES

client = TestClient(app)
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
EVERY_FIXTURE = [*step_fixtures.ALL_FIXTURES, *PROCESS_FIXTURES]

FORM = {
    "material": "Steel",
    "source_unit": "mm",
    "part_id": "P-ROUTE",
    "part_name": "Route part",
    "quantity": "5",
    # poc-default prices turning as well as milling and drilling.
    "rate_set_id": "poc-default",
}


@lru_cache(maxsize=None)
def step(name: str) -> bytes:
    builder = PROCESS_FIXTURES.get(name) or getattr(step_fixtures, name)
    return builder()


@lru_cache(maxsize=None)
def _classified(name: str) -> str:
    recognition = recognize_features_from_bytes(step(name), f"{name}.step")
    return classify_process(recognition, suggest_operations(recognition)).model_dump_json()


def classified(name: str) -> ProcessClassification:
    return ProcessClassification.model_validate_json(_classified(name))


def stage_ids(route) -> list[str]:
    return [stage.stage for stage in route.stages]


def signal(classification: ProcessClassification, signal_id: str):
    return next(s for s in classification.signals if s.signal_id == signal_id)


def suggested(name: str) -> dict:
    response = client.post(
        "/cad/operations/suggest",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def committed_plan(name: str) -> ManufacturingPlan:
    """Every suggested operation with a typed cycle time, so the plan is costable."""
    recognition = recognize_features_from_bytes(step(name), f"{name}.step")
    plan = build_plan(suggest_operations(recognition).suggestions, source_filename=f"{name}.step")
    for operation in plan.operations:
        plan = plan.replacing(operation.with_times(setup_time_minutes=0, cycle_time_minutes=2.0))
    return plan


def cost(name: str, *, plan: ManufacturingPlan | None = None, classification=None):
    data = dict(FORM)
    if plan is not None:
        data["manufacturing_plan"] = plan.model_dump_json()
    if classification is not None:
        data["process_classification"] = (
            classification if isinstance(classification, str) else json.dumps(classification)
        )
    return client.post(
        "/cost/cad-part",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
        data=data,
    )


def decide(classification: dict, action: str, route_id: str | None = None, note=None):
    return client.post(
        "/cad/process/decide",
        json={"classification": classification, "action": action, "route_id": route_id, "note": note},
    )


# --- 1. machining from features cut into the solid ---------------------------


def test_a_pocketed_and_drilled_part_is_recommended_for_milling_and_drilling():
    c = classified("thin_plate_with_pocket")

    assert c.system_family is ManufacturingFamily.MACHINING
    assert stage_ids(c.system_route) == ["milling", "drilling"]
    assert c.system_status == "recommended"
    # The features it stood on are named, by id, in the signal and the stages.
    machining = signal(c, "machining_features")
    assert machining.observed is True and "PKT-001" in machining.detail
    assert any("H-00" in line for line in c.system_route.stages[1].basis)


def test_a_counterbored_block_is_machining_at_medium_and_never_higher():
    c = classified("block_with_counterbore")
    assert c.system_family is ManufacturingFamily.MACHINING
    assert c.system_confidence is Confidence.MEDIUM
    assert any("capped at medium" in line for line in c.evidence)


@pytest.mark.parametrize("name", EVERY_FIXTURE)
def test_no_fixture_is_ever_classified_with_high_confidence(name):
    assert classified(name).system_confidence is not Confidence.HIGH


def test_through_holes_alone_are_machining_at_low_confidence():
    # A drilled block is what a cast or cut blank also carries: nothing was cut
    # *into* the solid, so the recommendation stays low.
    c = classified("block_with_through_hole")
    assert c.system_family is ManufacturingFamily.MACHINING
    assert c.system_confidence is Confidence.LOW


# --- 2. turning only when the part is rotational ------------------------------


def test_a_stepped_shaft_is_recommended_for_turning():
    c = classified("stepped_shaft")
    assert c.part_form == "rotational"
    assert stage_ids(c.system_route) == ["turning"]
    assert signal(c, "rotational_form").observed is True


def test_an_external_cylinder_on_a_prismatic_part_does_not_make_a_turning_route():
    # The per-feature rule still suggests turning for the Ø18 boss; the route
    # does not, because the plate's side faces are not square to the boss axis.
    recognition = recognize_features_from_bytes(step("plate_with_boss_and_bores"), "boss.step")
    suggestions = suggest_operations(recognition)
    assert "turning" in suggestions.suggestion_counts

    c = classify_process(recognition, suggestions)
    assert c.part_form == "prismatic"
    assert "turning" not in stage_ids(c.system_route)
    assert signal(c, "rotational_form").observed is False
    milling = next(s for s in c.system_route.stages if s.stage == "milling")
    assert any("not rotational" in line for line in milling.basis)


def test_a_block_with_no_cylinder_cannot_be_assessed_for_rotation():
    assert signal(classified("block_with_pocket"), "rotational_form").observed is None


# --- 3. another family's shape is a recommendation, not proof -----------------


def test_a_constant_thickness_bracket_is_a_low_confidence_sheet_recommendation():
    c = classified("bent_sheet_bracket")

    assert c.system_family is ManufacturingFamily.SHEET_METAL
    assert c.system_confidence is Confidence.LOW
    assert c.decision_status == "awaiting_review"
    assert "recommendation, not a finding" in c.system_reason
    # Machining is offered beside it, because a plate can be machined to the same shape.
    assert ManufacturingFamily.MACHINING in {candidate.family for candidate in c.candidates}
    assert any("sheet or machined" in need for need in c.needs_information)


def test_a_pocket_rules_a_sheet_route_out_even_on_a_constant_wall():
    c = classified("thin_plate_with_pocket")
    assert signal(c, "constant_thickness").observed is True
    assert c.system_family is ManufacturingFamily.MACHINING
    assert ManufacturingFamily.SHEET_METAL in {candidate.family for candidate in c.candidates}


@pytest.mark.parametrize("name", EVERY_FIXTURE)
def test_families_with_no_detectable_evidence_are_never_recommended(name):
    never = {
        ManufacturingFamily.CASTING,
        ManufacturingFamily.FORGING,
        ManufacturingFamily.MOULDING,
        ManufacturingFamily.ADDITIVE,
        ManufacturingFamily.PURCHASED,
        ManufacturingFamily.FABRICATION_WELDING,
    }
    c = classified(name)
    assert c.system_family not in never
    assert any("not assessed" in line for line in c.not_assessed)


def test_separate_solids_are_offered_as_a_possible_weldment_not_called_one():
    c = classified("two_solids")
    assert c.system_family is not ManufacturingFamily.FABRICATION_WELDING
    assert c.system_confidence is Confidence.LOW
    assert ManufacturingFamily.FABRICATION_WELDING in {candidate.family for candidate in c.candidates}
    assert any("solids are joined" in need for need in c.needs_information)


# --- 4. thin evidence is an explicit unknown ---------------------------------


@pytest.mark.parametrize("name", ["plain_block", "block_with_fillet", "block_with_chamfer"])
def test_faces_and_blends_alone_are_unknown_with_what_is_needed(name):
    c = classified(name)
    assert c.system_family is ManufacturingFamily.UNKNOWN
    assert c.system_route is None
    assert c.system_status == "insufficient_evidence"
    assert c.decision_status == "insufficient_evidence"
    assert c.needs_information


def test_there_is_nothing_to_accept_when_nothing_was_recommended():
    with pytest.raises(ValueError, match="nothing to accept"):
        record_route_decision(classified("plain_block"), UserAction.ACCEPTED)


def test_an_unreviewed_unknown_does_not_block_the_engineers_own_operations():
    c = classified("plain_block")
    assert c.costing_status == "route_not_established"
    assert c.blocks_costing is False


def test_a_hole_the_suggestions_declined_is_named_rather_than_dropped():
    c = classified("stepped_shaft_with_cross_hole")
    assert stage_ids(c.system_route) == ["turning"]
    assert any("H-001" in line and "does not include drilling" in line for line in c.evidence)


# --- 5. several stages --------------------------------------------------------


def test_a_bent_bracket_is_a_two_stage_cut_and_bend_route():
    route = classified("bent_sheet_bracket").system_route
    assert route.route_id == "sheet_cutting_bending"
    assert stage_ids(route) == ["sheet_cutting", "bending"]


def test_a_flat_blank_has_no_bending_stage():
    assert classified("flat_sheet_blank").system_route.route_id == "sheet_cutting"


def test_a_shaft_with_an_axial_hole_is_turned_then_drilled():
    assert stage_ids(classified("stepped_shaft_with_axial_hole").system_route) == [
        "turning",
        "drilling",
    ]


def test_a_chosen_multi_stage_route_reports_each_stage_and_its_cost_model():
    c = record_route_decision(
        classified("block_with_counterbore"), UserAction.EDITED, route_id="casting_machining"
    )
    assert stage_ids(c.route) == ["casting", "milling", "drilling"]
    assert c.stage_rate_stages == ["Casting"]
    assert [stage.cost_model_available for stage in c.route.stages] == [False, True, True]
    # Decided, so it is priced from stage rates at costing time rather than refused here.
    assert c.costing_status == "stage_rates_required"
    assert c.blocks_costing is False


# --- 6. the engineer's answer sits beside the recommendation ------------------


def test_an_override_keeps_the_recommendation_unchanged():
    original = classified("block_with_counterbore")
    decided = record_route_decision(
        original, UserAction.EDITED, route_id="forging_machining", note="forged blank from supplier"
    )
    assert decided.system_route == original.system_route
    assert decided.system_family is original.system_family
    assert decided.user_route.route_id == "forging_machining"
    assert decided.family is ManufacturingFamily.FORGING
    assert decided.decision_status == "overridden"
    assert decided.user_note == "forged blank from supplier"


def test_choosing_the_recommended_route_is_an_acceptance_not_an_override():
    original = classified("block_with_counterbore")
    decided = record_route_decision(
        original, UserAction.EDITED, route_id=original.system_route.route_id
    )
    assert decided.user_action is UserAction.ACCEPTED
    assert decided.user_route is None
    assert decided.decision_status == "confirmed"


def test_marking_unknown_and_withdrawing_both_round_trip():
    original = classified("block_with_counterbore")
    unknown = record_route_decision(original, UserAction.EDITED, route_id="unknown")
    assert unknown.decision_status == "marked_unknown"
    assert unknown.costing_status == "route_unknown"
    withdrawn = record_route_decision(unknown, UserAction.PENDING)
    assert withdrawn.decision_status == "awaiting_review"
    assert withdrawn.system_route == original.system_route


@pytest.mark.parametrize("action", [UserAction.REJECTED, UserAction.ADDED])
def test_actions_that_are_not_route_decisions_are_refused(action):
    with pytest.raises(ValueError):
        record_route_decision(classified("block_with_counterbore"), action)


def test_the_decide_endpoint_applies_the_decision_and_refuses_an_unknown_route():
    classification = suggested("block_with_counterbore")["process_classification"]
    accepted = decide(classification, "accepted")
    assert accepted.status_code == 200
    assert accepted.json()["decision_status"] == "confirmed"
    assert accepted.json()["system_route"] == classification["system_route"]

    refused = decide(classification, "edited", route_id="laser_unicorn")
    assert refused.status_code == 400
    assert "Unknown route" in refused.json()["detail"]


def test_the_route_catalog_is_served_and_marks_which_stages_can_be_costed():
    catalog = client.get("/process-routes").json()
    assert {route["route_id"] for route in catalog["routes"]} == set(ROUTE_CATALOG)
    costable = {
        stage["stage"]
        for route in catalog["routes"]
        for stage in route["stages"]
        if stage["cost_model_available"]
    }
    assert costable == {"milling", "drilling", "turning"}
    assert {family["family"] for family in catalog["families"]} == {
        family.value for family in ManufacturingFamily
    }


# --- 7. an unsupported route gets no estimate ---------------------------------


def _estimate_count() -> int:
    return client.get("/estimates").json()["total"]


@pytest.mark.parametrize(
    "route_id, error",
    [
        # The seeded rate set has no stage rates, so a decided route with a
        # non-machining stage cannot be priced; a route marked unknown never can.
        ("casting_machining", "route_stages_not_priceable"),
        ("sheet_cutting_bending", "route_stages_not_priceable"),
        ("purchased", "route_stages_not_priceable"),
        ("unknown", "process_route_not_costable"),
    ],
)
def test_a_route_the_costing_cannot_price_is_refused_and_nothing_is_stored(route_id, error):
    name = "block_with_counterbore"
    classification = decide(suggested(name)["process_classification"], "edited", route_id).json()
    before = _estimate_count()

    response = cost(name, plan=committed_plan(name), classification=classification)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == error
    assert _estimate_count() == before


def test_an_unreviewed_sheet_recommendation_is_refused_until_someone_answers():
    classification = suggested("bent_sheet_bracket")["process_classification"]
    response = cost("bent_sheet_bracket", classification=classification)
    assert response.status_code == 422
    assert "has not been reviewed" in response.json()["detail"]["message"]

    machined = decide(classification, "edited", "milling_drilling").json()
    assert cost("bent_sheet_bracket", classification=machined).status_code == 200


def test_a_browser_cannot_declare_its_own_route_costable():
    # Computed fields in the payload are ignored and recomputed on the server.
    classification = decide(
        suggested("block_with_counterbore")["process_classification"], "edited", "casting"
    ).json()
    classification["blocks_costing"] = False
    classification["costing_status"] = "machining_supported"
    classification["stage_rate_stages"] = []
    # Nor by claiming the casting stage is machined: the catalog decides by stage id.
    for stage in classification["user_route"]["stages"]:
        stage["process_type"] = "milling"
        stage["cost_model_available"] = True

    assert cost("block_with_counterbore", classification=classification).status_code == 422


# --- 8. existing costing is unchanged ----------------------------------------


def test_a_confirmed_machining_route_costs_exactly_what_no_route_costs():
    name = "block_with_counterbore"
    plan = committed_plan(name)
    confirmed = decide(suggested(name)["process_classification"], "accepted").json()

    without = cost(name, plan=plan)
    with_route = cost(name, plan=plan, classification=confirmed)

    assert without.status_code == with_route.status_code == 200
    plain, routed = without.json()["part_cost"], with_route.json()["part_cost"]
    assert routed["total_part_cost"] == plain["total_part_cost"]
    assert routed["total_operation_cost"] == plain["total_operation_cost"]
    assert routed["total_material_cost"] == plain["total_material_cost"]


def test_a_snapshot_without_a_route_has_no_route_keys_at_all():
    response = cost("block_with_counterbore", plan=committed_plan("block_with_counterbore"))
    stored = client.get(f"/estimates/{response.json()['estimate_id']}").json()
    assert "process_classification" not in stored["request_snapshot"]
    assert "process_route_differences" not in stored["request_snapshot"]


def test_the_suggestions_themselves_are_unchanged_by_classification():
    name = "plate_with_boss_and_bores"
    recognition = recognize_features_from_bytes(step(name), f"{name}.step")
    direct = suggest_operations(recognition).model_dump(mode="json", exclude={"elapsed_ms"})
    served = suggested(name)
    served.pop("elapsed_ms")
    served.pop("process_classification")
    served.pop("raw_stock")
    assert served == direct


# --- 9. the decision is stored with the estimate, deterministically -----------


def test_the_recommendation_and_the_override_are_both_stored_on_the_estimate():
    name = "block_with_counterbore"
    classification = decide(
        suggested(name)["process_classification"],
        "edited",
        "turning_milling_drilling",
        note="turned on the mill-turn",
    ).json()

    response = cost(name, plan=committed_plan(name), classification=classification)
    assert response.status_code == 200
    stored = client.get(f"/estimates/{response.json()['estimate_id']}").json()["request_snapshot"]

    snapshot = stored["process_classification"]
    assert snapshot["system_route"]["route_id"] == "milling_drilling"
    assert snapshot["user_route"]["route_id"] == "turning_milling_drilling"
    assert snapshot["user_action"] == "edited"
    assert snapshot["user_note"] == "turned on the mill-turn"
    assert snapshot["costing_status"] == "machining_supported"
    # The plan costs no turning, and that disagreement is recorded, not corrected.
    assert stored["process_route_differences"] == [
        "The route 'Turning + milling + drilling' includes turning, but the plan costs no turning "
        "operation."
    ]


def test_plan_route_differences_name_both_directions():
    assert plan_route_differences(classified("stepped_shaft"), ["turning", "milling"]) == [
        "The plan costs milling operations, which the route 'CNC turning' does not include."
    ]
    assert plan_route_differences(classified("plain_block"), ["milling"]) == []


@pytest.mark.parametrize("name", ["bent_sheet_bracket", "stepped_shaft", "block_with_counterbore"])
def test_classification_is_deterministic(name):
    first = suggested(name)
    second = suggested(name)
    first.pop("elapsed_ms")
    second.pop("elapsed_ms")
    assert first == second


# --- 10. invalid and missing geometry ----------------------------------------


def test_an_stl_upload_is_refused_for_classification_as_it_is_for_features():
    stl = (
        b"solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
        b"endloop\nendfacet\nendsolid t\n"
    )
    response = client.post(
        "/cad/operations/suggest", files={"file": ("part.stl", stl, "application/octet-stream")}
    )
    assert response.status_code == 400


def test_an_unreadable_file_is_a_400_not_a_classification():
    response = client.post(
        "/cad/operations/suggest",
        files={"file": ("broken.step", b"ISO-10303-21;\nnot really\n", "application/octet-stream")},
    )
    assert response.status_code == 400


def test_a_malformed_classification_is_refused_with_a_400():
    response = cost("block_with_counterbore", classification="{not json")
    assert response.status_code == 400
    assert "process_classification" in response.json()["detail"]


def test_a_recognition_with_no_features_is_unknown_not_an_error():
    recognition = recognize_features_from_bytes(step("plain_block"), "p.step")
    empty = recognition.model_copy(update={"features": [], "feature_counts": {}})
    c = classify_process(empty, suggest_operations(empty))
    assert c.system_family is ManufacturingFamily.UNKNOWN
    assert c.system_rule == "no_features_unclassified"


# --- boundaries ---------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_classifier_imports_no_costing_and_only_main_imports_it():
    own = {
        module
        for module in _imports(PACKAGE / "process_classification.py")
        if module.startswith("cad_costing_agent")
    }
    assert own == {
        "cad_costing_agent.features.model",
        "cad_costing_agent.features.rotational",
        "cad_costing_agent.operation_suggestions",
    }

    readers = {
        path.name
        for path in PACKAGE.rglob("*.py")
        if "cad_costing_agent.process_classification" in _imports(path)
    }
    assert readers == {"main.py"}


def test_a_surface_mode_iges_export_is_weaker_evidence_and_says_why_in_its_own_terms():
    from tests import iges_fixtures

    solid = recognize_features_from_bytes(iges_fixtures.block_with_hole_solid(), "b.igs")
    surfaces = recognize_features_from_bytes(iges_fixtures.block_with_hole_surfaces(), "b.igs")
    assert surfaces.representation == "surface_model"

    from_surfaces = classify_process(surfaces, suggest_operations(surfaces))
    exact = signal(from_surfaces, "exact_geometry")
    assert exact.observed is False
    assert "trimmed surfaces" in exact.detail and "polygons" not in exact.detail
    assert from_surfaces.system_confidence is Confidence.LOW
    assert signal(classify_process(solid, suggest_operations(solid)), "exact_geometry").observed is True


# --- 11. a decided route regroups the plan -----------------------------------
#
# The plan_with_boss_and_bores part suggests milling, turning (the boss) and
# drilling, so a route that leaves one process out has something to leave out.

BOSS = "plate_with_boss_and_bores"


@pytest.mark.parametrize(
    ("name", "route_id", "expected"),
    [
        (BOSS, None, None),
        (BOSS, "milling_drilling", ["milling", "drilling"]),
        (BOSS, "casting_machining", ["milling", "drilling"]),
        (BOSS, "turning_drilling", ["turning", "drilling"]),
        (BOSS, "sheet_cutting_bending", []),
        (BOSS, "purchased", []),
        (BOSS, "unknown", []),
        ("plain_block", None, None),
    ],
)
def test_plan_processes_follow_the_decided_route_and_nothing_else(name, route_id, expected):
    c = classified(name)
    if route_id is not None:
        c = record_route_decision(c, UserAction.EDITED, route_id=route_id)
    assert c.plan_processes == expected


def test_an_accepted_recommendation_sets_the_plan_processes_too():
    c = record_route_decision(classified(BOSS), UserAction.ACCEPTED)
    assert c.plan_processes == ["milling", "drilling"]


def _types(requirements) -> set[str]:
    return {requirement.process_type for requirement in requirements}


def test_with_no_route_every_operation_counts_exactly_as_before():
    plan = committed_plan(BOSS)
    assert plan.route_processes is None
    assert _types(plan.to_operation_requirements(1)) == {"milling", "turning", "drilling"}
    assert plan.out_of_route_operations() == []
    assert plan.time_summary().out_of_route_operation_ids == []


def test_operations_outside_the_route_are_kept_but_not_costed_or_called_missing():
    plan = committed_plan(BOSS)
    routed = plan.model_copy(update={"route_processes": ["milling", "drilling"]})

    turning = [o.operation_id for o in plan.active if o.operation_type == "turning"]
    assert turning
    assert _types(routed.to_operation_requirements(1)) == {"milling", "drilling"}
    assert [o.operation_id for o in routed.out_of_route_operations()] == turning
    assert routed.uncosted_operations() == []
    assert routed.time_summary().out_of_route_operation_ids == turning
    # Kept whole: nothing about the operation itself changed.
    assert [o.model_dump() for o in routed.operations] == [o.model_dump() for o in plan.operations]


def test_an_empty_route_costs_no_operation_and_switching_back_restores_all():
    plan = committed_plan(BOSS)
    empty = plan.model_copy(update={"route_processes": []})
    assert empty.to_operation_requirements(1) == []
    assert len(empty.out_of_route_operations()) == len(plan.active)

    restored = empty.model_copy(update={"route_processes": None})
    assert restored.to_operation_requirements(1) == plan.to_operation_requirements(1)


def test_changing_an_operations_process_into_the_route_brings_it_back():
    plan = committed_plan(BOSS).model_copy(update={"route_processes": ["milling", "drilling"]})
    turning = next(o for o in plan.active if o.operation_type == "turning")
    moved = plan.replacing(turning.with_operation_type("milling").with_times(cycle_time_minutes=3.0))
    assert moved.out_of_route_operations() == []
    assert turning.operation_id in {r.operation_id for r in moved.to_operation_requirements(1)}


def test_a_setups_time_is_still_charged_once_when_its_first_operation_is_out_of_route():
    plan = committed_plan(BOSS)
    plan = plan.replacing_setup(plan.setups[0].with_setup_time(10.0))
    first = plan.ordered[0]
    routed = plan.model_copy(
        update={"route_processes": [p for p in ("milling", "turning", "drilling") if p != first.operation_type]}
    )
    requirements = routed.to_operation_requirements(1)
    assert first.operation_id not in {r.operation_id for r in requirements}
    assert sum(r.setup_time_minutes for r in requirements) == 10.0


def test_a_decided_route_decides_which_operations_are_costed_through_the_api():
    plan = committed_plan(BOSS)
    accepted = decide(suggested(BOSS)["process_classification"], "accepted").json()
    assert accepted["plan_processes"] == ["milling", "drilling"]

    routed = cost(BOSS, plan=plan, classification=accepted)
    # The same plan with its turning lines removed by hand, and no route at all.
    by_hand = plan
    for operation in plan.active:
        if operation.operation_type == "turning":
            by_hand = by_hand.replacing(operation.rejected())
    manual = cost(BOSS, plan=by_hand)

    assert routed.status_code == manual.status_code == 200
    assert routed.json()["part_cost"]["total_part_cost"] == manual.json()["part_cost"]["total_part_cost"]
    stored = client.get(f"/estimates/{routed.json()['estimate_id']}").json()
    turning = [o.operation_id for o in plan.active if o.operation_type == "turning"]
    assert stored["plan_snapshot"]["out_of_route_operation_ids"] == turning
    assert stored["plan_snapshot"]["plan"]["route_processes"] == ["milling", "drilling"]


def test_the_server_applies_the_route_from_the_classification_not_the_browser():
    plan = committed_plan(BOSS)
    accepted = decide(suggested(BOSS)["process_classification"], "accepted").json()
    honest = cost(BOSS, plan=plan, classification=accepted)
    # A plan claiming a different route is costed under the classification's route.
    claimed = cost(
        BOSS, plan=plan.model_copy(update={"route_processes": ["turning"]}), classification=accepted
    )
    assert honest.json()["part_cost"]["total_part_cost"] == claimed.json()["part_cost"]["total_part_cost"]


def test_an_unreviewed_recommendation_leaves_the_plan_alone():
    plan = committed_plan(BOSS)
    pending = suggested(BOSS)["process_classification"]
    assert pending["plan_processes"] is None
    with_route = cost(BOSS, plan=plan, classification=pending)
    without = cost(BOSS, plan=plan)
    assert with_route.json()["part_cost"]["total_part_cost"] == without.json()["part_cost"]["total_part_cost"]
    stored = client.get(f"/estimates/{without.json()['estimate_id']}").json()
    assert "out_of_route_operation_ids" not in stored["plan_snapshot"]


def test_the_browser_plan_builder_copies_the_route_to_the_server():
    source = (PACKAGE / "static" / "plan.js").read_text(encoding="utf-8")
    payload = source[source.index("function planPayload()"):]
    assert "route_processes: routeProcesses()" in payload


# --- 12. a cast or forged blank leaves its blends as formed -------------------
#
# Choosing "Casting followed by CNC machining" used to change nothing in the
# plan: milling and drilling are in the route, so every fillet stayed a milling
# line to be removed by hand.

from cad_costing_agent.operation_feedback import feedback_from_plan  # noqa: E402

FILLETED = "block_with_fillet"


@pytest.mark.parametrize(
    ("route_id", "expected", "label"),
    [
        ("casting_machining", ["fillet", "chamfer"], "As cast"),
        ("forging_machining", ["fillet", "chamfer"], "As forged"),
        ("milling_drilling", None, None),
        ("casting", None, None),  # nothing is machined: the route alone leaves it out
        ("sheet_cutting_bending", None, None),
    ],
)
def test_only_a_cast_or_forged_then_machined_route_leaves_features_as_formed(route_id, expected, label):
    c = record_route_decision(classified(BOSS), UserAction.EDITED, route_id=route_id)
    assert c.as_formed_feature_types == expected
    assert c.as_formed_label == label


def test_an_unreviewed_recommendation_leaves_nothing_as_formed():
    assert classified(BOSS).as_formed_feature_types is None


def filleted_plan(route_id: str = "casting_machining"):
    c = record_route_decision(classified(FILLETED), UserAction.EDITED, route_id=route_id)
    plan = committed_plan(FILLETED)
    return plan.model_copy(
        update={"route_processes": c.plan_processes, "as_formed_feature_types": c.as_formed_feature_types}
    )


def test_a_casting_route_leaves_the_fillet_as_cast_and_keeps_it():
    plan = filleted_plan()
    fillet = next(o for o in plan.active if o.feature_type == "fillet")
    assert plan.is_as_formed(fillet)
    assert [o.operation_id for o in plan.as_formed_operations()] == [fillet.operation_id]
    assert fillet.operation_id not in {r.operation_id for r in plan.to_operation_requirements(1)}
    assert fillet.operation_id not in {o.operation_id for o in plan.uncosted_operations()}
    assert plan.time_summary().as_formed_operation_ids == [fillet.operation_id]
    # Faces stay machined by default: the geometry cannot tell a datum from a cast wall.
    assert any(o.feature_type == "plane" for o in plan.active if plan.counts_toward_cost(o))


def test_the_engineer_can_machine_a_blend_or_leave_a_face_as_cast():
    plan = filleted_plan()
    fillet = next(o for o in plan.active if o.feature_type == "fillet")
    face = next(o for o in plan.active if o.feature_type == "plane")
    plan = plan.replacing(fillet.model_copy(update={"user_machined": True}))
    plan = plan.replacing(face.model_copy(update={"user_machined": False}))
    costed = {r.operation_id for r in plan.to_operation_requirements(1)}
    assert fillet.operation_id in costed
    assert face.operation_id not in costed
    assert face.operation_id in {o.operation_id for o in plan.as_formed_operations()}


def test_on_a_machining_route_nothing_is_left_as_formed_whatever_a_row_says():
    plan = filleted_plan("milling_drilling")
    face = next(o for o in plan.active if o.feature_type == "plane")
    plan = plan.replacing(face.model_copy(update={"user_machined": False}))
    assert plan.as_formed_operations() == []
    assert face.operation_id in {r.operation_id for r in plan.to_operation_requirements(1)}


def test_the_preview_reports_what_was_left_as_formed():
    plan = filleted_plan()
    response = client.post(
        "/operations/plan-cost-preview?rate_set_id=poc-default&quantity=1",
        json=plan.model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["time"]["as_formed_operation_ids"]) == 1


def test_feedback_records_what_the_plan_actually_costed():
    """It used to say `costed: true` for operations the route or the blank left out."""
    route_out = committed_plan(BOSS).model_copy(update={"route_processes": ["milling", "drilling"]})
    for record in feedback_from_plan(route_out):
        operation = route_out.get(record.operation_id)
        assert record.costed == route_out.counts_toward_cost(operation)
    assert not all(record.costed for record in feedback_from_plan(route_out))

    as_cast = filleted_plan()
    fillet = next(o for o in as_cast.active if o.feature_type == "fillet")
    record = next(r for r in feedback_from_plan(as_cast) if r.operation_id == fillet.operation_id)
    assert record.costed is False


def test_the_browser_mirrors_and_sends_the_as_formed_default():
    source = (PACKAGE / "static" / "plan.js").read_text(encoding="utf-8")
    payload = source[source.index("function planPayload()"):]
    assert "as_formed_feature_types: asFormedTypes()" in payload
    assert "function isAsFormed(operation)" in source
