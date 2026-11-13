"""Measured wall thickness, and the one DFM rule that reads it.

The measurement casts rays along each face's inward normal from a fixed grid of
sample points. Every fixture wall lies between two parallel planes or two
coaxial cylinders, where that ray meets the far face in an analytic
intersection, so values are asserted to 1e-6 mm: far looser than the float
rounding of a line/plane or line/cylinder solve (~1e-12 relative) and far
tighter than any manufacturing meaning, so a wrong pairing of faces cannot pass.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import trimesh
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cad_costing_agent import db as db_module
from cad_costing_agent import limits
from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.db_models import DfmProfileRow
from cad_costing_agent.dfm import (
    BASIS_PUBLISHED,
    DEFAULT_DFM_PROFILE_ID,
    SEED_DFM_PROFILES,
    GUIDE_URL,
    RuleId,
    WallEvidence,
    evaluate_dfm,
)
from cad_costing_agent.features import wall_thickness
from cad_costing_agent.features.topology import build_topology
from cad_costing_agent.features.wall_thickness import (
    HISTOGRAM_EDGES_MM,
    LIMITATIONS,
    measure_wall_thickness,
)
from cad_costing_agent.main import app
from tests import faceted_fixtures, iges_fixtures, step_fixtures
from tests.step_fixtures import WALL_FIXTURES

client = TestClient(app)
SRC = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
EXACT = 1e-6


def _measure(name: str, **options):
    builder, _ = WALL_FIXTURES[name]
    return measure_wall_thickness(read_step_shape(builder()), **options)


def _post(path: str, data: bytes, name: str = "part.step", **form):
    return client.post(path, files={"file": (name, data, "application/octet-stream")}, data=form)


def _wall(data: bytes, name: str = "part.step"):
    return _post("/cad/wall-thickness", data, name)


def _dfm(data: bytes, name: str = "part.step", **form):
    return _post("/cad/dfm", data, name, **form)


# --- the measurement, on exact fixtures ---------------------------------------------------


@pytest.mark.parametrize("name", sorted(WALL_FIXTURES))
def test_each_fixture_measures_its_known_minimum_wall(name):
    _, expected = WALL_FIXTURES[name]
    result = _measure(name)
    assert result.measured and result.not_measured_reason is None
    assert result.minimum.thickness_mm == pytest.approx(expected, abs=EXACT)
    assert result.rays_without_exit == 0
    assert not result.budget_limited


def test_the_pocket_floor_is_two_and_every_side_wall_three():
    result = _measure("box_with_pocket_walls")
    minima = sorted(face.minimum_mm for face in result.faces if face.minimum_mm is not None)
    # The pocket floor and the bottom face see each other at 2.0; the four outer sides
    # and the four pocket sides see each other at 3.0.
    assert minima == pytest.approx([2.0] * 2 + [3.0] * 8, abs=EXACT)
    floor_pair = {result.minimum.from_face, result.minimum.to_face}
    assert {face.face_id for face in result.faces if face.minimum_mm == pytest.approx(2.0)} == floor_pair
    assert result.minimum.direction.as_tuple() == pytest.approx((0.0, 0.0, 1.0))


def test_a_face_no_sample_lands_on_is_reported_not_skipped_silently():
    # The 3 mm rim round the pocket mouth: every point of a 5 x 5 grid over its
    # 50 x 40 parameter range falls in the opening, so no ray starts there.
    result = _measure("box_with_pocket_walls")
    assert len(result.faces_without_samples) == 1
    assert result.unmeasured_surface_types == {"plane": 1}
    assert result.faces_measured == result.face_count - 1


def test_face_ids_are_the_recognizers_face_numbers():
    builder, _ = WALL_FIXTURES["box_with_pocket_walls"]
    shape = read_step_shape(builder())
    result = measure_wall_thickness(shape)
    topology = build_topology(shape)
    first = topology.faces[result.minimum.from_face]
    second = topology.faces[result.minimum.to_face]
    assert first.surface_type == second.surface_type == "plane"
    # Opposite sides of one wall: outward normals point away from each other.
    assert first.normal.dot(second.normal) == pytest.approx(-1.0)
    assert abs(first.bbox_min.z - second.bbox_min.z) == pytest.approx(2.0, abs=EXACT)


def test_the_tube_wall_is_radial_between_the_two_cylinders():
    result = _measure("tube_3mm_wall")
    cylinders = [face for face in result.faces if face.surface_type == "cylinder"]
    assert len(cylinders) == 2
    assert all(face.minimum_mm == pytest.approx(3.0, abs=EXACT) for face in cylinders)
    assert result.minimum.direction.z == pytest.approx(0.0, abs=EXACT)


def test_the_rib_minimum_is_located_across_the_rib():
    span = _measure("thin_rib_part").minimum
    assert abs(span.direction.x) == pytest.approx(1.0)
    assert sorted((span.start.x, span.end.x)) == pytest.approx([29.7, 30.3], abs=EXACT)
    assert span.midpoint.x == pytest.approx(30.0, abs=EXACT)
    assert 10.0 < span.midpoint.z < 30.0


def test_a_plate_with_a_hole_has_no_thin_wall():
    result = _measure("plate_with_centre_hole")
    assert result.minimum.thickness_mm == pytest.approx(10.0, abs=EXACT)
    hole = next(face for face in result.faces if face.surface_type == "cylinder")
    assert hole.minimum_mm >= 20.0 - EXACT


def test_the_distribution_uses_fixed_bins_and_counts_every_sample():
    result = _measure("box_with_pocket_walls")
    assert [b.lower_mm for b in result.histogram] == [0.0, *HISTOGRAM_EDGES_MM]
    assert result.histogram[-1].upper_mm is None
    assert sum(b.samples for b in result.histogram) == sum(f.samples for f in result.faces)
    assert sum(f.samples for f in result.faces) == result.rays_cast
    two_to_three = next(b for b in result.histogram if b.lower_mm == 2.0)
    assert two_to_three.samples == 50  # 25 on the floor, 25 on the bottom face


def test_the_result_states_its_method_and_what_it_misses():
    result = _measure("thin_rib_part")
    assert result.method == "inward_normal_ray_grid"
    assert "inward normal" in result.method_description
    assert result.limitations == list(LIMITATIONS)
    text = " ".join(result.limitations)
    assert "between sample points" in text and "never thicker" in text
    assert "taper" in text and "Faceted exports" in text


@pytest.mark.parametrize("name", sorted(WALL_FIXTURES))
def test_measurement_is_deterministic(name):
    first = _measure(name).model_dump(exclude={"elapsed_ms"})
    second = _measure(name).model_dump(exclude={"elapsed_ms"})
    assert first == second


def test_an_inverted_solid_is_measured_the_same():
    builder, _ = WALL_FIXTURES["thin_rib_part"]
    shape = read_step_shape(builder())
    upright = measure_wall_thickness(shape)
    inverted = measure_wall_thickness(shape.Reversed())
    assert inverted.minimum.thickness_mm == pytest.approx(upright.minimum.thickness_mm, abs=EXACT)
    assert inverted.rays_without_exit == 0


def test_separate_solids_are_measured_separately():
    # Solid 1 is a 30 x 20 x 20 block with a Ø12 hole at its centre: 4 mm to its
    # long sides. A ray never crosses into the other solid.
    result = measure_wall_thickness(read_step_shape(step_fixtures.two_solids()))
    assert result.solid_count == 2
    assert result.minimum.thickness_mm == pytest.approx(4.0, abs=EXACT)
    assert {face.solid_index for face in result.faces} == {0, 1}


# --- budget ------------------------------------------------------------------------------


def test_over_budget_the_grid_is_coarsened_for_every_face_and_says_so():
    full = _measure("thin_rib_part")
    face_count = len(full.faces)
    coarse = _measure("thin_rib_part", max_rays=face_count * 4)
    assert coarse.budget_limited and coarse.grid_side == 2
    assert coarse.rays_cast <= face_count * 4
    assert "CAD_COSTING_MAX_WALL_RAYS" in coarse.budget_note
    assert "missed thin spot" in coarse.budget_note
    assert not full.budget_limited and full.budget_note is None and full.grid_side == 5


def test_below_one_ray_per_face_nothing_is_measured_and_the_reason_says_why():
    result = _measure("thin_rib_part", max_rays=3)
    assert not result.measured and result.budget_limited
    assert result.minimum is None and result.faces == []
    assert "CAD_COSTING_MAX_WALL_RAYS" in result.not_measured_reason
    assert "costing are unaffected" in result.not_measured_reason


def test_the_budget_is_a_configurable_limit_listed_with_the_others():
    source = (SRC / "limits.py").read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(source))
    assert "CAD_COSTING_MAX_WALL_RAYS" in docstring
    assert limits.MAX_WALL_RAYS > 0


def test_over_budget_degrades_only_the_wall_analysis(monkeypatch):
    builder, _ = WALL_FIXTURES["thin_rib_part"]
    data = builder()
    monkeypatch.setattr(limits, "MAX_WALL_RAYS", 3)

    response = _wall(data)
    assert response.status_code == 200
    body = response.json()
    assert body["measured"] is False and body["budget_limited"] is True

    report = _dfm(data).json()
    rules = {rule["rule_id"]: rule for rule in report["rules"]}
    assert rules["dfm_min_wall"]["evaluated"] is False
    assert "CAD_COSTING_MAX_WALL_RAYS" in rules["dfm_min_wall"]["skipped_reason"]
    assert all(rules[r]["evaluated"] for r in rules if r != "dfm_min_wall")
    assert not [f for f in report["flags"] if f["rule_id"] == "dfm_min_wall"]

    # The estimate is untouched: the costing path never measures walls.
    costed = _post(
        "/cost/cad-part",
        data,
        material="Steel",
        source_unit="mm",
        part_id="R1",
        part_name="Rib",
        quantity="1",
        operations="[]",
        rate_set_id="poc-default",
    )
    assert costed.status_code == 200, costed.text


# --- the endpoint and what it refuses -------------------------------------------------------


def test_the_endpoint_returns_the_same_measurement_as_the_module():
    builder, expected = WALL_FIXTURES["box_with_pocket_walls"]
    body = _wall(builder()).json()
    direct = _measure("box_with_pocket_walls").model_dump(mode="json", exclude={"elapsed_ms"})
    body.pop("elapsed_ms")
    direct.pop("filename")
    body.pop("filename")
    assert body == direct
    assert body["minimum"]["thickness_mm"] == pytest.approx(expected, abs=EXACT)


def test_the_endpoint_is_deterministic():
    builder, _ = WALL_FIXTURES["tube_3mm_wall"]
    data = builder()
    first, second = _wall(data).json(), _wall(data).json()
    first.pop("elapsed_ms")
    second.pop("elapsed_ms")
    assert first == second


def test_a_faceted_export_is_refused_with_the_reason():
    response = _wall(faceted_fixtures.faceted_plain_block(), "faceted.step")
    assert response.status_code == 400
    assert "faceted" in response.json()["detail"]
    assert "B-Rep" in response.json()["detail"]


def test_an_stl_is_refused_like_feature_recognition():
    stl = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")
    response = _wall(stl, "cube.stl")
    assert response.status_code == 400
    assert response.json()["detail"] == _post("/cad/features", stl, "cube.stl").json()["detail"]


def test_an_iges_solid_is_measured_and_an_iges_surface_model_is_refused():
    solid = _wall(iges_fixtures.block_with_hole_solid(), "block.igs")
    assert solid.status_code == 200
    # 40 x 30 x 20 with a centred R5 through hole: 10 mm from the hole to the long sides.
    assert solid.json()["minimum"]["thickness_mm"] == pytest.approx(10.0, abs=EXACT)

    surfaces = _wall(iges_fixtures.block_with_hole_surfaces(), "block.igs")
    assert surfaces.status_code == 400
    assert "loose trimmed surfaces" in surfaces.json()["detail"]


def test_refusal_reasons_follow_the_recognition_representation_alone():
    assert wall_thickness.refusal_reason("brep", True) is None
    for representation, has_shape in (("faceted", False), ("surface_model", True), ("brep", False)):
        assert wall_thickness.refusal_reason(representation, has_shape)


def test_measuring_walls_stores_nothing():
    builder, _ = WALL_FIXTURES["thin_rib_part"]
    total = client.get("/estimates").json()["total"]
    assert _wall(builder()).status_code == 200
    assert client.get("/estimates").json()["total"] == total


# --- the minimum-wall DFM rule ------------------------------------------------------------


def _wall_flags(body: dict) -> list[dict]:
    return [flag for flag in body["flags"] if flag["rule_id"] == "dfm_min_wall"]


def test_a_thin_rib_raises_one_minimum_wall_flag_with_its_measured_location():
    builder, _ = WALL_FIXTURES["thin_rib_part"]
    data = builder()
    body = _dfm(data).json()
    assert body["dfm_profile_version"] == "1.1.0"
    (flag,) = _wall_flags(body)
    assert flag["measured_value"] == pytest.approx(0.6, abs=EXACT)
    assert flag["measured_unit"] == "mm" and flag["comparison"] == "less_than"
    assert flag["threshold"] == 0.8
    assert flag["basis"] == BASIS_PUBLISHED and flag["source_url"] == GUIDE_URL
    assert flag["evidence_type"] == "brep" and flag["evidence_note"] is None
    # No recognized feature is a wall, so none is named; the marker is the measurement's own.
    assert flag["feature_ids"] == []
    minimum = _wall(data).json()["minimum"]
    assert flag["marker"]["location"] == minimum["midpoint"]
    assert flag["marker"]["axis"] == minimum["direction"]
    assert "0.6 mm" in flag["explanation"] and "never thicker" in flag["explanation"]
    for word in ("severity", "confidence"):
        assert word not in flag
    assert body["wall"]["minimum_mm"] == pytest.approx(0.6, abs=EXACT)
    assert body["wall"]["limitations"] == list(LIMITATIONS)


@pytest.mark.parametrize("name", ["box_with_pocket_walls", "tube_3mm_wall", "plate_with_centre_hole"])
def test_walls_at_or_above_the_threshold_raise_no_flag_but_are_checked(name):
    builder, expected = WALL_FIXTURES[name]
    body = _dfm(builder()).json()
    assert _wall_flags(body) == []
    rule = next(r for r in body["rules"] if r["rule_id"] == "dfm_min_wall")
    assert rule["evaluated"] and rule["features_checked"] == len(body["wall"]["face_minima"]) > 0
    assert body["wall"]["minimum_mm"] == pytest.approx(expected, abs=EXACT)


def test_the_rule_fires_strictly_below_the_threshold():
    profile = SEED_DFM_PROFILES[1]
    at = WallEvidence(measured=True, minimum_mm=0.8, face_minima=[(1, 0.8)])
    below = WallEvidence(measured=True, minimum_mm=0.79, face_minima=[(1, 0.79), (2, 0.79), (3, 5.0)])
    assert [f for f in evaluate_dfm([], profile, wall=at).flags] == []
    (flag,) = evaluate_dfm([], profile, wall=below).flags
    assert "2 measured face(s)" in flag.explanation
    assert flag.marker is None  # no location was handed in, so none is invented


def test_the_rule_is_not_evaluated_when_walls_were_not_measured():
    profile = SEED_DFM_PROFILES[1]
    report = evaluate_dfm([], profile)
    rule = next(r for r in report.rules if r.rule_id is RuleId.MIN_WALL)
    assert not rule.evaluated and "not measured" in rule.skipped_reason
    refused = evaluate_dfm([], profile, wall=WallEvidence(measured=False, not_measured_reason="Because."))
    rule = next(r for r in refused.rules if r.rule_id is RuleId.MIN_WALL)
    assert not rule.evaluated and rule.skipped_reason == "Because."


def test_a_faceted_part_keeps_its_other_flags_and_says_why_walls_were_not_checked():
    body = _dfm(faceted_fixtures.faceted_plate_with_four_holes(), "faceted.step").json()
    rules = {rule["rule_id"]: rule for rule in body["rules"]}
    assert rules["dfm_min_wall"]["evaluated"] is False
    assert "faceted" in rules["dfm_min_wall"]["skipped_reason"]
    assert rules["dfm_deep_hole"]["evaluated"] is True
    assert body["wall"]["measured"] is False


def test_profile_1_0_0_never_measures_walls(monkeypatch):
    calls = []
    real = wall_thickness.measure_wall_thickness

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(wall_thickness, "measure_wall_thickness", counting)
    builder, _ = WALL_FIXTURES["thin_rib_part"]
    body = _dfm(builder(), dfm_profile_version="1.0.0").json()
    assert calls == []
    assert body["wall"] is None and _wall_flags(body) == []
    # The same request under 1.1.0 does measure, through the same function.
    _dfm(builder())
    assert calls == [1]
    rule = next(r for r in body["rules"] if r["rule_id"] == "dfm_min_wall")
    assert rule["evaluated"] is False and "sets no threshold" in rule["skipped_reason"]


def test_a_wall_flag_is_listed_beside_feature_flags_without_touching_flagged_features():
    # The deep hole names a feature; the thin rib names none.
    from tests.step_fixtures import _box, _cut, _cylinder, _fuse, to_step

    shape = _fuse(_box(60.0, 40.0, 30.0), _box(0.6, 40.0, 20.0, at=(29.7, 0.0, 30.0)))
    shape = _cut(shape, _cylinder(2.5, 40.0, (10.0, 20.0, 5.0)))
    body = _dfm(to_step(shape)).json()
    rules = {flag["rule_id"] for flag in body["flags"]}
    assert rules == {"dfm_deep_hole", "dfm_min_wall"}
    named = {fid for flag in body["flags"] for fid in flag["feature_ids"]}
    assert {f["feature_id"] for f in body["flagged_features"]} == named


# --- versioned thresholds ----------------------------------------------------------------


def test_1_0_0_is_unchanged_and_1_1_0_adds_only_the_wall_rule():
    first, second = SEED_DFM_PROFILES
    assert (first.version, second.version) == ("1.0.0", "1.1.0")
    assert [r.rule_id for r in first.rules] == [
        RuleId.DEEP_HOLE,
        RuleId.DEEP_NARROW_RECESS,
        RuleId.SMALL_INTERNAL_RADIUS,
        RuleId.MANY_HOLE_DIAMETERS,
    ]
    assert [r.threshold for r in first.rules] == [4.0, 4.0, 0.5, 6.0]
    assert second.rules[:4] == first.rules
    (added,) = second.rules[4:]
    assert added.rule_id is RuleId.MIN_WALL and added.threshold == 0.8 and added.unit == "mm"
    assert added.basis == BASIS_PUBLISHED and added.source_url == GUIDE_URL
    assert "0.8 mm" in added.source_quote


def _engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def _profiles(engine) -> list[tuple]:
    with sessionmaker(bind=engine)() as session:
        rows = session.execute(select(DfmProfileRow)).scalars().all()
        return sorted(
            (r.dfm_profile_id, r.version, r.name, r.description, r.is_illustrative, r.rules)
            for r in rows
        )


def test_a_fresh_database_and_one_seeded_before_1_1_0_end_up_identical():
    from cad_costing_agent.repository import DfmProfileRepository

    fresh = _engine()
    db_module.init_db(fresh)

    existing = _engine()
    db_module.run_migrations(existing)
    with sessionmaker(bind=existing)() as session:
        assert DfmProfileRepository(session).seed(SEED_DFM_PROFILES[:1]) == 1
        session.commit()
    before = _profiles(existing)
    assert [row[1] for row in before] == ["1.0.0"]

    db_module.init_db(existing)
    after = _profiles(existing)
    assert after == _profiles(fresh)
    assert [row[1] for row in after] == ["1.0.0", "1.1.0"]
    # The stored 1.0.0 row is the one seeded before; nothing rewrote it.
    assert after[0] == before[0]


def test_the_latest_default_profile_is_1_1_0_and_1_0_0_stays_fetchable():
    latest = client.get(f"/dfm-profiles/{DEFAULT_DFM_PROFILE_ID}").json()
    assert latest["version"] == "1.1.0"
    old = client.get(f"/dfm-profiles/{DEFAULT_DFM_PROFILE_ID}", params={"version": "1.0.0"}).json()
    assert "dfm_min_wall" not in {r["rule_id"] for r in old["rules"]}


# --- structural boundaries ---------------------------------------------------------------


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


def test_the_measuring_module_imports_only_limits_and_the_feature_model_and_tolerance():
    named = _imports(SRC / "features" / "wall_thickness.py")
    assert named == {
        "cad_costing_agent",
        "cad_costing_agent.limits",
        "cad_costing_agent.features.model",
        "cad_costing_agent.features.model.OUTPUT_DECIMALS",
        "cad_costing_agent.features.model.Point3D",
        "cad_costing_agent.features.topology",
        "cad_costing_agent.features.topology.LINEAR_TOLERANCE_MM",
    }, named


def test_only_main_reads_the_wall_measurement():
    importers = set()
    for path in SRC.rglob("*.py"):
        if path.name == "wall_thickness.py":
            continue
        if any(m.endswith("wall_thickness") for m in _imports(path)):
            importers.add(path.relative_to(SRC).as_posix())
    assert importers == {"main.py"}, importers


def test_dfm_never_reaches_the_measuring_code():
    assert not any("wall_thickness" in m for m in _imports(SRC / "dfm.py"))
    assert "OCP" not in _code_without_prose(SRC / "dfm.py")


def test_the_measurement_has_no_randomness_and_no_cost_or_time_vocabulary():
    code = _code_without_prose(SRC / "features" / "wall_thickness.py")
    for forbidden in ("random", "rate_set", "RateSet", "calculate_", "cycle_time", "setup_time", "session"):
        assert forbidden not in code, forbidden


def test_the_main_section_wires_the_measurement_into_nothing_but_dfm():
    source = (SRC / "main.py").read_text(encoding="utf-8")
    banner = "# ------------------------------------------------------------- wall thickness"
    assert source.count(banner) == 1
    dfm_start = source.index('@app.post("/cad/dfm"')
    dfm_end = source.index("\n@app.", dfm_start + 1)
    section = source.index(banner)
    calls = [
        m.start()
        for m in re.finditer(r"_wall_evidence\(|_measure_walls\(|measure_wall_thickness\(", source)
    ]
    assert calls
    outside = [p for p in calls if not (dfm_start < p < dfm_end or p > section)]
    assert outside == [], "walls are measured only in their own section and in /cad/dfm"


# --- the panel ---------------------------------------------------------------------------


def _js_code(name: str) -> str:
    source = (SRC / "static" / name).read_text(encoding="utf-8")
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL))


def test_the_dfm_panel_prints_the_servers_wall_and_points_at_its_marker():
    code = _js_code("dfm.js")
    assert "report.wall" in code and "dfmWallMarkup" in code
    for field in ("minimum_mm", "method_description", "limitations", "not_measured_reason", "budget_note"):
        assert f"wall.{field}" in code, field
    # A measured point goes to the viewer's marker, never through the feature selection.
    assert "flag.marker" in code and "features.concat(markers)" in code
    assert "if (features.length && typeof selectFeatures" in code
    # Nothing compared or derived in the browser.
    for fragment in ("minimum_mm <", "minimum_mm >", "minimum_mm -", "Math.", "toFixed"):
        assert fragment not in code, fragment
    markup = (SRC / "static" / "index.html").read_text(encoding="utf-8")
    markup = re.sub(r"<!--.*?-->", "", markup, flags=re.DOTALL)
    assert markup.count('id="dfm-wall"') == 1
    assert "halfway through the thinnest wall measured" in markup
