"""Volume and mass from faceted STEP polygons.

A faceted export has no exact surfaces, but it can still have a perfectly good
volume — and volume is all that mass and costing need. These cover the path
that makes such a file costable, and, just as importantly, the cases where it
must refuse: a volume derived from geometry that does not hold water is worse
than no volume at all.

Feature recognition is deliberately not involved. A faceted model may yield
limited features and a completely valid mass; the two are independent.
"""

from functools import lru_cache

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient

from cad_costing_agent import limits
from cad_costing_agent.cad_faceted import (
    analyse_faceted_volume,
    detect_length_unit,
    parse_faceted_step,
)
from cad_costing_agent.cad_geometry import extract_geometry_from_bytes
from cad_costing_agent.main import app
from tests.faceted_fixtures import (
    faceted_block_with_blind_hole,
    faceted_plain_block,
    faceted_plate_with_four_holes,
    faceted_step_from_mesh,
    faceted_tube,
)
from tests.step_fixtures import ALL_FIXTURES

client = TestClient(app)

# A unit cube, wound outwards, as vertices and triangles.
CUBE_VERTICES = np.array(
    [
        [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0], [1.0, 0.0, 1.0], [1.0, 1.0, 1.0], [0.0, 1.0, 1.0],
    ]
)
CUBE_TRIANGLES = np.array(
    [
        [0, 2, 1], [0, 3, 2],  # bottom
        [4, 5, 6], [4, 6, 7],  # top
        [0, 1, 5], [0, 5, 4],
        [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6],
        [3, 0, 4], [3, 4, 7],
    ]
)


def cube(scale: float = 1.0, offset=(0.0, 0.0, 0.0), **kwargs) -> bytes:
    vertices = CUBE_VERTICES * scale + np.asarray(offset)
    return faceted_step_from_mesh(vertices, CUBE_TRIANGLES, **kwargs)


@lru_cache(maxsize=None)
def geometry_of(name: str):
    builders = {
        "plain_block": faceted_plain_block,
        "plate_four_holes": faceted_plate_with_four_holes,
        "blind_hole": faceted_block_with_blind_hole,
    }
    return extract_geometry_from_bytes(builders[name](), ".step")


# --- a closed faceted mesh has a real volume --------------------------------


def test_a_closed_faceted_block_gives_its_exact_volume():
    geometry = geometry_of("plain_block")

    assert geometry.volume == pytest.approx(60.0 * 40.0 * 20.0, rel=1e-9)
    assert geometry.surface_area == pytest.approx(2 * (60 * 40 + 60 * 20 + 40 * 20), rel=1e-9)
    assert geometry.bounding_box.x == pytest.approx(60.0, rel=1e-9)


def test_the_result_says_the_volume_came_from_a_mesh():
    geometry = geometry_of("plain_block")

    assert geometry.geometry_source == "mesh"
    assert geometry.volume_method == "mesh_signed_volume"
    assert geometry.volume_confidence == "high"
    assert geometry.facet_count == 12
    assert geometry.closed_component_count == 1
    assert geometry.open_component_count == 0
    assert "faceted STEP export with no exact surfaces" in geometry.units_note


def test_an_analytic_step_still_says_it_came_from_exact_surfaces():
    geometry = extract_geometry_from_bytes(ALL_FIXTURES["plain_block"](), ".step")

    assert geometry.geometry_source == "brep"
    assert geometry.volume_method == "brep_mass_properties"
    assert geometry.facet_count == 0


def test_a_tessellated_hole_removes_slightly_less_than_the_true_cylinder():
    """The polygons are inscribed in the real bore, and that is reported honestly."""
    faceted = geometry_of("plate_four_holes")
    analytic = extract_geometry_from_bytes(ALL_FIXTURES["plate_with_four_holes"](), ".step")

    assert faceted.volume > analytic.volume
    assert faceted.volume == pytest.approx(analytic.volume, rel=1e-3)


def test_a_blind_hole_model_is_measured_from_its_polygons():
    geometry = geometry_of("blind_hole")
    expected = 60.0 * 40.0 * 20.0 - np.pi * 8.0**2 * 12.0

    assert geometry.volume == pytest.approx(expected, rel=2e-3)


# --- units ------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefix, name, millimetres",
    [("$", "metre", 1000.0), (".MILLI.", "millimetre", 1.0), (".CENTI.", "centimetre", 10.0)],
)
def test_the_declared_length_unit_is_read(prefix, name, millimetres):
    unit = detect_length_unit(cube(unit=prefix))

    assert unit is not None
    assert unit.name == name
    assert unit.millimetres_per_unit == millimetres


def test_a_metre_based_file_is_converted_to_millimetres():
    """Getting this wrong scales every mass by a thousand."""
    geometry = extract_geometry_from_bytes(cube(scale=0.1, unit="$"), ".step")

    assert geometry.volume == pytest.approx(100.0**3, rel=1e-9)
    assert geometry.bounding_box.x == pytest.approx(100.0, rel=1e-9)
    assert geometry.detected_unit == "mm"


def test_a_file_with_no_declared_unit_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="does not declare a length unit"):
        extract_geometry_from_bytes(cube(declare_unit=False), ".step")


# --- welding ----------------------------------------------------------------


def test_vertices_are_welded_by_position():
    """Real faceted exporters write a separate point per facet corner.

    Built here the same way — every triangle carrying its own three corners —
    because welding them back together is what makes anything adjacent to
    anything, and without it no closure could ever be detected.
    """
    unwelded_vertices = CUBE_VERTICES[CUBE_TRIANGLES].reshape(-1, 3)
    unwelded_triangles = np.arange(len(unwelded_vertices)).reshape(-1, 3)
    data = faceted_step_from_mesh(unwelded_vertices, unwelded_triangles)

    model = parse_faceted_step(data)

    assert len(unwelded_vertices) == 36  # every corner written separately
    assert model.shells[0].vertex_count == 8  # welded back to a cube's corners
    assert analyse_faceted_volume(data).components[0].boundary_edge_count == 0


def test_without_welding_nothing_would_be_adjacent():
    topology = analyse_faceted_volume(cube())

    assert topology.vertex_count == 8
    assert topology.triangle_count == 12
    assert topology.components[0].boundary_edge_count == 0


# --- closure decides whether a volume is reported ---------------------------


def test_an_open_mesh_reports_that_it_needs_input_rather_than_a_volume():
    """No volume is invented, and none is silently taken from the closed part."""
    geometry = extract_geometry_from_bytes(faceted_tube(sides=32), ".step")

    assert geometry.geometry_status == "incomplete_requires_input"
    assert geometry.volume == 0.0
    assert geometry.volume_confidence == "unavailable"
    assert geometry.incomplete is not None
    assert geometry.incomplete.open_component_count == 1
    assert "boundary edges" in geometry.incomplete.explanation
    assert "total_volume" in geometry.incomplete.resolution_methods
    assert "missing_volume" in geometry.incomplete.resolution_methods


def test_an_open_shell_declaration_does_not_by_itself_prevent_a_volume():
    """The STEP entity name and the polygon topology are separate things."""
    from cad_costing_agent.features.step_text import classify_step_bytes

    data = cube(open_shell=True)
    assert classify_step_bytes(data).faceted_brep_on_open_shell is True

    geometry = extract_geometry_from_bytes(data, ".step")
    assert geometry.volume == pytest.approx(1.0, rel=1e-9)


def test_a_flipped_triangle_is_caught_even_though_the_mesh_stays_watertight():
    """Undirected edge counts cannot see it; directed ones can."""
    flipped = CUBE_TRIANGLES.copy()
    flipped[0] = flipped[0][::-1]

    topology = analyse_faceted_volume(faceted_step_from_mesh(CUBE_VERTICES, flipped))

    assert topology.components[0].boundary_edge_count == 0
    assert topology.components[0].winding_consistent is False
    assert topology.components[0].is_closed is False
    assert not topology.is_watertight


def test_an_inconsistently_wound_model_yields_no_volume():
    flipped = CUBE_TRIANGLES.copy()
    flipped[0] = flipped[0][::-1]

    geometry = extract_geometry_from_bytes(faceted_step_from_mesh(CUBE_VERTICES, flipped), ".step")

    assert geometry.geometry_status == "incomplete_requires_input"
    assert geometry.volume == 0.0


def test_inward_winding_still_gives_a_positive_volume():
    """A solid modelled inside-out has the right size, just a negative sign."""
    inside_out = CUBE_TRIANGLES[:, ::-1].copy()

    topology = analyse_faceted_volume(faceted_step_from_mesh(CUBE_VERTICES * 3.0, inside_out))

    assert topology.is_watertight
    assert topology.components[0].signed_volume < 0
    assert topology.volume == pytest.approx(27.0, rel=1e-9)


# --- several bodies ---------------------------------------------------------


def test_two_separate_closed_bodies_are_both_counted():
    vertices = np.vstack([CUBE_VERTICES, CUBE_VERTICES * 2.0 + np.array([10.0, 0.0, 0.0])])
    triangles = np.vstack([CUBE_TRIANGLES, CUBE_TRIANGLES + 8])

    geometry = extract_geometry_from_bytes(faceted_step_from_mesh(vertices, triangles), ".step")

    assert geometry.closed_component_count == 2
    assert geometry.open_component_count == 0
    assert geometry.volume == pytest.approx(1.0 + 8.0, rel=1e-9)


def test_one_open_body_among_closed_ones_withholds_the_whole_volume():
    """Partial volume would silently price a fraction of the assembly."""
    open_sheet = np.array([[0.0, 0.0, 5.0], [1.0, 0.0, 5.0], [1.0, 1.0, 5.0]])
    vertices = np.vstack([CUBE_VERTICES, open_sheet])
    triangles = np.vstack([CUBE_TRIANGLES, np.array([[8, 9, 10]])])

    geometry = extract_geometry_from_bytes(faceted_step_from_mesh(vertices, triangles), ".step")

    assert geometry.geometry_status == "incomplete_requires_input"
    assert geometry.volume == 0.0
    # The measured part is offered as a starting point, never as the answer.
    assert geometry.incomplete.known_volume_mm3 == pytest.approx(1.0, rel=1e-9)
    assert geometry.incomplete.closed_component_count == 1
    assert geometry.incomplete.open_component_count == 1


def test_the_closed_subset_is_still_reported_in_the_refusal():
    """The counts are the useful part of saying no."""
    open_sheet = np.array([[0.0, 0.0, 5.0], [1.0, 0.0, 5.0], [1.0, 1.0, 5.0]])
    vertices = np.vstack([CUBE_VERTICES, open_sheet])
    triangles = np.vstack([CUBE_TRIANGLES, np.array([[8, 9, 10]])])
    topology = analyse_faceted_volume(faceted_step_from_mesh(vertices, triangles))

    assert len(topology.closed) == 1
    assert len(topology.open) == 1
    assert topology.volume == pytest.approx(1.0, rel=1e-9)
    assert topology.is_watertight is False


def test_a_file_with_no_polygons_has_no_geometry():
    empty = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"

    with pytest.raises(ValueError):
        extract_geometry_from_bytes(empty, ".step")


# --- determinism ------------------------------------------------------------


@pytest.mark.parametrize("build", [faceted_plain_block, faceted_plate_with_four_holes])
def test_the_same_file_always_gives_the_same_volume(build):
    data = build()

    first = extract_geometry_from_bytes(data, ".step")
    second = extract_geometry_from_bytes(data, ".step")

    assert first.model_dump() == second.model_dump()


# --- mass and costing -------------------------------------------------------


def test_faceted_geometry_produces_a_mass():
    response = client.post(
        "/cad/mass",
        files={"file": ("block.step", faceted_plain_block(), "application/octet-stream")},
        data={"material": "Steel", "source_unit": "mm"},
    )

    assert response.status_code == 200
    body = response.json()
    # 48,000 mm^3 = 4.8e-5 m^3 at 7850 kg/m^3.
    assert body["mass_kg"] == pytest.approx(4.8e-5 * 7850.0, rel=1e-6)


def test_a_faceted_model_can_be_costed():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("block.step", faceted_plain_block(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "FacetedBlock",
            "quantity": "10",
            "rate_set_id": "poc-machine-shop",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["geometry"]["geometry_source"] == "mesh"
    assert body["part_cost"]["total_part_cost"] > 0
    assert body["part_cost"]["currency"] == "INR"


def test_a_faceted_models_mass_uses_the_rate_sets_material_density():
    """The Step 17 rule must survive the new geometry path."""
    profile = {
        "rate_set_id": "faceted-shop",
        "name": "Faceted Shop",
        "version": "1.0.0",
        "currency": "INR",
        "is_illustrative": False,
        # The built-in catalog says steel is 7850; this profile says 8000.
        "materials": {"steel": {"name": "Steel", "density_kg_m3": 8000.0, "cost_per_kg": 50.0}},
        "operations": {"milling": {"machine_rate_per_hour": 1000.0, "labor_rate_per_hour": 400.0}},
        "additional_costs": {},
    }
    assert client.post("/rates", json=profile).status_code == 201

    body = client.post(
        "/cost/cad-part",
        files={"file": ("block.step", faceted_plain_block(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "FacetedBlock",
            "quantity": "1",
            "rate_set_id": "faceted-shop",
        },
    ).json()

    assert body["mass"]["material"]["density_kg_m3"] == 8000.0
    assert body["mass"]["mass_kg"] == pytest.approx(4.8e-5 * 8000.0, rel=1e-6)


def test_a_faceted_model_that_cannot_be_measured_asks_for_input():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("tube.step", faceted_tube(sides=32), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Tube",
            "quantity": "1",
            "rate_set_id": "poc-machine-shop",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "needs_geometry_input"
    assert detail["geometry"]["open_component_count"] == 1
    assert "total_volume" in detail["geometry"]["resolution_methods"]


# --- nothing else changed ---------------------------------------------------


def test_analytic_step_geometry_is_unchanged():
    geometry = extract_geometry_from_bytes(ALL_FIXTURES["block_with_through_hole"](), ".step")

    expected = 60.0 * 40.0 * 20.0 - np.pi * 5.0**2 * 20.0
    assert geometry.volume == pytest.approx(expected, rel=1e-9)
    assert geometry.detected_unit == "mm"


def test_stl_geometry_is_unchanged():
    mesh = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")

    geometry = extract_geometry_from_bytes(mesh, ".stl")

    assert geometry.volume == pytest.approx(6000.0, rel=1e-9)
    assert geometry.detected_unit is None
    assert geometry.geometry_source == "mesh"


def test_analytic_costing_is_unchanged():
    response = client.post(
        "/cost/cad-part",
        files={
            "file": (
                "block.step",
                ALL_FIXTURES["block_with_through_hole"](),
                "application/octet-stream",
            )
        },
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Block",
            "quantity": "10",
            "rate_set_id": "poc-machine-shop",
        },
    )

    assert response.status_code == 200
    assert response.json()["geometry"]["geometry_source"] == "brep"


# --- the viewer stays independent -------------------------------------------


def test_a_faceted_model_is_costable_even_when_it_cannot_be_previewed():
    """OpenCASCADE cannot tessellate these files at all; costing does not care."""
    data = cube(scale=20.0, open_shell=True)

    preview = client.post("/cad/mesh", files={"file": ("b.step", data)})
    costed = client.post(
        "/cost/cad-part",
        files={"file": ("b.step", data, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Block",
            "quantity": "1",
            "rate_set_id": "poc-machine-shop",
        },
    )

    # The preview may fail or come back empty; the cost must not depend on it.
    assert preview.status_code in (200, 400)
    assert costed.status_code == 200
    assert costed.json()["part_cost"]["total_part_cost"] > 0


# --- budgets ----------------------------------------------------------------


def test_the_geometry_budget_is_far_larger_than_the_feature_budget():
    """Deriving a volume is cheaper than reconstructing features."""
    assert limits.MAX_GEOMETRY_FACETS > limits.MAX_FACETS


def test_a_model_over_the_geometry_budget_is_refused(monkeypatch):
    monkeypatch.setattr(limits, "MAX_GEOMETRY_FACETS", 4)

    with pytest.raises(ValueError, match="above the"):
        extract_geometry_from_bytes(faceted_plate_with_four_holes(), ".step")


def test_a_faceted_model_can_also_be_previewed():
    """The polygons are already a mesh; no tessellation is needed.

    Without this the viewer answered "no transferable geometry" for a file
    whose geometry, mass and cost all worked.
    """
    from cad_costing_agent.cad_mesh import build_viewer_model

    model = build_viewer_model(faceted_plain_block(), "block.step")

    assert model.mesh is not None
    assert model.mesh.triangle_count == 12
    assert model.mesh.tessellation.method == "none"
    assert "faceted STEP export" in model.mesh.tessellation.note
    assert model.geometry.geometry_source == "mesh"


def test_a_faceted_model_over_the_viewer_budget_is_simplified_not_dropped(monkeypatch):
    """Being too dense to send costs preview detail, not the preview itself."""
    import cad_costing_agent.cad_mesh as cad_mesh
    from cad_costing_agent.cad_mesh import build_viewer_model

    # Built before the limit is lowered: the fixture is itself produced by
    # tessellating an analytic shape, which the patched budget would reduce.
    data = faceted_plain_block()
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 4)

    model = build_viewer_model(data, "block.step")

    assert model.mesh is not None
    assert model.mesh.display_mesh_simplified is True
    assert model.mesh.original_triangle_count == 12
    assert model.mesh.triangle_count <= 4
    # And the display mesh has not touched the measured geometry.
    assert model.geometry.volume == pytest.approx(48000.0, rel=1e-9)
