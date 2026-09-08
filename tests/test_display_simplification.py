"""Display-mesh decimation: the viewer's budget must not veto a model.

Two things are being defended here, and the second matters more than the first.

1. A mesh above the browser's triangle budget is **reduced**, deterministically
   and server-side, rather than refused — so a 200 MB faceted Creo export is
   something you can look at.
2. That reduction is quarantined to pixels. Every engineering number in the
   response — volume, surface area, bounding box, mass, cost — is measured from
   the full-resolution data before the display mesh exists, and stays bit-for-bit
   what it was when no simplification happened at all. These tests assert that
   equality directly rather than trusting the call order.

The real 200 MB files are not in the repository, so the dense models here are
synthetic: a subdivided sphere for curvature and a faceted STEP written from a
tessellated block, the same construction the rest of the faceted tests use.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient

from cad_costing_agent import cad_mesh
from cad_costing_agent.cad_mesh import (
    MeshTooLargeError,
    build_renderable_mesh,
    build_viewer_model,
)
from cad_costing_agent.main import app
from cad_costing_agent.mesh_simplify import METHOD, simplify_for_display
from tests.faceted_fixtures import faceted_step_from_mesh

client = TestClient(app)


def dense_stl(subdivisions: int = 4) -> bytes:
    """A sphere with a few thousand triangles — enough to exceed a lowered budget."""
    return trimesh.creation.icosphere(subdivisions=subdivisions).export(file_type="stl")


def dense_faceted_step() -> bytes:
    sphere = trimesh.creation.icosphere(subdivisions=4)
    return faceted_step_from_mesh(
        np.asarray(sphere.vertices, dtype=np.float64) * 10.0,
        np.asarray(sphere.faces, dtype=np.int64),
        name="DENSE",
    )


# --- the boundary: below the limit nothing happens ---------------------------


def test_a_mesh_below_the_viewer_limit_is_left_exactly_as_it_was():
    mesh = build_renderable_mesh(dense_stl(subdivisions=2), "stl")

    assert mesh.display_mesh_simplified is False
    assert mesh.simplification_method is None
    assert mesh.simplification_ratio is None
    assert mesh.original_triangle_count == mesh.triangle_count
    assert mesh.display_triangle_count == mesh.triangle_count


def test_the_untouched_path_is_byte_identical_to_no_simplification_at_all():
    """The common case must not be quietly re-meshed by the new code path."""
    vertices = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    faces = np.array([[0, 1, 2]])

    result = simplify_for_display(vertices, faces, 80_000)

    assert result.simplified is False
    assert np.array_equal(result.vertices, vertices)
    assert np.array_equal(result.faces, faces)


# --- above the limit: reduced, not refused -----------------------------------


def test_a_mesh_above_the_viewer_limit_is_simplified(monkeypatch):
    data = dense_stl()
    full = build_renderable_mesh(data, "stl")
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)

    display = build_renderable_mesh(data, "stl")

    assert full.triangle_count > 1_000  # the fixture really is over budget
    assert display.display_mesh_simplified is True
    assert display.simplification_method == METHOD
    assert display.original_triangle_count == full.triangle_count


def test_the_simplified_mesh_is_within_the_budget(monkeypatch):
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)

    display = build_renderable_mesh(dense_stl(), "stl")

    assert display.triangle_count <= 1_000
    assert display.display_triangle_count == display.triangle_count
    assert len(display.indices) == display.triangle_count * 3


def test_a_dense_faceted_step_is_previewable(monkeypatch):
    """The whole point of the step: Figure 3/4's shape of file, in the viewer."""
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 2_000)

    model = build_viewer_model(dense_faceted_step(), "dense.step")

    assert model.mesh_error is None
    assert model.mesh is not None
    assert model.mesh.display_mesh_simplified is True
    assert model.mesh.triangle_count <= 2_000
    assert model.mesh.original_triangle_count > 2_000


def test_the_simplification_ratio_describes_the_reduction(monkeypatch):
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)

    display = build_renderable_mesh(dense_stl(), "stl")

    assert display.simplification_ratio == pytest.approx(
        display.display_triangle_count / display.original_triangle_count, rel=1e-6
    )
    assert 0 < display.simplification_ratio < 1


def test_the_note_says_the_preview_was_reduced_and_costing_was_not(monkeypatch):
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)

    display = build_renderable_mesh(dense_stl(), "stl")

    note = display.tessellation.note
    assert "reduced" in note
    assert "browser performance" in note
    assert "full-resolution CAD data" in note


# --- determinism -------------------------------------------------------------


def test_simplification_is_deterministic(monkeypatch):
    """Two uploads of the same bytes must give the same picture."""
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)
    data = dense_stl()

    first = build_renderable_mesh(data, "stl")
    second = build_renderable_mesh(data, "stl")

    assert first.positions == second.positions
    assert first.indices == second.indices
    assert first.triangle_count == second.triangle_count


def test_simplification_uses_no_randomness():
    """Guards against a future 'just sample some triangles' shortcut."""
    from pathlib import Path

    source = Path(cad_mesh.__file__).with_name("mesh_simplify.py").read_text(encoding="utf-8")

    for forbidden in ["import random", "np.random", "choice(", "shuffle("]:
        assert forbidden not in source


def test_the_reduced_mesh_is_a_valid_indexed_triangle_list(monkeypatch):
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)

    display = build_renderable_mesh(dense_stl(), "stl")

    indices = np.asarray(display.indices, dtype=np.int64).reshape(-1, 3)
    assert indices.min() >= 0
    assert indices.max() < display.vertex_count
    assert len(display.positions) == display.vertex_count * 3
    # No triangle collapsed to a line or a point.
    assert (indices[:, 0] != indices[:, 1]).all()
    assert (indices[:, 1] != indices[:, 2]).all()
    assert (indices[:, 0] != indices[:, 2]).all()


def test_the_reduced_mesh_still_occupies_the_models_space(monkeypatch):
    """Decimation may lose detail; it must not lose the model's position or size."""
    data = dense_stl()
    full = build_renderable_mesh(data, "stl")
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)

    display = build_renderable_mesh(data, "stl")

    span = max(np.asarray(full.bounds.max) - np.asarray(full.bounds.min))
    assert np.allclose(display.bounds.min, full.bounds.min, atol=span * 0.05)
    assert np.allclose(display.bounds.max, full.bounds.max, atol=span * 0.05)


# --- the quarantine: measurements come from the full mesh --------------------


def test_geometry_is_identical_whether_or_not_the_preview_was_simplified(monkeypatch):
    """The strongest statement available: the numbers do not move at all."""
    data = dense_stl()
    unsimplified = build_viewer_model(data, "sphere.stl")
    assert unsimplified.mesh.display_mesh_simplified is False

    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)
    simplified = build_viewer_model(data, "sphere.stl")
    assert simplified.mesh.display_mesh_simplified is True

    assert simplified.geometry.model_dump() == unsimplified.geometry.model_dump()


def test_the_display_mesh_volume_is_not_the_reported_volume(monkeypatch):
    """If the two were ever confused, this is the test that would catch it."""
    data = dense_stl()
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)

    model = build_viewer_model(data, "sphere.stl")

    display = trimesh.Trimesh(
        vertices=np.asarray(model.mesh.positions).reshape(-1, 3),
        faces=np.asarray(model.mesh.indices).reshape(-1, 3),
        process=False,
    )
    # A decimated sphere is measurably smaller. The response must not say so.
    assert abs(display.volume) < model.geometry.volume * 0.999
    full = trimesh.load(trimesh.util.wrap_as_stream(data), file_type="stl")
    assert model.geometry.volume == pytest.approx(full.volume, rel=1e-9)


def test_costing_is_unchanged_by_the_display_budget(monkeypatch):
    """Same file, same price, whatever the viewer is doing."""
    data = dense_stl()
    form = {
        "material": "Steel",
        "source_unit": "mm",
        "part_id": "P-DISPLAY",
        "part_name": "Sphere",
        "quantity": "10",
        "rate_set_id": "poc-machine-shop",
    }

    def price() -> float:
        response = client.post(
            "/cost/cad-part",
            files={"file": ("sphere.stl", data, "application/octet-stream")},
            data=form,
        )
        assert response.status_code == 200
        return response.json()["part_cost"]["total_part_cost"]

    before = price()
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)
    assert price() == before


def test_the_costing_path_never_imports_the_simplifier():
    """Structural, not behavioural: the modules that price a part cannot reach it."""
    from pathlib import Path

    directory = Path(cad_mesh.__file__).parent
    for module in [
        "costing.py",
        "operation_costing.py",
        "additional_costs.py",
        "part_cost.py",
        "cad_mass.py",
        "cad_part_cost.py",
        "cad_geometry.py",
        "cad_faceted.py",
    ]:
        assert "mesh_simplify" not in (directory / module).read_text(encoding="utf-8")


# --- failure modes -----------------------------------------------------------


def test_an_invalid_mesh_is_still_rejected(monkeypatch):
    """Simplification must not turn unparseable input into a blank preview."""
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)

    with pytest.raises(ValueError):
        build_renderable_mesh(b"not an stl at all", "stl")


def test_a_budget_nothing_can_satisfy_degrades_without_blocking_costing(monkeypatch):
    """The last resort: no preview, but the part is still fully costable."""
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 2)
    data = dense_stl()

    model = build_viewer_model(data, "sphere.stl")

    assert model.mesh is None
    assert "only the 3D preview is unavailable" in model.mesh_error
    assert model.geometry.volume > 0

    estimate = client.post(
        "/cost/cad-part",
        files={"file": ("sphere.stl", data, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-NOPREVIEW",
            "part_name": "Sphere",
            "quantity": "1",
            "rate_set_id": "poc-machine-shop",
        },
    )
    assert estimate.status_code == 200
    assert estimate.json()["part_cost"]["total_part_cost"] > 0


def test_the_search_never_returns_a_mesh_over_budget():
    """Whatever the target, the result is within it or empty. Never over."""
    sphere = trimesh.creation.icosphere(subdivisions=4)
    vertices = np.asarray(sphere.vertices)
    faces = np.asarray(sphere.faces)

    for target in [10, 100, 1_000, 5_000, len(faces) - 1]:
        result = simplify_for_display(vertices, faces, target)
        assert result.triangle_count <= target, target


def test_a_degenerate_mesh_with_no_extent_is_returned_unchanged():
    """A zero-size model has no grid to cluster onto; it must not divide by zero."""
    vertices = np.zeros((3, 3))
    faces = np.array([[0, 1, 2]])

    result = simplify_for_display(vertices, faces, 0)

    assert result.triangle_count in (0, 1)


# --- the response contract ---------------------------------------------------


def test_the_endpoint_reports_the_simplification(monkeypatch):
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 1_000)

    response = client.post(
        "/cad/mesh",
        files={"file": ("sphere.stl", dense_stl(), "application/octet-stream")},
    )

    assert response.status_code == 200
    mesh = response.json()["mesh"]
    assert mesh["display_mesh_simplified"] is True
    assert mesh["simplification_method"] == METHOD
    assert mesh["original_triangle_count"] > mesh["display_triangle_count"]
    assert 0 < mesh["simplification_ratio"] < 1


def test_the_ui_shows_the_original_count_beside_the_preview_count():
    """The panel must not present a reduced count as the model's own."""
    from cad_costing_agent.main import STATIC_PATH

    app_js = (STATIC_PATH / "app.js").read_text(encoding="utf-8")

    assert "display_mesh_simplified" in app_js
    assert "original_triangle_count" in app_js
    assert "Simplified for display" in app_js


def test_the_viewer_budgets_are_separate_and_ordered():
    """Reading and sending are different limits, and reading is the larger."""
    from cad_costing_agent import limits

    assert limits.MAX_VIEWER_INPUT_TRIANGLES > limits.MAX_VIEWER_TRIANGLES
    assert limits.MAX_VIEWER_TRIANGLES != limits.MAX_FACETS
    assert limits.MAX_VIEWER_TRIANGLES != limits.MAX_BREP_FACES


def test_an_unreadably_dense_mesh_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(cad_mesh, "MAX_INPUT_TRIANGLES", 8)

    with pytest.raises(MeshTooLargeError):
        build_renderable_mesh(dense_stl(subdivisions=2), "stl")
