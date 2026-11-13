"""Pull direction, draft and undercuts, and the draft and undercut DFM rules.

Every expected answer below follows from how the fixture is built
(`tests/draw_fixtures.py`), not from running the code: the drafted cup's walls
are offset by height x tan 2 deg, the side hole is a cylinder along X, the T-slot
is closed at both ends.
"""

from __future__ import annotations

import ast
import json
import re
from functools import lru_cache
from pathlib import Path

import pytest
import trimesh
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cad_costing_agent import db as db_module
from cad_costing_agent import limits
from cad_costing_agent.db_models import DfmProfileRow
from cad_costing_agent.dfm import (
    BASIS_PUBLISHED,
    DEFAULT_DFM_PROFILE_ID,
    MIN_DRAFT_SETTING,
    MIN_WALL_SETTING,
    NO_FORMING_PROCESS,
    SEED_DFM_PROFILES,
    UNDERCUT_SETTING,
    DrawEvidence,
    RuleId,
    evaluate_dfm,
)
from cad_costing_agent.features import draw_direction
from cad_costing_agent.features.draw_direction import LIMITATIONS, PARTING_NOTE, RECOMMENDATION_BASIS
from cad_costing_agent.features.model import Point3D
from cad_costing_agent.main import app
from tests import draw_fixtures, faceted_fixtures, iges_fixtures, step_fixtures

client = TestClient(app)
SRC = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
EXACT = 1e-6  # degrees, for every analytic draft


def _post(path: str, data: bytes, name: str = "part.step", **form):
    return client.post(path, files={"file": (name, data, "application/octet-stream")}, data=form)


@lru_cache(maxsize=None)
def _bytes(name: str) -> bytes:
    return getattr(draw_fixtures, name)()


@lru_cache(maxsize=None)
def _measured(name: str, pull_axis: str | None = None) -> str:
    form = {"pull_axis": pull_axis} if pull_axis else {}
    response = _post("/cad/draw-direction", _bytes(name), **form)
    assert response.status_code == 200, response.text
    return response.text


def measured(name: str, pull_axis: str | None = None) -> dict:
    return json.loads(_measured(name, pull_axis))


def candidate(body: dict, label: str) -> dict:
    return next(c for c in body["candidates"] if c["label"] == label)


def face(body: dict, face_id: int) -> dict:
    return next(f for f in body["faces"] if f["face_id"] == face_id)


# --- the measurement, on exact fixtures ----------------------------------------------------


def test_a_cup_drafted_2_degrees_is_pulled_along_z_with_every_wall_exactly_2_degrees():
    body = measured("drafted_cup")
    assert body["measured"] is True
    assert body["pull_axis_label"] == body["recommended_axis_label"] == "±Z"
    assert body["axis_source"] == "recommended"
    assert body["undercut_face_count"] == 0 and body["zero_draft_face_count"] == 0
    walls = [f for f in body["faces"] if f["draft_deg"] < 45.0]
    assert len(walls) == 8  # four outside, four inside
    for wall in walls:
        assert wall["surface_type"] == "plane" and wall["draft_method"] == "exact_plane"
        assert wall["draft_deg"] == pytest.approx(draw_fixtures.DRAFT_DEG, abs=EXACT)
    # The outside releases into the -Z half, the pocket into the +Z half.
    assert sorted(f["release"] for f in walls) == ["minus"] * 4 + ["plus"] * 4
    assert body["minimum_draft_deg"] == pytest.approx(2.0, abs=EXACT)
    # Across the cup the pocket is an undercut: the four inner walls and the floor.
    for label in ("±X", "±Y"):
        assert candidate(body, label)["undercut_faces"] == 5


def test_the_same_cup_with_0_degree_walls_has_eight_zero_draft_walls_and_no_undercut():
    body = measured("zero_draft_cup")
    assert body["pull_axis_label"] == "±Z"
    assert body["undercut_face_count"] == 0
    assert body["zero_draft_face_count"] == 8
    # 4 outer walls of 40 x 20 and 4 inner walls of 30 x 15.
    assert body["zero_draft_area_mm2"] == pytest.approx(4 * 800.0 + 4 * 450.0, abs=EXACT)
    for face_id in body["zero_draft_face_ids"]:
        assert face(body, face_id)["draft_deg"] == pytest.approx(0.0, abs=EXACT)
    assert candidate(body, "±X")["undercut_faces"] == 5
    assert candidate(body, "±Y")["undercut_faces"] == 5


def test_a_cone_whose_axis_is_the_pull_has_its_half_angle_as_its_exact_draft():
    body = measured("drafted_round_cup")
    assert body["pull_axis_label"] == "±Z" and body["undercut_face_count"] == 0
    cones = [f for f in body["faces"] if f["surface_type"] == "cone"]
    assert len(cones) == 2
    for cone in cones:
        assert cone["draft_method"] == "exact_axial_cone"
        assert cone["draft_deg"] == pytest.approx(2.0, abs=EXACT)


def test_a_side_hole_is_an_undercut_from_z_and_y_and_not_from_x():
    body = measured("block_with_side_hole")
    assert body["recommended_axis_label"] == body["pull_axis_label"] == "±X"
    assert body["undercut_face_count"] == 0
    for label in ("±Z", "±Y"):
        assert candidate(body, label)["undercut_faces"] == 1
        # The bore: 2 x pi x 5 x 40.
        assert candidate(body, label)["undercut_area_mm2"] == pytest.approx(400.0 * 3.141592653589793, abs=1e-5)
    bore = next(f for f in body["faces"] if f["surface_type"] == "cylinder")
    assert bore["draft_method"] == "exact_axial_cylinder" and bore["draft_deg"] == 0.0
    assert bore["release"] == "either"


def test_a_closed_t_slot_is_an_undercut_along_every_principal_axis():
    body = measured("closed_t_slot")
    counts = {c["label"]: c["undercut_faces"] for c in body["candidates"]}
    # Z: both ceilings, both chamber sides, both chamber ends and the chamber floor.
    # X and Y: every face of the void (4 slot faces, 2 ceilings, 4 chamber walls, floor).
    assert counts == {"±Z": 7, "±X": 11, "±Y": 11}
    assert body["recommended_axis_label"] == "±Z"
    assert body["undercut_face_count"] == 7
    for face_id in body["undercut_face_ids"]:
        undercut = face(body, face_id)
        assert undercut["samples_undercut"] > 0 and undercut["marker"] is not None


def test_a_plate_has_no_undercut_on_any_axis_and_z_has_the_least_zero_draft_area():
    body = measured("plate")
    assert all(c["undercut_faces"] == 0 for c in body["candidates"])
    assert body["recommended_axis_label"] == "±Z"
    assert candidate(body, "±Z")["zero_draft_area_mm2"] == pytest.approx(1600.0, abs=EXACT)
    assert candidate(body, "±X")["zero_draft_area_mm2"] == pytest.approx(13000.0, abs=EXACT)


def test_candidates_are_the_file_axes_then_inertia_then_largest_planes_deduplicated_in_order():
    body = measured("drafted_cup")
    labels = [c["label"] for c in body["candidates"]]
    assert labels[:3] == ["±Z", "±X", "±Y"]
    assert [c["order"] for c in body["candidates"]] == list(range(len(labels)))
    z = candidate(body, "±Z")
    # The square cup's X and Y moments are equal, so only its Z inertia axis is offered,
    # and it is the file's Z axis: one candidate carrying both reasons.
    assert any("principal axis of inertia" in s for s in z["sources"])
    assert not any("principal axis of inertia" in s for s in candidate(body, "±X")["sources"])
    # The third-largest planar face is a drafted wall: its normal is a candidate of its own.
    assert len(labels) == 4 and labels[3].startswith("±(")
    assert sum(c["recommended"] for c in body["candidates"]) == 1
    assert body["recommendation_basis"] == RECOMMENDATION_BASIS
    assert "not a design decision" in RECOMMENDATION_BASIS


def test_an_engineer_axis_overrides_the_choice_and_the_recommendation_stays_beside_it():
    body = measured("block_with_side_hole", "[0, 0, -2]")
    assert body["axis_source"] == "engineer_entered"
    assert body["pull_axis_label"] == "±Z" and body["recommended_axis_label"] == "±X"
    assert body["undercut_face_count"] == 1
    z = candidate(body, "±Z")
    assert z["engineer_entered"] is True and "entered by the engineer" in z["sources"]
    assert z["recommended"] is False and candidate(body, "±X")["recommended"] is True


def test_an_engineer_axis_no_candidate_offered_is_evaluated_last_and_never_recommended():
    body = measured("plate", '{"x": 1, "y": 1, "z": 0}')
    last = body["candidates"][-1]
    assert last["sources"] == ["entered by the engineer"] and last["engineer_entered"]
    assert last["recommended"] is False
    assert body["pull_axis"] == last["axis"]
    assert body["axis_source"] == "engineer_entered"
    assert body["recommended_axis_label"] == "±Z"


@pytest.mark.parametrize("axis", ["[0, 0, 0]", "[1, 2]", "not json", '["a", 0, 0]', "[1, 0, NaN]"])
def test_an_unusable_pull_axis_is_a_400(axis):
    response = _post("/cad/draw-direction", _bytes("plate"), pull_axis=axis)
    assert response.status_code == 400
    assert "pull_axis" in response.json()["detail"]


@pytest.mark.parametrize(
    "name", ["drafted_cup", "zero_draft_cup", "drafted_round_cup", "block_with_side_hole", "closed_t_slot"]
)
def test_the_batched_box_filtered_test_agrees_with_the_whole_shape_intersector(name):
    """`blocked_along` answers exactly what `first_hit` answers, ray for ray."""
    from cad_costing_agent.features import recognize_features_with_shape
    from cad_costing_agent.features.rays import ShapeRays

    shape = recognize_features_with_shape(_bytes(name), f"{name}.step").shape
    rays = ShapeRays(shape)
    origins = []
    for index in range(1, rays.faces.Extent() + 1):
        for point, normal in rays.samples(index, (5, 9)):
            origins.append(tuple(p + n * 0.01 for p, n in zip(point, normal)))
    for direction in ((0.0, 0.0, 1.0), (0.0, 0.0, -1.0), (1.0, 0.0, 0.0), (0.0, -1.0, 0.0)):
        batched = rays.blocked_along(origins, direction)
        single = [rays.first_hit(origin, direction) for origin in origins]
        assert batched == [(evaluated, hit is not None) for evaluated, hit in single]
        assert any(met for _, met in batched) and not all(met for _, met in batched)


def test_the_measurement_is_deterministic():
    first = _post("/cad/draw-direction", _bytes("closed_t_slot")).json()
    second = _post("/cad/draw-direction", _bytes("closed_t_slot")).json()
    first.pop("elapsed_ms")
    second.pop("elapsed_ms")
    assert first == second


def test_the_response_states_its_method_limits_and_why_no_parting_line_is_drawn():
    body = measured("plate")
    assert body["limitations"] == list(LIMITATIONS)
    assert body["parting"] is None and body["parting_note"] == PARTING_NOTE
    assert "A ray is not mould steel" in LIMITATIONS[0]
    assert body["grid"] == [5, 9] and body["budget_limited"] is False


def test_over_budget_the_grid_coarsens_and_then_nothing_is_measured(monkeypatch):
    data = _bytes("block_with_side_hole")
    monkeypatch.setattr(limits, "MAX_DRAW_RAYS", 1000)
    coarse = _post("/cad/draw-direction", data).json()
    assert coarse["measured"] and coarse["grid"] == [3, 7] and coarse["budget_limited"]
    assert "CAD_COSTING_MAX_DRAW_RAYS" in coarse["budget_note"]
    assert coarse["recommended_axis_label"] == "±X"
    monkeypatch.setattr(limits, "MAX_DRAW_RAYS", 10)
    refused = _post("/cad/draw-direction", data).json()
    assert refused["measured"] is False and refused["budget_limited"] is True
    assert "CAD_COSTING_MAX_DRAW_RAYS" in refused["not_measured_reason"]


def test_faceted_stl_and_iges_surface_models_are_refused_with_the_reason():
    faceted = _post("/cad/draw-direction", faceted_fixtures.faceted_plate_with_four_holes(), "f.step")
    assert faceted.status_code == 400 and "faceted" in faceted.json()["detail"]
    stl = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")
    refused = _post("/cad/draw-direction", stl, "cube.stl")
    assert refused.status_code == 400
    assert refused.json()["detail"] == _post("/cad/features", stl, "cube.stl").json()["detail"]
    surfaces = _post("/cad/draw-direction", iges_fixtures.block_with_hole_surfaces(), "b.igs")
    assert surfaces.status_code == 400 and "loose trimmed surfaces" in surfaces.json()["detail"]
    solid = _post("/cad/draw-direction", iges_fixtures.block_with_hole_solid(), "b.igs")
    assert solid.status_code == 200 and solid.json()["measured"] is True


def test_measuring_the_pull_direction_stores_nothing():
    total = client.get("/estimates").json()["total"]
    assert _post("/cad/draw-direction", _bytes("plate")).status_code == 200
    assert client.get("/estimates").json()["total"] == total


# --- the DFM rules, gated by a stated forming process ---------------------------------------


def _dfm(name: str, **form) -> dict:
    response = _post("/cad/dfm", _bytes(name), **form)
    assert response.status_code == 200, response.text
    return response.json()


def _rule(body: dict, rule_id: str) -> dict:
    return next(r for r in body["rules"] if r["rule_id"] == rule_id)


def _flags(body: dict, rule_id: str) -> list[dict]:
    return [f for f in body["flags"] if f["rule_id"] == rule_id]


def test_without_a_stated_forming_process_neither_rule_is_evaluated(monkeypatch):
    calls = []
    real = draw_direction.measure_draw_direction
    monkeypatch.setattr(
        draw_direction, "measure_draw_direction", lambda *a, **k: calls.append(1) or real(*a, **k)
    )
    body = _dfm("zero_draft_cup")
    assert body["dfm_profile_version"] == "1.2.0"
    for rule_id in ("dfm_min_draft", "dfm_undercut"):
        rule = _rule(body, rule_id)
        assert rule["evaluated"] is False and rule["skipped_reason"] == NO_FORMING_PROCESS
    assert body["draw"]["measured"] is False and body["draw"]["not_measured_reason"] == NO_FORMING_PROCESS
    assert not _flags(body, "dfm_min_draft") and not _flags(body, "dfm_undercut")
    assert calls == []  # nothing is measured to decide whether to measure
    assert "never guessed from the geometry" in NO_FORMING_PROCESS


def test_a_stated_process_checks_draft_against_the_recommended_axis():
    body = _dfm("zero_draft_cup", forming_process="casting")
    assert body["draw"]["forming_process"] == "casting"
    assert body["draw"]["forming_process_source"] == "stated in the request"
    assert body["draw"]["axis_source"] == "recommended" and body["draw"]["pull_axis_label"] == "±Z"
    (flag,) = _flags(body, "dfm_min_draft")
    assert flag["measured_value"] == 0.0 and flag["measured_unit"] == "degrees"
    assert flag["comparison"] == "less_than" and flag["threshold"] == 1.0
    assert flag["basis"] == BASIS_PUBLISHED and flag["source_url"] == MIN_DRAFT_SETTING.source_url
    assert "8 face(s)" in flag["explanation"] and "recommended axis" in flag["explanation"]
    assert flag["feature_ids"] == [] and flag["marker"]["axis"] == {"x": 0.0, "y": 0.0, "z": 1.0}
    assert _rule(body, "dfm_undercut")["evaluated"] is True and not _flags(body, "dfm_undercut")
    for word in ("severity", "confidence"):
        assert word not in flag


def test_two_degrees_of_draft_passes_the_one_degree_rule():
    body = _dfm("drafted_cup", forming_process="moulding")
    assert _rule(body, "dfm_min_draft")["evaluated"] and _rule(body, "dfm_min_draft")["features_checked"] == 11
    assert not _flags(body, "dfm_min_draft") and not _flags(body, "dfm_undercut")


def test_an_undercut_against_the_engineers_axis_raises_one_flag_marked_on_its_face():
    body = _dfm("block_with_side_hole", forming_process="forging", pull_axis="[0, 0, 1]")
    (flag,) = _flags(body, "dfm_undercut")
    assert flag["measured_value"] == 1.0 and flag["comparison"] == "at_least"
    assert flag["threshold"] == 1.0 and flag["measured_unit"] == "undercut faces"
    assert "the axis the engineer entered" in flag["explanation"]
    bore = next(
        f for f in measured("block_with_side_hole", "[0, 0, 1]")["faces"] if f["release"] == "undercut"
    )
    assert flag["marker"]["location"] == bore["marker"]
    assert flag["marker"]["label"] == f"Undercut face {bore['face_id']}"
    # Against the recommended X axis the same part has none.
    assert not _flags(_dfm("block_with_side_hole", forming_process="forging"), "dfm_undercut")


def test_a_decided_forming_route_states_the_process_and_an_unreviewed_one_does_not():
    suggested = _post("/cad/operations/suggest", _bytes("closed_t_slot")).json()["process_classification"]
    unreviewed = _dfm("closed_t_slot", process_classification=json.dumps(suggested))
    assert _rule(unreviewed, "dfm_undercut")["skipped_reason"] == NO_FORMING_PROCESS

    decided = client.post(
        "/cad/process/decide",
        json={"classification": suggested, "action": "edited", "route_id": "casting_machining"},
    ).json()
    body = _dfm("closed_t_slot", process_classification=json.dumps(decided))
    assert body["draw"]["forming_process"] == "casting"
    assert "casting" in body["draw"]["forming_process_source"].lower()
    (flag,) = _flags(body, "dfm_undercut")
    assert flag["measured_value"] == 7.0

    machined = client.post(
        "/cad/process/decide",
        json={"classification": suggested, "action": "edited", "route_id": "milling_drilling"},
    ).json()
    quiet = _dfm("closed_t_slot", process_classification=json.dumps(machined))
    assert _rule(quiet, "dfm_undercut")["evaluated"] is False
    # An explicit process that contradicts the decided route is refused, not resolved.
    clash = _post(
        "/cad/dfm",
        _bytes("closed_t_slot"),
        process_classification=json.dumps(machined),
        forming_process="casting",
    )
    assert clash.status_code == 400 and "contradicts" in clash.json()["detail"]


def test_an_unknown_forming_process_is_a_400():
    response = _post("/cad/dfm", _bytes("plate"), forming_process="welding")
    assert response.status_code == 400 and "casting, forging, moulding" in response.json()["detail"]


def test_profile_1_1_0_never_measures_the_pull_direction(monkeypatch):
    calls = []
    real = draw_direction.measure_draw_direction
    monkeypatch.setattr(
        draw_direction, "measure_draw_direction", lambda *a, **k: calls.append(1) or real(*a, **k)
    )
    body = _dfm("zero_draft_cup", forming_process="casting", dfm_profile_version="1.1.0")
    assert calls == [] and body["draw"] is None
    assert "sets no threshold" in _rule(body, "dfm_min_draft")["skipped_reason"]
    _dfm("zero_draft_cup", forming_process="casting")
    assert calls == [1]


def test_the_rules_fire_on_their_comparisons_and_invent_no_marker():
    profile = SEED_DFM_PROFILES[2]
    axis = Point3D.of(0.0, 0.0, 1.0)
    at = DrawEvidence(measured=True, pull_axis=axis, face_drafts=[(1, 1.0)], faces_checked=1)
    report = evaluate_dfm([], profile, draw=at)
    assert report.flags == []
    below = DrawEvidence(
        measured=True,
        pull_axis=axis,
        face_drafts=[(1, 0.99), (2, 0.0), (3, 5.0)],
        undercut_faces=[(4, Point3D.of(1.0, 2.0, 3.0))],
        faces_checked=4,
    )
    flags = {f.rule_id: f for f in evaluate_dfm([], profile, draw=below).flags}
    assert flags[RuleId.MIN_DRAFT].measured_value == 0.0
    assert "2 face(s)" in flags[RuleId.MIN_DRAFT].explanation
    assert flags[RuleId.MIN_DRAFT].marker is None  # no point was handed in, so none is invented
    assert flags[RuleId.UNDERCUT].marker.location == Point3D.of(1.0, 2.0, 3.0)


# --- versioned thresholds ------------------------------------------------------------------


def test_1_2_0_is_1_1_0_plus_the_two_rules_and_the_earlier_versions_are_unchanged():
    first, second, third = SEED_DFM_PROFILES
    assert (first.version, second.version, third.version) == ("1.0.0", "1.1.0", "1.2.0")
    assert [r.threshold for r in first.rules] == [4.0, 4.0, 0.5, 6.0]
    assert second.rules == [*first.rules, MIN_WALL_SETTING]
    assert third.rules == [*second.rules, MIN_DRAFT_SETTING, UNDERCUT_SETTING]
    assert MIN_DRAFT_SETTING.threshold == 1.0 and MIN_DRAFT_SETTING.unit == "degrees"
    assert UNDERCUT_SETTING.threshold == 1.0 and UNDERCUT_SETTING.unit == "faces"
    for setting in (MIN_DRAFT_SETTING, UNDERCUT_SETTING):
        assert setting.basis == BASIS_PUBLISHED and setting.source_url.startswith("https://www.protolabs.com/")
    assert "A common starting point is 1 to 2 degrees" in MIN_DRAFT_SETTING.source_quote
    assert "undercuts may require side-actions" in UNDERCUT_SETTING.source_quote


def _engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def _profiles(engine) -> list[tuple]:
    with sessionmaker(bind=engine)() as session:
        rows = session.execute(select(DfmProfileRow)).scalars().all()
        return sorted(
            (r.dfm_profile_id, r.version, r.name, r.description, r.is_illustrative, r.rules)
            for r in rows
        )


def test_a_fresh_database_and_one_seeded_before_1_2_0_end_up_identical():
    from cad_costing_agent.repository import DfmProfileRepository

    fresh = _engine()
    db_module.init_db(fresh)
    existing = _engine()
    db_module.run_migrations(existing)
    with sessionmaker(bind=existing)() as session:
        assert DfmProfileRepository(session).seed(SEED_DFM_PROFILES[:2]) == 2
        session.commit()
    before = _profiles(existing)
    db_module.init_db(existing)
    after = _profiles(existing)
    assert after == _profiles(fresh)
    assert [row[1] for row in after] == ["1.0.0", "1.1.0", "1.2.0"]
    assert after[:2] == before  # the stored 1.0.0 and 1.1.0 rows were not rewritten


# --- structural boundaries -----------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("cad_costing_agent"):
                found.add(node.module)
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("cad_costing_agent"))
    return found


def _code_without_prose(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)  # unparse drops comments


def test_the_measuring_module_imports_only_limits_the_feature_model_and_the_shared_rays():
    modules = {m for m in _imports(SRC / "features" / "draw_direction.py") if m.count(".") <= 2}
    assert modules == {
        "cad_costing_agent",
        "cad_costing_agent.limits",
        "cad_costing_agent.features.model",
        "cad_costing_agent.features.rays",
    }, modules


def test_there_is_one_ray_caster_and_it_imports_nothing_from_the_package():
    assert _imports(SRC / "features" / "rays.py") == set()
    for name in ("draw_direction.py", "approach.py"):
        assert "cad_costing_agent.features.rays.ShapeRays" in _imports(SRC / "features" / name), name
    for name in ("draw_direction.py", "approach.py"):
        code = _code_without_prose(SRC / "features" / name)
        assert "IntCurvesFace" not in code and "BRepLProp_SLProps" not in code, name


def test_only_main_reads_the_pull_direction_and_dfm_never_reaches_it():
    importers = set()
    for path in SRC.rglob("*.py"):
        if path.name == "draw_direction.py":
            continue
        if any(m.endswith("draw_direction") for m in _imports(path)):
            importers.add(path.relative_to(SRC).as_posix())
    assert importers == {"main.py"}, importers
    assert "OCP" not in _code_without_prose(SRC / "dfm.py")


def test_the_measurement_has_no_randomness_and_no_process_cost_or_time_vocabulary():
    code = _code_without_prose(SRC / "features" / "draw_direction.py")
    for forbidden in (
        "random", "rate_set", "RateSet", "calculate_", "cycle_time", "setup_time", "session",
        "ManufacturingFamily", "process_classification", "route",
    ):
        assert forbidden not in code, forbidden


def test_the_process_is_never_read_from_the_geometry():
    """The gate reads two request fields and nothing measured."""
    tree = ast.parse((SRC / "main.py").read_text(encoding="utf-8"))
    gate = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_stated_forming_process"
    )
    assert [a.arg for a in gate.args.args] == ["forming_process", "process_classification"]
    code = ast.unparse(gate)
    for forbidden in ("recognized", "features", "draw_direction", "shape", "draft"):
        assert forbidden not in code.split('"""')[-1], forbidden


def test_the_main_section_wires_the_measurement_into_nothing_but_dfm():
    source = (SRC / "main.py").read_text(encoding="utf-8")
    banner = "# ------------------------------------------------------------- pull direction"
    assert source.count(banner) == 1
    section = source.index(banner)
    section_end = source.index("\n# ----", section + len(banner))
    dfm_start = source.index('@app.post("/cad/dfm"')
    dfm_end = source.index("\n@app.", dfm_start + 1)
    calls = [m.start() for m in re.finditer(r"_draw_evidence\(|_measure_draw\(|measure_draw_direction\(", source)]
    assert calls
    outside = [p for p in calls if not (dfm_start < p < dfm_end or section < p < section_end)]
    assert outside == [], "the pull direction is measured only in its own section and in /cad/dfm"


# --- the panel -------------------------------------------------------------------------------


def _js_code(name: str) -> str:
    source = (SRC / "static" / name).read_text(encoding="utf-8")
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL))


def test_the_pull_direction_card_prints_the_server_and_does_no_arithmetic():
    code = _js_code("draw.js")
    assert "/cad/draw-direction" in code and "escapeHtml" in code
    assert not re.search(r"^(?:async )?function ", code, re.MULTILINE), "draw.js declares no global function"
    assert code.lstrip().startswith("(function () {")
    stripped = re.sub(r"`[^`]*`|\"[^\"\n]*\"", "", code)
    assert not re.search(r"[A-Za-z_\)\]]\s*[*/+-]\s*[A-Za-z_\(\d]", stripped), "no arithmetic in draw.js"
    for fragment in ("Math.", "toFixed", ".sort(", "undercut_faces <", "draft_deg <"):
        assert fragment not in code, fragment
    # Undercuts are marked with the viewer's existing flag marker, from the server's points.
    assert "markFlaggedFeatures" in code and "face.marker" in code
    for claim in ("highlight", "outline the face."):
        assert claim not in code.lower(), claim


def test_the_card_sits_in_the_cad_section_after_the_dfm_card_and_loads_after_dfm():
    markup = (SRC / "static" / "index.html").read_text(encoding="utf-8")
    assert markup.index('id="dfm-card"') < markup.index('id="draw-card"')
    assert markup.index('src="dfm.js"') < markup.index('src="draw.js"') < markup.index('src="estimateflow.js"')
    stripped = re.sub(r"<!--.*?-->", "", markup, flags=re.DOTALL)
    for element_id in ("draw-axis-x", "draw-axis-y", "draw-axis-z", "draw-show-undercuts", "dfm-forming-process"):
        assert stripped.count(f'id="{element_id}"') == 1, element_id


def test_the_dfm_panel_sends_the_stated_process_and_prints_the_pull_direction_it_read():
    code = _js_code("dfm.js")
    assert 'body.append("forming_process"' in code
    assert 'body.append("process_classification"' in code and 'body.append("pull_axis"' in code
    assert "report.draw" in code and "draw.not_measured_reason" in code
    assert 'at_least: "at or above"' in code
