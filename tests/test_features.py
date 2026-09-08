"""Deterministic CAD feature recognition.

Each fixture in `step_fixtures.py` is built from primitives whose features are
known exactly, so these tests assert real numbers — Ø10.0, depth 12.0 — rather
than "some holes were found". The negative cases matter as much as the
positive ones: a plain block must produce no pocket, and an STL must be refused
rather than guessed at.
"""

from functools import lru_cache

import pytest
import trimesh
from fastapi.testclient import TestClient

from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.features import (
    MAX_FACES,
    recognize_features,
    recognize_features_from_bytes,
)
from cad_costing_agent.features.model import Confidence, FeatureType
from cad_costing_agent.features.recognizers import (
    ChamferRecognizer,
    CylinderRecognizer,
    FilletRecognizer,
    HoleRecognizer,
    PlanarFaceRecognizer,
    PocketRecognizer,
    RecognitionContext,
)
from cad_costing_agent.features.topology import build_topology
from cad_costing_agent.main import STATIC_PATH, app
from tests.step_fixtures import ALL_FIXTURES

client = TestClient(app)


@lru_cache(maxsize=None)
def step(name: str) -> bytes:
    """Fixture bytes, built once per session — each costs an OCC boolean."""
    return ALL_FIXTURES[name]()


@lru_cache(maxsize=None)
def analysed(name: str):
    return recognize_features_from_bytes(step(name), f"{name}.step")


def topology_of(name: str):
    return build_topology(read_step_shape(step(name)))


def features_of(result, feature_type: FeatureType):
    return [feature for feature in result.features if feature.feature_type is feature_type]


def only(result, feature_type: FeatureType):
    matches = features_of(result, feature_type)
    assert len(matches) == 1, f"expected exactly one {feature_type.value}, got {len(matches)}"
    return matches[0]


# --- the recognizers do not invent features ---------------------------------


def test_a_plain_block_has_six_planes_and_nothing_else():
    result = analysed("plain_block")

    assert result.feature_counts == {"plane": 6}
    assert result.solid_count == 1
    assert result.unclassified_face_count == 0


def test_a_blocks_top_face_is_not_mistaken_for_a_pocket_floor():
    """The one condition that separates a recess from an ordinary outer face.

    A block's top face is also ringed by perpendicular planes; what makes it
    not a pocket is that those walls hang below it rather than rising from it.
    """
    result = analysed("plain_block")

    assert features_of(result, FeatureType.POCKET) == []
    assert features_of(result, FeatureType.SLOT) == []


# --- holes ------------------------------------------------------------------


def test_a_through_hole_is_found_with_its_diameter_depth_and_axis():
    hole = only(analysed("block_with_through_hole"), FeatureType.HOLE)

    assert hole.subtype == "through"
    assert hole.dimensions["diameter_mm"] == pytest.approx(10.0, abs=1e-6)
    assert hole.dimensions["depth_mm"] == pytest.approx(20.0, abs=1e-6)
    assert hole.axis.as_tuple() == (0.0, 0.0, 1.0)
    assert hole.confidence is Confidence.HIGH


def test_four_identical_through_holes_are_all_found():
    result = analysed("plate_with_four_holes")
    holes = features_of(result, FeatureType.HOLE)

    assert len(holes) == 4
    assert all(hole.subtype == "through" for hole in holes)
    assert all(hole.dimensions["diameter_mm"] == pytest.approx(10.0, abs=1e-6) for hole in holes)
    assert all(hole.dimensions["depth_mm"] == pytest.approx(10.0, abs=1e-6) for hole in holes)
    assert all(hole.axis.as_tuple() == (0.0, 0.0, 1.0) for hole in holes)
    # Four separate holes, not one hole claimed four times.
    assert len({tuple(hole.source_faces) for hole in holes}) == 4


def test_identical_holes_are_summarised_as_one_line():
    summary = analysed("plate_with_four_holes").summary

    assert any(line.startswith("4 × hole (through) Ø10 mm") for line in summary), summary


def test_a_blind_hole_is_distinguished_from_a_through_hole():
    hole = only(analysed("block_with_blind_hole"), FeatureType.HOLE)

    assert hole.subtype == "blind"
    assert hole.dimensions["diameter_mm"] == pytest.approx(8.0, abs=1e-6)
    assert hole.dimensions["depth_mm"] == pytest.approx(12.0, abs=1e-6)
    assert hole.confidence is Confidence.HIGH
    assert any("floor of the bore" in line for line in hole.evidence)


def test_a_blind_holes_floor_belongs_to_the_hole_not_to_a_separate_plane():
    result = analysed("block_with_blind_hole")
    hole = only(result, FeatureType.HOLE)

    # Cylinder plus floor.
    assert len(hole.source_faces) == 2
    # The block still has exactly its six outer faces as planes.
    assert result.feature_counts["plane"] == 6


def test_a_counterbore_is_reported_as_one_hole_with_both_diameters():
    hole = only(analysed("block_with_counterbore"), FeatureType.HOLE)

    assert hole.subtype == "counterbore"
    assert hole.dimensions["diameter_mm"] == pytest.approx(8.0, abs=1e-6)
    assert hole.dimensions["counterbore_diameter_mm"] == pytest.approx(14.0, abs=1e-6)
    assert hole.dimensions["counterbore_depth_mm"] == pytest.approx(6.0, abs=1e-6)
    assert any("annular floor" in line for line in hole.evidence)


def test_a_countersink_is_reported_with_its_included_angle():
    hole = only(analysed("block_with_countersunk_hole"), FeatureType.HOLE)

    assert hole.subtype.endswith("_countersunk")
    assert hole.dimensions["diameter_mm"] == pytest.approx(8.0, abs=1e-6)
    assert hole.dimensions["countersink_angle_deg"] == pytest.approx(90.0, abs=1e-3)


def test_a_boss_is_a_cylinder_and_not_a_hole():
    """Material inside the surface makes it a shaft; outside makes it a bore."""
    result = analysed("plate_with_boss_and_bores")

    cylinder = only(result, FeatureType.CYLINDER)
    assert cylinder.subtype == "shaft"
    assert cylinder.dimensions["diameter_mm"] == pytest.approx(18.0, abs=1e-6)
    assert cylinder.dimensions["length_mm"] == pytest.approx(10.0, abs=1e-6)

    holes = features_of(result, FeatureType.HOLE)
    assert sorted(hole.subtype for hole in holes) == ["blind", "through"]


def test_a_through_hole_reports_the_full_depth_it_traverses():
    """The bore runs through the plate and the boss above it."""
    result = analysed("plate_with_boss_and_bores")
    through = next(h for h in features_of(result, FeatureType.HOLE) if h.subtype == "through")

    assert through.dimensions["depth_mm"] == pytest.approx(30.0, abs=1e-6)


# --- pockets and slots ------------------------------------------------------


def test_a_pocket_is_found_with_its_floor_size_and_depth():
    pocket = only(analysed("block_with_pocket"), FeatureType.POCKET)

    assert pocket.dimensions["length_mm"] == pytest.approx(60.0, abs=1e-6)
    assert pocket.dimensions["width_mm"] == pytest.approx(30.0, abs=1e-6)
    assert pocket.dimensions["depth_mm"] == pytest.approx(8.0, abs=1e-6)
    assert pocket.dimensions["floor_area_mm2"] == pytest.approx(1800.0, abs=1e-3)
    assert pocket.confidence is Confidence.HIGH
    # Floor plus four walls.
    assert len(pocket.source_faces) == 5


def test_a_long_narrow_recess_is_named_a_slot():
    result = analysed("block_with_slot")
    slot = only(result, FeatureType.SLOT)

    assert features_of(result, FeatureType.POCKET) == []
    assert slot.dimensions["length_mm"] == pytest.approx(60.0, abs=1e-6)
    assert slot.dimensions["width_mm"] == pytest.approx(12.0, abs=1e-6)
    assert slot.dimensions["depth_mm"] == pytest.approx(6.0, abs=1e-6)
    assert any("aspect ratio" in line for line in slot.evidence)


def test_pocket_dimensions_are_measured_in_the_faces_own_plane():
    """A rotated pocket is still 60 x 30, not its axis-aligned bounding box."""
    upright = only(analysed("block_with_pocket"), FeatureType.POCKET)
    rotated = only(analysed("rotated_block_with_pocket"), FeatureType.POCKET)

    assert rotated.dimensions["length_mm"] == pytest.approx(upright.dimensions["length_mm"], abs=1e-6)
    assert rotated.dimensions["width_mm"] == pytest.approx(upright.dimensions["width_mm"], abs=1e-6)
    assert rotated.dimensions["depth_mm"] == pytest.approx(upright.dimensions["depth_mm"], abs=1e-6)


# --- edge treatments --------------------------------------------------------


def test_a_fillet_is_found_from_its_tangent_neighbours():
    fillet = only(analysed("block_with_fillet"), FeatureType.FILLET)

    assert fillet.subtype == "convex"
    assert fillet.dimensions["radius_mm"] == pytest.approx(3.0, abs=1e-6)
    assert fillet.dimensions["length_mm"] == pytest.approx(20.0, abs=1e-6)
    assert any("tangent" in line for line in fillet.evidence)


def test_a_fillet_is_not_confused_with_a_bore():
    """A full turn is a bore; an arc that blends tangentially is a fillet."""
    assert features_of(analysed("block_with_fillet"), FeatureType.HOLE) == []
    assert features_of(analysed("block_with_through_hole"), FeatureType.FILLET) == []


def test_a_chamfer_is_found_with_its_angle_but_never_above_medium_confidence():
    chamfer = only(analysed("block_with_chamfer"), FeatureType.CHAMFER)

    assert chamfer.dimensions["angle_deg"] == pytest.approx(45.0, abs=1e-3)
    # A 2.5 mm leg on a right-angled edge gives a face 2.5 * sqrt(2) wide.
    assert chamfer.dimensions["width_mm"] == pytest.approx(3.5355339, abs=1e-4)
    assert chamfer.confidence is Confidence.MEDIUM
    assert any("medium confidence" in line for line in chamfer.evidence)


# --- multiple solids --------------------------------------------------------


def test_multiple_solids_stay_separable():
    result = analysed("two_solids")

    assert result.solid_count == 2
    assert [solid.solid_index for solid in result.solids] == [0, 1]
    # The plain block has no hole; the drilled one does.
    assert result.solids[0].feature_counts.get("hole", 0) == 0
    assert result.solids[1].feature_counts.get("hole", 0) == 1


def test_every_feature_is_attributed_to_a_real_solid():
    result = analysed("two_solids")

    for feature in result.features:
        assert 0 <= feature.solid_index < result.solid_count


def test_each_solid_reports_its_own_volume():
    result = analysed("two_solids")

    assert result.solids[0].volume_mm3 == pytest.approx(8000.0, abs=1e-3)


# --- determinism ------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_recognizing_the_same_file_twice_gives_the_same_result(name):
    """The mandatory determinism guarantee, over every fixture.

    `elapsed_ms` is the one field that legitimately varies, and is excluded.
    """
    data = step(name)

    first = recognize_features_from_bytes(data, f"{name}.step").model_dump(exclude={"elapsed_ms"})
    second = recognize_features_from_bytes(data, f"{name}.step").model_dump(exclude={"elapsed_ms"})

    assert first == second


def test_feature_ids_are_stable_across_runs():
    first = recognize_features_from_bytes(step("plate_with_four_holes"), "p.step")
    second = recognize_features_from_bytes(step("plate_with_four_holes"), "p.step")

    assert [f.feature_id for f in first.features] == [f.feature_id for f in second.features]
    assert [f.feature_id for f in first.features][:4] == ["H-001", "H-002", "H-003", "H-004"]


# --- traceability and explainability ----------------------------------------


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_every_feature_traces_back_to_real_cad_faces(name):
    result = analysed(name)

    for feature in result.features:
        assert feature.source_faces, f"{feature.feature_id} has no source faces"
        assert all(1 <= index <= result.face_count for index in feature.source_faces)


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_no_face_is_claimed_by_two_features(name):
    """A face belongs to at most one feature, so the counts describe the model once."""
    result = analysed(name)

    seen: set[int] = set()
    for feature in result.features:
        overlap = seen & set(feature.source_faces)
        assert not overlap, f"{feature.feature_id} re-claims face(s) {overlap}"
        seen.update(feature.source_faces)


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_no_unexplained_feature_claims(name):
    result = analysed(name)

    for feature in result.features:
        assert feature.detection_method
        assert feature.evidence, f"{feature.feature_id} claims a feature with no evidence"
        assert feature.confidence in set(Confidence)


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_only_the_controlled_vocabulary_is_used(name):
    result = analysed(name)

    for feature in result.features:
        assert feature.feature_type in set(FeatureType)


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_no_manufacturing_operation_is_inferred(name):
    """Features are geometry. Turning them into operations is a later step."""
    result = analysed(name)

    forbidden = ("drill", "mill", "turning_operation", "machining", "cost", "rate")
    blob = result.model_dump_json().lower()
    for word in forbidden:
        assert word not in blob, f"{name} mentions '{word}'"


def test_source_edges_are_reported_for_traceability():
    hole = only(analysed("block_with_through_hole"), FeatureType.HOLE)

    assert hole.source_edges
    assert all(index >= 1 for index in hole.source_edges)


# --- recognizers are independently testable ---------------------------------


def test_the_hole_recognizer_can_run_on_its_own():
    context = RecognitionContext(topology=topology_of("block_with_blind_hole"))

    holes = HoleRecognizer().recognize(context)

    assert len(holes) == 1
    assert holes[0].subtype == "blind"


def test_the_pocket_recognizer_can_run_on_its_own():
    context = RecognitionContext(topology=topology_of("block_with_pocket"))

    pockets = PocketRecognizer().recognize(context)

    assert len(pockets) == 1
    assert pockets[0].dimensions["depth_mm"] == pytest.approx(8.0, abs=1e-6)


@pytest.mark.parametrize(
    "recognizer",
    [HoleRecognizer(), PocketRecognizer(), FilletRecognizer(), ChamferRecognizer(), CylinderRecognizer()],
)
def test_no_recognizer_finds_anything_in_a_plain_block(recognizer):
    """Only the planar recognizer should have anything to say about a box."""
    context = RecognitionContext(topology=topology_of("plain_block"))

    assert recognizer.recognize(context) == []


def test_the_planar_recognizer_finds_every_face_of_a_plain_block():
    context = RecognitionContext(topology=topology_of("plain_block"))

    planes = PlanarFaceRecognizer().recognize(context)

    assert len(planes) == 6


def test_a_recognizer_that_fails_does_not_sink_the_others():
    class Exploding:
        name = "exploding"

        def recognize(self, context):
            raise RuntimeError("boom")

    shape = read_step_shape(step("block_with_through_hole"))
    result = recognize_features(shape, recognizers=(Exploding(), HoleRecognizer()))

    assert result.feature_counts.get("hole") == 1
    assert any("exploding recognizer failed" in warning for warning in result.warnings)
    assert "exploding" not in result.recognizers


# --- robustness -------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, data, expected",
    [
        ("part.step", b"this is not STEP data", "not valid"),
        ("part.stp", b"", "empty"),
        ("part.iges", b"whatever", "not valid IGES data"),
        ("noextension", b"whatever", "Unsupported CAD file format"),
    ],
)
def test_bad_input_fails_with_a_readable_message(filename, data, expected):
    with pytest.raises(ValueError, match=expected):
        recognize_features_from_bytes(data, filename)


def test_stl_is_refused_with_an_explanation_rather_than_guessed_at():
    mesh = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")

    with pytest.raises(ValueError) as caught:
        recognize_features_from_bytes(mesh, "cube.stl")

    message = str(caught.value)
    assert "triangle mesh" in message
    assert "STEP" in message
    # And it says what still works, so the user is not left thinking STL broke.
    assert "costing still work" in message


def test_a_step_file_with_no_brep_faces_says_so_instead_of_reporting_nothing():
    """A faceted STEP export reads fine but transfers no faces.

    Found on a real 198 MB Creo `FACETED_BREP` export, which produced an empty
    feature list that read as "this part has no features" rather than "there
    was no B-Rep here to look at".
    """
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    builder = BRep_Builder()
    empty = TopoDS_Compound()
    builder.MakeCompound(empty)

    with pytest.raises(ValueError) as caught:
        recognize_features(empty, filename="faceted.step")

    message = str(caught.value)
    assert "no B-Rep faces" in message
    assert "faceted" in message.lower()
    # And it says what to do about it.
    assert "Re-export" in message


def test_the_face_budget_is_sane():
    assert 100 < MAX_FACES <= 100_000


def test_a_model_over_the_face_budget_is_refused_cleanly(monkeypatch):
    import cad_costing_agent.features as features_module

    monkeypatch.setattr(features_module, "MAX_FACES", 2)

    with pytest.raises(ValueError, match="above the"):
        recognize_features_from_bytes(step("plain_block"), "plain_block.step")


# --- the endpoint -----------------------------------------------------------


def post_features(filename: str, data: bytes):
    return client.post(
        "/cad/features", files={"file": (filename, data, "application/octet-stream")}
    )


def test_the_endpoint_returns_recognized_features():
    response = post_features("block.step", step("block_with_through_hole"))

    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "block.step"
    assert body["source_format"] == "step"
    assert body["units"] == "mm"
    assert body["feature_counts"]["hole"] == 1
    assert body["features"][0]["feature_type"] == "hole"
    assert body["features"][0]["subtype"] == "through"


def test_the_endpoint_reports_how_long_recognition_took():
    body = post_features("block.step", step("plain_block")).json()

    assert body["elapsed_ms"] > 0


@pytest.mark.parametrize(
    "filename, data",
    [
        ("part.step", b"not step at all"),
        ("part.stl", b"anything"),
        ("part.iges", b"anything"),
    ],
)
def test_the_endpoint_rejects_what_it_cannot_analyse(filename, data):
    response = post_features(filename, data)

    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)


def test_the_endpoint_refuses_an_oversized_upload(monkeypatch):
    import cad_costing_agent.main as main_module

    monkeypatch.setattr(main_module, "MAX_UPLOAD_BYTES", 100)

    response = post_features("block.step", step("plain_block"))

    assert response.status_code == 400
    assert "MB limit" in response.json()["detail"]


def test_analysing_a_model_persists_nothing():
    """Feature recognition is not a costing event."""
    before = client.get("/estimates").json()["total"]

    post_features("block.step", step("block_with_through_hole"))

    assert client.get("/estimates").json()["total"] == before


# --- nothing about geometry or costing changed ------------------------------


def test_geometry_extraction_still_works_for_step():
    response = client.post(
        "/cad/geometry", files={"file": ("block.step", step("plain_block"))}
    )

    assert response.status_code == 200
    assert response.json()["volume"] == pytest.approx(60.0 * 40.0 * 20.0, rel=1e-9)


def test_the_viewer_mesh_still_works_for_step():
    response = client.post("/cad/mesh", files={"file": ("block.step", step("plain_block"))})

    assert response.status_code == 200
    assert response.json()["mesh"]["triangle_count"] == 12


def test_stl_remains_fully_supported_everywhere_except_features():
    mesh = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")

    assert client.post("/cad/geometry", files={"file": ("c.stl", mesh)}).status_code == 200
    assert client.post("/cad/mesh", files={"file": ("c.stl", mesh)}).status_code == 200
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


def test_analysing_features_does_not_change_the_cost_of_the_same_part():
    data = step("block_with_through_hole")
    form = {
        "material": "Steel",
        "source_unit": "mm",
        "part_id": "P-1",
        "part_name": "Block",
        "quantity": "10",
        "rate_set_id": "poc-machine-shop",
    }

    def cost():
        return client.post(
            "/cost/cad-part",
            files={"file": ("block.step", data, "application/octet-stream")},
            data=form,
        ).json()["part_cost"]["total_part_cost"]

    before = cost()
    post_features("block.step", data)
    assert cost() == before


# --- the UI panel -----------------------------------------------------------


def test_the_features_script_is_served_and_referenced():
    html = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    assert (STATIC_PATH / "features.js").is_file()
    assert '<script src="features.js"></script>' in html
    assert client.get("/ui/features.js").status_code == 200


def test_the_page_has_a_features_panel():
    html = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    for element in ("features-card", "analyse-features", "features-body", "features-summary"):
        assert f'id="{element}"' in html


def test_the_features_panel_does_no_geometry_or_costing_of_its_own():
    features_js = (STATIC_PATH / "features.js").read_text(encoding="utf-8")

    for forbidden in ["Math.PI", "Math.sqrt", "/cost/", "total_part_cost", "rate_set"]:
        assert forbidden not in features_js
    assert '"/cad/features"' in features_js


def test_the_features_panel_shows_the_evidence_and_the_source_faces():
    features_js = (STATIC_PATH / "features.js").read_text(encoding="utf-8")

    assert "detection_method" in features_js
    assert "evidence" in features_js
    assert "source_faces" in features_js
