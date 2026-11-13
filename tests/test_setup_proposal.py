"""Setups proposed from tool approach directions, with a straight-ray access check.

The expected answers below are stated from how each fixture is built
(`tests/approach_fixtures.py`), not read back from the code: a blind hole on top
is reached along +Z and nowhere else, a hole under an overhanging flange is
blocked along +Z by material exactly 30 mm up, and so on.

What is defended, in order:

1. the approach directions and the access check, per fixture;
2. the proposal's exact grouping, its determinism and its confidence cap;
3. that nothing changes until a person applies it — the default plan stays
   byte-for-byte what `build_plan` produces;
4. that applying writes `user_setup_id` beside an untouched `system_setup_id`,
   records where the grouping came from, invents no time, and moves the cost by
   exactly the extra setup's committed time at the machine rate;
5. refusals for geometry with no exact faces, per-component scope, and the
   module boundaries.
"""

from __future__ import annotations

import ast
import json
import re
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.features.approach import (
    ApproachUnavailable,
    approach_directions,
    shape_and_features,
)
from cad_costing_agent.features.model import Confidence
from cad_costing_agent.main import STATIC_PATH, app
from cad_costing_agent.manufacturing_plan import (
    DEFAULT_SETUP_ID,
    ManufacturingPlan,
    ManufacturingSetup,
    PlannedOperation,
    build_plan,
)
from cad_costing_agent.operation_feedback import feedback_from_plan
from cad_costing_agent.operation_suggestions import UserAction, suggest_operations
from cad_costing_agent.setup_proposal import (
    APPROACH_GROUPING,
    SetupProposal,
    apply_setup_proposal,
    features_to_analyse,
    propose_setups,
)
from tests import approach_fixtures, faceted_fixtures, iges_fixtures, step_fixtures
from tests.assembly_fixtures import pump_assembly

client = TestClient(app)

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"

# poc-default's drilling machine rate, to check the setup arithmetic by hand.
DRILLING_MACHINE_RATE = 1800.0


@lru_cache(maxsize=None)
def fixture_bytes(name: str) -> bytes:
    source = getattr(approach_fixtures, name, None) or getattr(step_fixtures, name)
    return source()


@lru_cache(maxsize=None)
def _analysed(name: str):
    shape, recognition = shape_and_features(fixture_bytes(name), "step")
    plan = build_plan(
        suggest_operations(recognition).suggestions,
        units=recognition.units,
        source_filename=f"{name}.step",
    )
    return shape, recognition, plan.model_dump_json()


def full_plan(name: str) -> ManufacturingPlan:
    return ManufacturingPlan.model_validate_json(_analysed(name)[2])


def holes_only(name: str) -> ManufacturingPlan:
    """The plan with every facing operation removed, as an engineer would remove them."""
    plan = full_plan(name)
    return plan.model_copy(
        update={
            "operations": [
                o if o.feature_type == "hole" else o.rejected() for o in plan.operations
            ]
        }
    )


def approaches(name: str):
    shape, recognition, _ = _analysed(name)
    return {a.feature_id: a for a in approach_directions(shape, list(recognition.features))}


def propose(name: str, plan: ManufacturingPlan) -> SetupProposal:
    shape, recognition, _ = _analysed(name)
    return propose_setups(
        plan, approach_directions(shape, features_to_analyse(plan, list(recognition.features)))
    )


def grouping(proposal: SetupProposal) -> list[tuple[str, list[str]]]:
    return [(s.direction_key, s.feature_ids) for s in proposal.setups]


def post_propose(name: str, plan: ManufacturingPlan, path: str = "/cad/setups/propose", **data):
    return client.post(
        path,
        files={"file": (f"{name}.step", fixture_bytes(name), "application/octet-stream")},
        data={"manufacturing_plan": plan.model_dump_json(), **data},
    )


# ----------------------------------------------------------- 1. approach directions


@pytest.mark.parametrize(
    "fixture, feature_id, reachable, blocked",
    [
        ("block_with_top_holes", "H-001", ["+Z"], []),
        ("block_with_top_holes", "H-002", ["+Z"], []),
        ("block_with_top_and_side_holes", "H-001", ["+Z"], []),
        ("block_with_top_and_side_holes", "H-002", ["+X"], []),
        ("block_with_top_and_bottom_holes", "H-001", ["+Z"], []),
        ("block_with_top_and_bottom_holes", "H-002", ["-Z"], []),
        ("block_with_through_hole", "H-001", ["+Z", "-Z"], []),
        ("bracket_with_overhung_through_hole", "H-001", ["-Z"], ["+Z"]),
        ("bracket_with_unreachable_blind_hole", "H-001", [], ["+Z"]),
    ],
)
def test_each_hole_is_approached_only_from_its_open_ends(fixture, feature_id, reachable, blocked):
    approach = approaches(fixture)[feature_id]

    assert [c.direction_key for c in approach.candidates if c.reachable] == reachable
    assert [c.direction_key for c in approach.candidates if not c.reachable] == blocked
    assert all(c.kind == "primary" for c in approach.candidates)


def test_a_blind_hole_is_approached_along_its_floors_outward_normal():
    """Read from the B-Rep: the floor's normal points out through the open end."""
    approach = approaches("block_with_top_and_bottom_holes")
    top, bottom = approach["H-001"], approach["H-002"]

    assert top.rule == bottom.rule == "blind_hole_open_end"
    assert top.candidates[0].direction.as_tuple() == (0.0, 0.0, 1.0)
    assert bottom.candidates[0].direction.as_tuple() == (0.0, 0.0, -1.0)
    assert "floor of this blind hole" in top.candidates[0].basis
    # The rays start at the open end: z = 20 on top, z = 0 underneath, lifted 0.01 mm.
    assert top.candidates[0].origin.z == pytest.approx(20.01)
    assert bottom.candidates[0].origin.z == pytest.approx(-0.01)


def test_a_blocked_direction_names_how_many_rays_hit_and_how_far_away():
    """The flange's underside is at z = 40 and the hole's top end at z = 10."""
    blocked = next(
        c for c in approaches("bracket_with_overhung_through_hole")["H-001"].candidates
        if c.direction_key == "+Z"
    )

    assert blocked.reachable is False
    assert blocked.rays_cast == blocked.rays_blocked == 5
    assert blocked.nearest_hit_mm == pytest.approx(29.99)
    assert blocked.reason == (
        "not reachable along +Z (top): 5 of 5 ray(s) hit material, the nearest 29.990 mm away"
    )


def test_a_feature_with_no_clear_direction_is_reported_unreachable_not_assigned():
    approach = approaches("bracket_with_unreachable_blind_hole")["H-001"]

    assert approach.status == "unreachable"
    assert approach.reason.startswith("not reachable along any candidate direction")


def test_a_counterbore_and_a_countersink_are_approached_from_their_cut_side_only():
    counterbore = approaches("block_with_counterbore")["H-001"]
    countersink = approaches("block_with_countersunk_hole")["H-001"]

    assert [c.direction_key for c in counterbore.candidates] == ["+Z"]
    assert counterbore.rule == "counterbore_open_end"
    assert [c.direction_key for c in countersink.candidates] == ["+Z"]
    assert countersink.rule == "through_hole_countersunk_end"


def test_a_pocket_is_approached_along_its_floor_normal():
    approach = approaches("block_with_pocket")["PKT-001"]

    assert [(c.direction_key, c.reachable) for c in approach.candidates] == [("+Z", True)]
    assert approach.candidates[0].rays_cast == 9


def test_a_planar_face_is_faced_head_on_or_side_milled_by_the_flank():
    """The block's +X face: head-on along +X, by the flank along the four
    directions in its plane, and never from behind (−X)."""
    plane = next(
        a for a in approaches("block_with_top_holes").values()
        if a.feature_type == "plane" and a.candidates and any(
            c.direction_key == "+X" and c.kind == "primary" for c in a.candidates
        )
    )

    kinds = {c.direction_key: c.kind for c in plane.candidates}
    assert kinds == {"+Z": "flank", "-Z": "flank", "+X": "primary", "+Y": "flank", "-Y": "flank"}
    assert all(c.reachable for c in plane.candidates)


def test_a_blend_takes_the_normals_of_the_planes_it_blends():
    approach = approaches("block_with_fillet")["FIL-001"]

    # The two faces it rounds between, not the two end faces it merely meets.
    assert approach.rule == "fillet_neighbour_normals"
    assert len(approach.candidates) == 2
    assert all(c.reachable and c.kind == "primary" for c in approach.candidates)
    assert all("blends into" in c.basis for c in approach.candidates)


def test_turning_and_unclassified_bores_have_no_approach():
    shape, recognition = shape_and_features(step_fixtures.stepped_shaft_with_cross_hole(), "step")
    result = {a.feature_id: a for a in approach_directions(shape, list(recognition.features))}

    cylinders = [a for a in result.values() if a.feature_type == "cylinder"]
    assert cylinders and all(a.status == "no_approach" for a in cylinders)
    assert "turned work has no milling approach" in cylinders[0].reason
    ambiguous = [a for a in result.values() if a.subtype == "ambiguous"]
    assert ambiguous and ambiguous[0].status == "no_approach"


def test_faceted_geometry_is_refused_with_its_reason():
    with pytest.raises(ApproachUnavailable, match="faceted export"):
        shape_and_features(faceted_fixtures.faceted_block_with_blind_hole(), "step")


# ----------------------------------------------------------- 2. the proposal


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("block_with_top_holes", [("+Z", ["H-001", "H-002"])]),
        ("block_with_top_and_side_holes", [("+Z", ["H-001"]), ("+X", ["H-002"])]),
        ("block_with_top_and_bottom_holes", [("+Z", ["H-001"]), ("-Z", ["H-002"])]),
        # Reachable from either end; the fixed order breaks the tie.
        ("block_with_through_hole", [("+Z", ["H-001"])]),
        ("bracket_with_overhung_through_hole", [("-Z", ["H-001"])]),
        ("bracket_with_unreachable_blind_hole", []),
    ],
)
def test_holes_are_grouped_one_setup_per_direction(fixture, expected):
    assert grouping(propose(fixture, holes_only(fixture))) == expected


def test_facing_the_whole_block_adds_exactly_the_flip_for_its_bottom_face():
    """Top, holes and the four sides (by the flank) from +Z; the bottom face
    can only be faced from −Z or side-milled, and head-on wins the tie."""
    proposal = propose("block_with_top_holes", full_plan("block_with_top_holes"))

    assert [s.direction_key for s in proposal.setups] == ["+Z", "-Z"]
    top, bottom = proposal.setups
    assert top.feature_ids[:2] == ["H-001", "H-002"]
    assert len(top.operation_ids) == 7
    assert len(bottom.operation_ids) == 1
    assert [o.access for o in bottom.operations] == ["primary"]
    assert sorted(o.access for o in top.operations).count("flank") == 4


def test_an_unreachable_operation_is_listed_and_left_where_it_is():
    proposal = propose(
        "bracket_with_unreachable_blind_hole", full_plan("bracket_with_unreachable_blind_hole")
    )

    assert [u.feature_ids for u in proposal.unplaced] == [["H-001"]]
    assert proposal.unplaced[0].status == "unreachable"
    assert "29.990 mm away" in proposal.unplaced[0].reason
    assert "H-001" not in [fid for s in proposal.setups for fid in s.feature_ids]
    assert proposal.confidence is Confidence.LOW


def test_confidence_is_medium_at_most_and_low_when_anything_is_unplaced():
    clean = propose("block_with_top_holes", holes_only("block_with_top_holes"))

    assert clean.confidence is Confidence.MEDIUM
    assert "a ray is not a tool" in clean.confidence_reason
    with pytest.raises(ValueError, match="never high confidence"):
        SetupProposal(confidence=Confidence.HIGH)


def test_the_proposal_states_its_limits_and_its_machine():
    proposal = propose("block_with_top_holes", holes_only("block_with_top_holes"))

    assert proposal.machine == "3_axis_mill"
    assert "4-axis" in proposal.machine_note and "5-axis" in proposal.machine_note
    assert "not guaranteed to be the fewest" in proposal.algorithm_note
    text = " ".join(proposal.limitations)
    for limit in ("holder", "length", "Undercuts", "fixture"):
        assert limit in text


def test_the_proposal_is_deterministic():
    plan = full_plan("bracket_with_overhung_through_hole")
    first = post_propose("bracket_with_overhung_through_hole", plan)
    second = post_propose("bracket_with_overhung_through_hole", plan)

    assert first.status_code == 200
    assert first.content == second.content


def test_turned_and_manual_lines_are_not_placed_and_say_why():
    shape, recognition = shape_and_features(step_fixtures.stepped_shaft_with_axial_hole(), "step")
    plan = build_plan(suggest_operations(recognition).suggestions, units="mm")
    proposal = propose_setups(
        plan, approach_directions(shape, features_to_analyse(plan, list(recognition.features)))
    )

    turned = [u for u in proposal.unplaced if u.status == "not_a_tool_approach_process"]
    assert turned and all("turning has no milling approach" in u.reason for u in turned)
    placed = [fid for s in proposal.setups for fid in s.feature_ids]
    assert placed == ["H-001"]


# ----------------------------------------------------------- 3. nothing changes by itself


def test_the_default_plan_still_has_one_assumed_setup_and_no_new_fields():
    plan = full_plan("block_with_top_and_side_holes")

    assert [s.system_grouping for s in plan.setups] == ["system_assumed"]
    assert plan.setups[0].system_grouping_confidence is Confidence.LOW
    assert {o.system_setup_id for o in plan.operations} == {DEFAULT_SETUP_ID}
    assert all(o.user_setup_id is None for o in plan.operations)
    # No field was added to the plan's models, so every stored plan and every
    # plan `build_plan` produces serializes exactly as before.
    assert set(ManufacturingSetup.model_fields) == {
        "setup_id", "sequence_position", "name", "description", "process_context", "origin",
        "system_operation_ids", "system_grouping", "system_grouping_reason",
        "system_grouping_confidence", "system_grouping_assumptions",
        "system_setup_time_minutes", "system_setup_time_source", "user_action",
        "user_setup_time_minutes", "system_setup_time_accepted",
    }
    assert "approach" not in " ".join(PlannedOperation.model_fields)
    assert "approach" not in " ".join(ManufacturingPlan.model_fields)


def test_proposing_changes_nothing_in_the_plan_sent():
    plan = full_plan("block_with_top_and_side_holes")
    before = plan.model_dump_json()

    response = post_propose("block_with_top_and_side_holes", plan)

    assert response.status_code == 200
    assert plan.model_dump_json() == before
    assert response.json()["kind"] == "proposal"
    assert "plan" not in response.json()


def test_suggesting_operations_does_not_propose_setups():
    response = client.post(
        "/cad/operations/suggest",
        files={"file": ("p.step", fixture_bytes("block_with_top_and_side_holes"))},
    )
    assert response.status_code == 200
    assert "setup" not in " ".join(response.json()).lower()


# ----------------------------------------------------------- 4. applying it


def test_applying_writes_user_setups_and_never_touches_the_system_grouping():
    plan = holes_only("block_with_top_and_side_holes")
    applied = apply_setup_proposal(plan, propose("block_with_top_and_side_holes", plan))
    result = applied.plan

    assert applied.created_setup_ids == ["SETUP-002", "SETUP-003"]
    # SETUP-001 still holds the removed facing lines, so it stays.
    assert applied.removed_setup_ids == []
    hole_ops = {o.feature_ids[0]: o for o in result.operations if o.feature_type == "hole"}
    assert hole_ops["H-001"].system_setup_id == DEFAULT_SETUP_ID
    assert hole_ops["H-001"].user_setup_id == "SETUP-002"
    assert hole_ops["H-002"].system_setup_id == DEFAULT_SETUP_ID
    assert hole_ops["H-002"].user_setup_id == "SETUP-003"
    assert all(o.system_setup_id == DEFAULT_SETUP_ID for o in result.operations)
    # The system's own setup is untouched, field for field.
    assert result.setup(DEFAULT_SETUP_ID) == plan.setup(DEFAULT_SETUP_ID)

    created = [result.setup(sid) for sid in applied.created_setup_ids]
    assert [s.name for s in created] == ["Approach +Z (top)", "Approach +X"]
    assert all(s.system_grouping == APPROACH_GROUPING for s in created)
    assert all(s.system_grouping_confidence is Confidence.MEDIUM for s in created)
    assert all(s.user_action is UserAction.ACCEPTED for s in created)
    assert "reachable along +X" in " ".join(created[1].system_grouping_assumptions)


def test_a_new_setup_carries_the_profile_estimate_uncommitted():
    """Derived the way the default setup's is — the largest per-process
    baseline among its operations — and charged nothing until accepted."""
    plan = holes_only("block_with_top_and_side_holes")
    result = apply_setup_proposal(plan, propose("block_with_top_and_side_holes", plan)).plan

    for setup_id in ("SETUP-002", "SETUP-003"):
        setup = result.setup(setup_id)
        members = result.operations_in(setup_id)
        assert setup.system_setup_time_minutes == max(
            o.system_suggested_setup_time for o in members
        )
        assert setup.system_setup_time_source == "largest_process_baseline"
        assert setup.system_setup_time_accepted is False
        assert setup.setup_time_minutes is None
        assert setup.charged_setup_time == 0.0


def test_a_setup_the_apply_empties_is_removed_and_one_still_holding_work_is_kept():
    whole = full_plan("block_with_top_holes")
    applied = apply_setup_proposal(whole, propose("block_with_top_holes", whole))
    assert applied.removed_setup_ids == [DEFAULT_SETUP_ID]
    assert [s.setup_id for s in applied.plan.ordered_setups] == ["SETUP-002", "SETUP-003"]

    blocked = full_plan("bracket_with_unreachable_blind_hole")
    applied = apply_setup_proposal(blocked, propose("bracket_with_unreachable_blind_hole", blocked))
    assert applied.removed_setup_ids == []
    hole = next(o for o in applied.plan.operations if o.feature_ids == ["H-001"])
    assert hole.setup_id == DEFAULT_SETUP_ID and hole.user_setup_id is None
    assert hole.operation_id in applied.left_in_place


def test_the_feedback_record_says_the_grouping_came_from_the_approach_proposal():
    plan = holes_only("block_with_top_and_side_holes")
    result = apply_setup_proposal(plan, propose("block_with_top_and_side_holes", plan)).plan

    records = {r.operation_id: r for r in feedback_from_plan(result)}
    hole = next(o for o in result.operations if o.feature_ids == ["H-002"])
    record = records[hole.operation_id]
    assert record.system_setup_id == DEFAULT_SETUP_ID
    assert record.final_setup_id == "SETUP-003"
    assert record.setup_changed is True
    assert record.system_setup_grouping == APPROACH_GROUPING


def preview(plan: ManufacturingPlan) -> dict:
    response = client.post(
        "/operations/plan-cost-preview?quantity=1&rate_set_id=poc-default",
        json=json.loads(plan.model_dump_json()),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_once_accepted_the_cost_rises_by_exactly_the_extra_setup_time_at_the_rate():
    """The whole top-and-side plan, every time accepted: one setup becomes two.

    Before: one assumed setup, its 10-minute estimate (the largest per-process
    baseline: milling's) accepted. After applying — +Z holds H-001 and the top
    and four sides, +X holds H-002, the +X face and the bottom by the flank, and
    the emptied SETUP-001 is removed — and accepting both new 10-minute
    estimates: two setups of 10 minutes. Each setup is charged at the machine
    rate of its first costed operation, a drilled hole in both, so setup cost
    rises by 10 min × ₹1,800/h ÷ 60 = ₹300.00, once. Cycle time and run cost do
    not move.
    """
    base = full_plan("block_with_top_and_side_holes")
    committed = base.model_copy(
        update={"operations": [o.accepted(use_estimated_time=True) for o in base.operations]}
    )
    committed = committed.replacing_setup(
        committed.setup(DEFAULT_SETUP_ID).accepted(use_estimated_time=True)
    )
    before = preview(committed)

    result = apply_setup_proposal(committed, propose("block_with_top_and_side_holes", committed))
    applied = result.plan
    uncommitted = preview(applied)
    accepted = applied
    for setup_id in ("SETUP-002", "SETUP-003"):
        accepted = accepted.replacing_setup(accepted.setup(setup_id).accepted(use_estimated_time=True))
    after = preview(accepted)

    assert result.removed_setup_ids == [DEFAULT_SETUP_ID]
    assert [applied.operations_in(sid)[0].operation_type for sid in ("SETUP-002", "SETUP-003")] == [
        "drilling",
        "drilling",
    ]
    minutes = applied.setup("SETUP-003").system_setup_time_minutes
    assert minutes == committed.setup(DEFAULT_SETUP_ID).system_setup_time_minutes == 10.0
    assert before["time"]["total_setup_time_minutes"] == 10.0
    assert after["time"]["total_setup_time_minutes"] == 20.0
    assert after["time"]["total_cycle_time_minutes"] == before["time"]["total_cycle_time_minutes"]
    assert after["run_cost"] == before["run_cost"]
    assert after["setup_cost"] - before["setup_cost"] == pytest.approx(
        minutes * DRILLING_MACHINE_RATE / 60.0
    )
    assert after["setup_cost"] - before["setup_cost"] == pytest.approx(300.0)
    assert after["total_operation_cost"] - before["total_operation_cost"] == pytest.approx(300.0)
    # Applied but not yet accepted: no setup is charged, and both say so.
    assert uncommitted["setup_cost"] == 0.0
    assert uncommitted["time"]["setups_needing_time"] == ["SETUP-002", "SETUP-003"]


def test_the_apply_endpoint_recomputes_the_proposal_and_returns_the_plan():
    plan = holes_only("block_with_top_and_side_holes")
    response = post_propose("block_with_top_and_side_holes", plan, path="/cad/setups/apply-proposal")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_setup_ids"] == ["SETUP-002", "SETUP-003"]
    assert [s["direction_key"] for s in body["proposal"]["setups"]] == ["+Z", "+X"]
    returned = ManufacturingPlan.model_validate(body["plan"])
    assert returned == apply_setup_proposal(plan, propose("block_with_top_and_side_holes", plan)).plan


def test_applying_with_nothing_placed_is_refused():
    plan = holes_only("bracket_with_unreachable_blind_hole")
    response = post_propose(
        "bracket_with_unreachable_blind_hole", plan, path="/cad/setups/apply-proposal"
    )

    assert response.status_code == 422
    assert "places no operation" in response.json()["detail"]


# ----------------------------------------------------------- 5. scope and refusals


def test_a_faceted_file_gets_no_proposal_and_says_why():
    data = faceted_fixtures.faceted_block_with_blind_hole()
    plan = full_plan("block_with_top_holes")
    files = {"file": ("faceted.step", data, "application/octet-stream")}
    form = {"manufacturing_plan": plan.model_dump_json()}

    proposed = client.post("/cad/setups/propose", files=files, data=form)
    applied = client.post("/cad/setups/apply-proposal", files=files, data=form)

    assert proposed.status_code == 200
    assert proposed.json()["available"] is False
    assert "faceted export" in proposed.json()["unavailable_reason"]
    assert proposed.json()["setups"] == []
    assert applied.status_code == 422


def test_an_iges_solid_is_analysed_and_a_surface_export_is_not():
    plan = full_plan("block_with_through_hole")
    solid = client.post(
        "/cad/setups/propose",
        files={"file": ("p.igs", iges_fixtures.block_with_hole_solid(), "application/octet-stream")},
        data={"manufacturing_plan": plan.model_dump_json()},
    )
    surfaces = client.post(
        "/cad/setups/propose",
        files={"file": ("p.igs", iges_fixtures.block_with_hole_surfaces(), "application/octet-stream")},
        data={"manufacturing_plan": plan.model_dump_json()},
    )

    assert solid.status_code == 200 and solid.json()["available"] is True
    assert solid.json()["setups"]
    assert surfaces.status_code == 200 and surfaces.json()["available"] is False
    assert "loose surfaces" in surfaces.json()["unavailable_reason"]


def test_an_stl_is_refused_as_the_features_path_refuses_it():
    import trimesh

    stl = trimesh.creation.box(extents=(40.0, 30.0, 20.0)).export(file_type="stl")
    response = client.post(
        "/cad/setups/propose",
        files={"file": ("box.stl", stl, "application/octet-stream")},
        data={"manufacturing_plan": full_plan("block_with_top_holes").model_dump_json()},
    )

    assert response.status_code == 400


def test_a_component_plan_is_proposed_on_its_own_shape_with_its_own_ids():
    plans = client.post(
        "/cad/assembly/plans",
        files={"file": ("pump.step", pump_assembly(), "application/octet-stream")},
        data={"rate_set_id": "poc-default"},
    )
    assert plans.status_code == 200, plans.text
    entry = next(
        e for e in plans.json()["plan"]["component_plans"] if e["component_name"] == "HOUSING"
    )
    plan = ManufacturingPlan.model_validate(entry["plan"])

    response = client.post(
        "/cad/setups/apply-proposal",
        files={"file": ("pump.step", pump_assembly(), "application/octet-stream")},
        data={"manufacturing_plan": plan.model_dump_json(), "component_id": entry["definition_id"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proposal"]["component_id"] == entry["definition_id"]
    assert all(fid.startswith("HOUSING/") for s in body["proposal"]["setups"] for fid in s["feature_ids"])
    assert body["created_setup_ids"] == ["HOUSING/SETUP-002", "HOUSING/SETUP-003"]
    assert [s["direction_key"] for s in body["proposal"]["setups"]] == ["+Z", "-Z"]


def test_an_unknown_component_is_a_404():
    response = client.post(
        "/cad/setups/propose",
        files={"file": ("pump.step", pump_assembly(), "application/octet-stream")},
        data={
            "manufacturing_plan": full_plan("block_with_top_holes").model_dump_json(),
            "component_id": "NO_SUCH_PART",
        },
    )
    assert response.status_code == 404


# ----------------------------------------------------------- module boundaries


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_approach_module_imports_no_plan_and_no_costing():
    imported = {m for m in _imports(PACKAGE / "features" / "approach.py") if m.startswith("cad_costing_agent")}

    assert imported <= {
        "cad_costing_agent.features",
        "cad_costing_agent.features.model",
        "cad_costing_agent.features.step_text",
        "cad_costing_agent.cad_assembly",
        "cad_costing_agent.cad_geometry",
        "cad_costing_agent.cad_iges",
    }, imported


def test_the_proposal_module_imports_the_plan_and_the_approach_evidence_only():
    imported = {m for m in _imports(PACKAGE / "setup_proposal.py") if m.startswith("cad_costing_agent")}

    assert imported == {
        "cad_costing_agent.features.approach",
        "cad_costing_agent.features.model",
        "cad_costing_agent.manufacturing_plan",
        "cad_costing_agent.operation_suggestions",
    }


def _modules_importing(module: str) -> list[str]:
    """Package files that import `cad_costing_agent.<module>`, in either spelling."""
    package, _, name = f"cad_costing_agent.{module}".rpartition(".")
    readers = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            direct = isinstance(node, ast.ImportFrom) and node.module == f"{package}.{name}"
            named = (
                isinstance(node, ast.ImportFrom)
                and node.module == package
                and any(alias.name == name for alias in node.names)
            )
            plain = isinstance(node, ast.Import) and any(
                alias.name == f"{package}.{name}" for alias in node.names
            )
            if direct or named or plain:
                readers.append(path.relative_to(PACKAGE).as_posix())
                break
    return readers


def test_only_main_imports_the_proposal_and_only_it_and_main_read_the_approach():
    assert _modules_importing("setup_proposal") == ["main.py"]
    assert _modules_importing("features.approach") == ["main.py", "setup_proposal.py"]


def _code_without_prose(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    body.pop(0)
    return ast.unparse(tree)


def test_neither_module_contains_rate_or_money_arithmetic():
    for path in (PACKAGE / "features" / "approach.py", PACKAGE / "setup_proposal.py"):
        code = _code_without_prose(path)
        for forbidden in ("rate_per_hour", "RateSet", "Money", "calculate_", "currency"):
            assert forbidden not in code, (path.name, forbidden)


def test_the_proposal_never_writes_a_system_setup_id():
    """No keyword argument, update key or attribute anywhere in the module
    names `system_setup_id`: operations are moved only through the plan's own
    `with_operation_in_setup`, which writes `user_setup_id`."""
    tree = ast.parse((PACKAGE / "setup_proposal.py").read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword):
            assert node.arg != "system_setup_id"
        if isinstance(node, ast.Dict):
            assert not any(
                isinstance(key, ast.Constant) and key.value == "system_setup_id" for key in node.keys
            )
        if isinstance(node, ast.Attribute):
            assert node.attr != "system_setup_id"
    assert "with_operation_in_setup" in _code_without_prose(PACKAGE / "setup_proposal.py")


# ----------------------------------------------------------- the browser


def _js_without_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in source.splitlines())


def _approach_section() -> str:
    source = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")
    start = source.index("// ------------------------------------------------- setups by approach direction")
    end = source.index("// ---------------------------------------------------------- the cost impact")
    return _js_without_comments(source[start:end])


def test_the_browser_sends_the_decision_to_the_server_and_groups_nothing():
    section = _approach_section()

    assert "/cad/setups/propose" in section
    assert "/cad/setups/apply-proposal" in section
    assert "planPayload()" in section
    assert "state.planComponentId" in section
    # No grouping, counting or direction maths of its own.
    for pattern in (
        r"direction\.[xyz]\s*[-+*/]",
        r"\.reduce\(",
        r"\.sort\(",
        r"system_setup_id\s*=(?!=)",
        r"user_setup_id\s*=(?!=)",
        r"Math\.",
    ):
        assert not re.search(pattern, section), pattern


def test_every_server_string_the_panel_prints_is_escaped():
    """Within the two functions that build markup from the server's proposal."""
    full = _approach_section()
    section = full[full.index("function approachSetupItem") : full.index("function showApproachSetup")]

    for field in ("direction_label", "operation_name", "reason", "confidence_reason", "unavailable_reason"):
        for match in re.finditer(r"\$\{([^}]*\b" + field + r"\b[^}]*)\}", section):
            assert "escapeHtml(" in match.group(1), match.group(0)


def test_the_panel_is_in_the_manufacturing_plan_card():
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    assert 'id="approach-toggle"' in markup
    assert "Proposed setups by approach direction" in markup
    assert markup.index('id="ops-body"') < markup.index('id="approach-toggle"') < markup.index(
        'id="suggestions-card"'
    )


def test_operations_keep_their_plan_order_inside_each_new_setup():
    plan = full_plan("block_with_top_holes")
    result = apply_setup_proposal(plan, propose("block_with_top_holes", plan)).plan
    original = [o.operation_id for o in plan.ordered]

    for setup in result.ordered_setups:
        members = result.operations_in(setup.setup_id)
        assert [o.sequence_position for o in members] == list(range(1, len(members) + 1))
        ids = [o.operation_id for o in members]
        assert ids == sorted(ids, key=original.index)
