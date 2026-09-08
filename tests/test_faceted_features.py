"""Faceted STEP: classification, reconstruction, and the limits of both.

The real files that motivated this are 200 MB Creo assembly exports, far too
large to unit-test. So the same kind of geometry is generated locally at a size
a test can assert on: a known shape is tessellated and written out as
`FACETED_BREP` polygons with every analytic surface discarded, exactly as a
real faceted export discards them. A 10 mm hole tessellated into polygons is
still a 10 mm hole, so these tests require the reconstruction to recover
10 mm — not merely "a hole".

The negative cases carry as much weight as the positive ones. A hole recovered
by fitting is a measurement, not a stored number, and the point of the fitting
rules is to know when *not* to claim one.
"""

from functools import lru_cache

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient

from cad_costing_agent import limits
from cad_costing_agent.features import MAX_FACETS, recognize_features_from_bytes
from cad_costing_agent.cad_faceted import parse_faceted_step
from cad_costing_agent.features.model import Confidence, FeatureType
from cad_costing_agent.features.step_text import (
    BREP,
    EMPTY,
    FACETED,
    WIREFRAME,
    classify_step_bytes,
)
from cad_costing_agent.main import app
from tests.faceted_fixtures import (
    ALL_FACETED_FIXTURES,
    faceted_block_with_blind_hole,
    faceted_block_with_boss,
    faceted_plain_block,
    faceted_plate_with_four_holes,
    faceted_tube,
)
from tests.step_fixtures import ALL_FIXTURES

client = TestClient(app)


@lru_cache(maxsize=None)
def faceted(name: str) -> bytes:
    return ALL_FACETED_FIXTURES[name]()


@lru_cache(maxsize=None)
def analysed(name: str):
    return recognize_features_from_bytes(faceted(name), f"{name}.step")


def features_of(result, feature_type: FeatureType):
    return [feature for feature in result.features if feature.feature_type is feature_type]


# --- what is in the file ----------------------------------------------------


def test_an_analytic_export_is_classified_as_brep():
    report = classify_step_bytes(ALL_FIXTURES["block_with_through_hole"]())

    assert report.representation == BREP
    assert report.is_brep and not report.is_faceted
    assert report.analytic_surface_count > 0
    assert report.facet_count == 0


def test_a_faceted_export_is_classified_as_faceted():
    report = classify_step_bytes(faceted("faceted_plate_with_four_holes"))

    assert report.representation == FACETED
    assert report.is_faceted and not report.is_brep
    assert report.analytic_surface_count == 0
    assert report.facet_count > 0
    assert report.shell_count == 1
    assert any("no analytic surfaces" in note for note in report.notes)


def test_the_schema_is_read_from_the_header():
    report = classify_step_bytes(faceted("faceted_plain_block"))

    assert report.schema is not None
    assert "AUTOMOTIVE_DESIGN" in report.schema


def test_the_open_shell_defect_is_detected_and_explained():
    """FACETED_BREP over an OPEN_SHELL is why OpenCASCADE transfers nothing."""
    report = classify_step_bytes(faceted_plate_with_four_holes(open_shell=True))

    assert report.faceted_brep_on_open_shell is True
    assert any("CLOSED_SHELL" in note and "ISO 10303-42" in note for note in report.notes)


def test_a_valid_faceted_export_is_not_flagged_with_the_defect():
    assert classify_step_bytes(faceted("faceted_plain_block")).faceted_brep_on_open_shell is False


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"ISO-10303-21;HEADER;ENDSEC;DATA;ENDSEC;END-ISO-10303-21;", EMPTY),
        (b"#1=CARTESIAN_POINT('',(0.,0.,0.));#2=CARTESIAN_POINT('',(1.,0.,0.));", WIREFRAME),
    ],
)
def test_files_with_no_surfaces_are_classified_as_such(data, expected):
    assert classify_step_bytes(data).representation == expected


def test_classification_does_not_need_the_file_to_be_loadable():
    """The point of the byte scan: it answers before anything heavy is tried."""
    broken = faceted_plate_with_four_holes(open_shell=True)

    report = classify_step_bytes(broken)

    assert report.is_faceted
    assert report.facet_count > 0


# --- reconstruction: what it gets right -------------------------------------


def test_four_holes_are_recovered_from_polygons_with_their_real_diameter():
    """The worked example, reconstructed rather than read."""
    result = analysed("faceted_plate_with_four_holes")
    holes = features_of(result, FeatureType.HOLE)

    assert len(holes) == 4
    for hole in holes:
        assert hole.subtype == "through"
        assert hole.dimensions["diameter_mm"] == pytest.approx(10.0, abs=1e-3)
        assert hole.dimensions["depth_mm"] == pytest.approx(10.0, abs=1e-3)
        assert hole.axis.as_tuple() == (0.0, 0.0, 1.0)
        assert hole.confidence is Confidence.HIGH
        assert hole.source_type == "faceted"


def test_a_blind_hole_is_still_told_apart_from_a_through_one():
    hole = features_of(analysed("faceted_block_with_blind_hole"), FeatureType.HOLE)[0]

    assert hole.subtype == "blind"
    assert hole.dimensions["diameter_mm"] == pytest.approx(16.0, abs=1e-3)
    assert hole.dimensions["depth_mm"] == pytest.approx(12.0, abs=1e-3)
    assert any("floor" in line for line in hole.evidence)


def test_a_boss_is_a_shaft_and_not_a_hole():
    result = analysed("faceted_block_with_boss")

    assert features_of(result, FeatureType.HOLE) == []
    shaft = features_of(result, FeatureType.CYLINDER)[0]
    assert shaft.subtype == "shaft"
    assert shaft.dimensions["diameter_mm"] == pytest.approx(30.0, abs=1e-3)
    assert shaft.dimensions["length_mm"] == pytest.approx(12.0, abs=1e-3)


def test_coplanar_facets_are_merged_back_into_whole_faces():
    """A block is six faces, not twelve triangles."""
    result = analysed("faceted_plain_block")

    planes = features_of(result, FeatureType.PLANE)
    assert len(planes) == 6
    areas = sorted(round(plane.dimensions["area_mm2"], 3) for plane in planes)
    assert areas == [800.0, 800.0, 1200.0, 1200.0, 2400.0, 2400.0]


def test_a_plain_block_yields_no_holes_from_facets():
    result = analysed("faceted_plain_block")

    assert result.feature_counts == {"plane": 6}


def test_bore_strips_belong_to_the_hole_not_to_dozens_of_flat_regions():
    """Reporting a bore as 33 tiny planes would be true and useless."""
    result = analysed("faceted_plate_with_four_holes")

    assert len(features_of(result, FeatureType.PLANE)) == 6
    assert len(features_of(result, FeatureType.HOLE)) == 4


# --- reconstruction: what it refuses to claim -------------------------------


def test_a_coarse_polygon_is_not_reported_as_a_round_hole():
    """An eight-sided tube is a polygon, not a tessellated circle."""
    result = recognize_features_from_bytes(faceted_tube(sides=8), "octagon.step")

    assert features_of(result, FeatureType.HOLE) == []
    assert features_of(result, FeatureType.CYLINDER) == []


def test_a_finely_tessellated_tube_is_recognized_as_cylindrical():
    """The same shape with enough segments is a circle, and is claimed."""
    result = recognize_features_from_bytes(faceted_tube(sides=48), "tube.step")

    cylindrical = features_of(result, FeatureType.CYLINDER) + features_of(result, FeatureType.HOLE)
    assert len(cylindrical) == 1
    assert cylindrical[0].dimensions["diameter_mm"] == pytest.approx(20.0, abs=1e-3)


def test_pockets_and_blends_are_not_reconstructed_from_facets():
    """Stated as a limitation rather than guessed at."""
    result = analysed("faceted_plate_with_four_holes")

    assert features_of(result, FeatureType.POCKET) == []
    assert features_of(result, FeatureType.SLOT) == []
    assert features_of(result, FeatureType.FILLET) == []
    assert features_of(result, FeatureType.CHAMFER) == []
    assert any("not reconstructed from faceted geometry" in note for note in result.warnings)


# --- the normalized model is shared with the analytic path ------------------


def test_both_paths_produce_the_same_feature_model():
    analytic = recognize_features_from_bytes(
        ALL_FIXTURES["plate_with_four_holes"](), "analytic.step"
    )
    from_facets = analysed("faceted_plate_with_four_holes")

    assert set(analytic.model_dump()) == set(from_facets.model_dump())
    assert {f.feature_type for f in analytic.features} >= {FeatureType.HOLE}
    for feature in analytic.features:
        assert feature.source_type == "brep"
    for feature in from_facets.features:
        assert feature.source_type == "faceted"


def test_the_result_says_which_representation_it_came_from():
    assert analysed("faceted_plain_block").representation == "faceted"
    assert (
        recognize_features_from_bytes(ALL_FIXTURES["plain_block"](), "b.step").representation
        == "brep"
    )


def test_faceted_features_are_traceable_to_their_polygons():
    """A faceted model has no CAD faces to point at, but the facets are there."""
    hole = features_of(analysed("faceted_block_with_blind_hole"), FeatureType.HOLE)[0]

    assert hole.source_facets
    assert hole.source_faces == []


@pytest.mark.parametrize("name", sorted(ALL_FACETED_FIXTURES))
def test_every_faceted_feature_explains_itself(name):
    for feature in analysed(name).features:
        assert feature.detection_method
        assert feature.evidence
        assert feature.confidence in set(Confidence)


def test_a_reconstructed_hole_says_how_it_was_measured():
    hole = features_of(analysed("faceted_plate_with_four_holes"), FeatureType.HOLE)[0]

    assert hole.detection_method == "facet_band_fitted_to_a_cylinder"
    assert any("circle fitted" in line for line in hole.evidence)
    assert any("tessellation places vertices on the true surface" in line for line in hole.evidence)


# --- determinism ------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ALL_FACETED_FIXTURES))
def test_faceted_recognition_is_deterministic(name):
    data = faceted(name)

    first = recognize_features_from_bytes(data, f"{name}.step").model_dump(exclude={"elapsed_ms"})
    second = recognize_features_from_bytes(data, f"{name}.step").model_dump(exclude={"elapsed_ms"})

    assert first == second


# --- multiple bodies --------------------------------------------------------


def test_each_shell_is_reported_as_its_own_body():
    from tests.faceted_fixtures import faceted_step_from_mesh

    tube = faceted_tube(sides=32)
    model = parse_faceted_step(tube)

    assert len(model.shells) == 1
    assert model.shells[0].triangle_count == 64


def test_a_file_with_no_shell_structure_still_yields_geometry():
    """Some writers omit shells; the polygons are still usable."""
    from tests.faceted_fixtures import faceted_step_from_mesh

    positions = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    triangles = np.array([[0, 1, 2]])
    data = faceted_step_from_mesh(positions, triangles)
    without_shell = data.replace(b"CLOSED_SHELL", b"UNKNOWN_GROUPING")

    model = parse_faceted_step(without_shell)

    assert len(model.shells) == 1
    assert any("no shell entities" in warning for warning in model.warnings)


# --- robustness -------------------------------------------------------------


def test_a_faceted_model_over_the_budget_is_refused_with_its_size(monkeypatch):
    import cad_costing_agent.features as features_module

    monkeypatch.setattr(features_module, "MAX_FACETS", 4)

    with pytest.raises(ValueError) as caught:
        recognize_features_from_bytes(faceted("faceted_plate_with_four_holes"), "big.step")

    message = str(caught.value)
    assert "polygons" in message
    assert "Geometry and costing are unaffected" in message


def test_the_parser_refuses_an_oversized_model_before_building_anything():
    with pytest.raises(ValueError, match="above the"):
        parse_faceted_step(faceted("faceted_plate_with_four_holes"), max_facets=4)


def test_the_facet_budget_is_sane():
    assert 1_000 < MAX_FACETS <= 5_000_000


@pytest.mark.parametrize(
    "filename, data, expected",
    [
        ("part.step", b"this is not STEP data", "not valid"),
        ("part.stp", b"", "empty"),
        ("part.iges", b"whatever", "not valid IGES data"),
    ],
)
def test_bad_input_still_fails_cleanly(filename, data, expected):
    with pytest.raises(ValueError, match=expected):
        recognize_features_from_bytes(data, filename)


def test_a_wireframe_export_is_refused_with_a_reason():
    points = b"ISO-10303-21;\nDATA;\n#1=CARTESIAN_POINT('',(0.,0.,0.));\nENDSEC;\n"

    with pytest.raises(ValueError, match="no surface geometry"):
        recognize_features_from_bytes(points, "wire.step")


# --- limits are separate ----------------------------------------------------


def test_upload_parsing_and_viewer_limits_are_independent():
    """A model too complex to draw must still be costable."""
    assert limits.MAX_UPLOAD_BYTES > 0
    assert limits.MAX_BREP_FACES > 0
    assert limits.MAX_FACETS > 0
    assert limits.MAX_VIEWER_TRIANGLES > 0
    # The viewer's budget is the smallest and does not constrain the others.
    assert limits.MAX_VIEWER_TRIANGLES < limits.MAX_FACETS
    assert limits.MAX_UPLOAD_BYTES >= 200 * 1024 * 1024


@pytest.mark.parametrize(
    "variable, attribute",
    [
        ("CAD_COSTING_MAX_UPLOAD_MB", "MAX_UPLOAD_MB"),
        ("CAD_COSTING_MAX_BREP_FACES", "MAX_BREP_FACES"),
        ("CAD_COSTING_MAX_FACETS", "MAX_FACETS"),
        ("CAD_COSTING_MAX_VIEWER_TRIANGLES", "MAX_VIEWER_TRIANGLES"),
    ],
)
def test_every_limit_can_be_configured_by_environment(monkeypatch, variable, attribute):
    import importlib

    monkeypatch.setenv(variable, "1234")
    reloaded = importlib.reload(limits)
    try:
        assert getattr(reloaded, attribute) == 1234
    finally:
        monkeypatch.delenv(variable, raising=False)
        importlib.reload(limits)


def test_an_unusable_limit_override_falls_back_to_the_default(monkeypatch):
    import importlib

    monkeypatch.setenv("CAD_COSTING_MAX_FACETS", "not-a-number")
    reloaded = importlib.reload(limits)
    try:
        assert reloaded.MAX_FACETS > 0
    finally:
        monkeypatch.delenv("CAD_COSTING_MAX_FACETS", raising=False)
        importlib.reload(limits)


def test_geometry_refuses_a_step_file_that_yielded_no_surfaces():
    """Zero volume must not be returned as a successful measurement.

    Found live: a 198 MB Creo export whose FACETED_BREP entities sit over an
    OPEN_SHELL transfers nothing, and `/cad/geometry` answered 200 with
    volume 0.0 — which reads as "this part has no volume" and only failed much
    later, during costing, as "volume must be positive".
    """
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    from cad_costing_agent.cad_geometry import _extract_step_geometry

    builder = BRep_Builder()
    empty = TopoDS_Compound()
    builder.MakeCompound(empty)

    import cad_costing_agent.cad_geometry as geometry_module

    original = geometry_module.read_step_shape
    geometry_module.read_step_shape = lambda data: empty
    try:
        with pytest.raises(ValueError) as caught:
            _extract_step_geometry(b"anything")
    finally:
        geometry_module.read_step_shape = original

    message = str(caught.value)
    assert "no surfaces" in message
    assert "OPEN_SHELL" in message
    assert "Re-export" in message


# --- the viewer and costing stay independent --------------------------------


def test_a_model_too_complex_to_render_is_still_costable(monkeypatch):
    """A viewer limit must never decide whether a part can be priced."""
    import cad_costing_agent.cad_mesh as cad_mesh

    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 2)
    mesh = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")

    preview = client.post("/cad/mesh", files={"file": ("c.stl", mesh)})
    assert preview.status_code == 200
    assert preview.json()["mesh"] is None
    assert "only the 3D preview is unavailable" in preview.json()["mesh_error"]

    costed = client.post(
        "/cost/cad-part",
        files={"file": ("c.stl", mesh, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Cube",
            "quantity": "5",
            "rate_set_id": "poc-machine-shop",
        },
    )
    assert costed.status_code == 200
    assert costed.json()["part_cost"]["total_part_cost"] > 0


def test_feature_recognition_failure_cannot_break_costing():
    """Recognition is a separate branch; its failure is not costing's problem."""
    mesh = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")

    assert client.post("/cad/features", files={"file": ("c.stl", mesh)}).status_code == 400

    costed = client.post(
        "/cost/cad-part",
        files={"file": ("c.stl", mesh, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Cube",
            "quantity": "1",
            "rate_set_id": "poc-machine-shop",
        },
    )
    assert costed.status_code == 200


def test_analysing_a_faceted_model_does_not_change_any_cost():
    mesh = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")
    form = {
        "material": "Steel",
        "source_unit": "mm",
        "part_id": "P-1",
        "part_name": "Cube",
        "quantity": "10",
        "rate_set_id": "poc-machine-shop",
    }

    def cost():
        return client.post(
            "/cost/cad-part",
            files={"file": ("c.stl", mesh, "application/octet-stream")},
            data=form,
        ).json()["part_cost"]["total_part_cost"]

    before = cost()
    client.post(
        "/cad/features",
        files={"file": ("plate.step", faceted("faceted_plate_with_four_holes"))},
    )
    assert cost() == before


# --- the endpoint -----------------------------------------------------------


def test_the_endpoint_reconstructs_features_from_a_faceted_upload():
    response = client.post(
        "/cad/features",
        files={"file": ("plate.step", faceted("faceted_plate_with_four_holes"))},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["representation"] == "faceted"
    assert body["facet_count"] > 0
    assert body["feature_counts"]["hole"] == 4
    assert all(
        feature["source_type"] == "faceted"
        for feature in body["features"]
        if feature["feature_type"] == "hole"
    )


def test_the_endpoint_still_reports_brep_for_an_analytic_upload():
    response = client.post(
        "/cad/features",
        files={"file": ("block.step", ALL_FIXTURES["block_with_through_hole"]())},
    )

    body = response.json()
    assert body["representation"] == "brep"
    assert body["features"][0]["source_type"] == "brep"


def test_no_ocp_class_names_leak_into_the_api():
    body = client.post(
        "/cad/features",
        files={"file": ("plate.step", faceted("faceted_plate_with_four_holes"))},
    ).json()

    blob = str(body)
    for leaked in ("TopoDS", "OCP.", "gp_", "BRep", "Geom_", "GeomAbs"):
        assert leaked not in blob
