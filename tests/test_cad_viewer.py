"""The CAD → renderable mesh pipeline behind the 3D viewer.

There is still no JS runtime in this project, so nothing here drives a browser.
What these cover instead is everything the browser *depends* on: that the mesh
the backend produces is a geometrically correct, correctly wound, correctly
sized triangle list; that the metadata shown beside it is the same metadata
costing uses; that bad input and oversized models fail in a way the viewer can
present; and that the assets and the vendored 3D library are actually served.

The gap that leaves — that the WebGL rendering itself works — is closed by the
live verification recorded in PROJECT_STATUS.md, not here.
"""

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient

from cad_costing_agent import cad_mesh
from cad_costing_agent.cad_mesh import (
    MAX_TRIANGLES,
    MeshTooLargeError,
    build_renderable_mesh,
    build_viewer_model,
)
from cad_costing_agent.main import STATIC_PATH, app

client = TestClient(app)

BOX = (10.0, 20.0, 30.0)
BOX_VOLUME = 10.0 * 20.0 * 30.0


def stl_bytes(x=BOX[0], y=BOX[1], z=BOX[2]) -> bytes:
    return trimesh.creation.box(extents=(x, y, z)).export(file_type="stl")


def post_mesh(filename: str, data: bytes):
    return client.post(
        "/cad/mesh",
        files={"file": (filename, data, "application/octet-stream")},
    )


def enclosed_volume(mesh) -> float:
    """Volume enclosed by the triangles, via the divergence theorem.

    This is the single most informative check available on a mesh: it comes out
    right only if the positions are right, the indices reference the right
    vertices, *and* every triangle is wound outwards. A mesh with inverted
    winding gives a negative volume and would render inside-out.
    """
    vertices = np.asarray(mesh.positions, dtype=float).reshape(-1, 3)
    faces = np.asarray(mesh.indices, dtype=int).reshape(-1, 3)
    a, b, c = vertices[faces[:, 0]], vertices[faces[:, 1]], vertices[faces[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


# --- both formats produce a renderable representation -----------------------


def test_stl_produces_a_renderable_mesh():
    mesh = build_renderable_mesh(stl_bytes(), "stl")

    assert mesh.source_format == "stl"
    assert mesh.triangle_count == 12  # a box is 6 faces x 2 triangles
    assert len(mesh.positions) == mesh.vertex_count * 3
    assert len(mesh.indices) == mesh.triangle_count * 3


def test_step_produces_a_renderable_mesh(step_box_bytes):
    mesh = build_renderable_mesh(step_box_bytes(*BOX), "step")

    assert mesh.source_format == "step"
    assert mesh.triangle_count == 12
    assert len(mesh.positions) == mesh.vertex_count * 3
    assert len(mesh.indices) == mesh.triangle_count * 3


@pytest.mark.parametrize("source", ["stl", "step"])
def test_every_index_references_a_real_vertex(source, step_box_bytes):
    data = stl_bytes() if source == "stl" else step_box_bytes(*BOX)

    mesh = build_renderable_mesh(data, source)

    assert min(mesh.indices) >= 0
    assert max(mesh.indices) < mesh.vertex_count


@pytest.mark.parametrize("source", ["stl", "step"])
def test_mesh_encloses_the_right_volume_and_faces_outwards(source, step_box_bytes):
    """Wrong winding renders the part inside-out, and shows up here as a sign flip."""
    data = stl_bytes() if source == "stl" else step_box_bytes(*BOX)

    mesh = build_renderable_mesh(data, source)

    assert enclosed_volume(mesh) == pytest.approx(BOX_VOLUME, rel=1e-6)


@pytest.mark.parametrize("source", ["stl", "step"])
def test_mesh_bounds_match_the_models_bounding_box(source, step_box_bytes):
    data = stl_bytes() if source == "stl" else step_box_bytes(*BOX)

    model = build_viewer_model(data, f"box.{source}")

    extents = [b - a for a, b in zip(model.mesh.bounds.min, model.mesh.bounds.max)]
    box = model.geometry.bounding_box
    assert extents == pytest.approx([box.x, box.y, box.z], rel=1e-6)


def test_step_and_stl_share_one_representation(step_box_bytes):
    """The viewer must not care which format the model came from."""
    from_stl = build_renderable_mesh(stl_bytes(), "stl")
    from_step = build_renderable_mesh(step_box_bytes(*BOX), "step")

    assert from_stl.model_fields_set >= {"positions", "indices", "bounds"}
    assert set(from_stl.model_dump()) == set(from_step.model_dump())
    assert from_stl.triangle_count == from_step.triangle_count


# --- vertices are deliberately not merged -----------------------------------


def test_stl_triangles_are_unmerged_so_the_surface_shades_flat():
    """STL describes facets and nothing more; merging would round off corners."""
    mesh = build_renderable_mesh(stl_bytes(), "stl")

    assert mesh.vertex_count == mesh.triangle_count * 3


def test_step_vertices_are_shared_within_a_face_but_not_between_faces(step_box_bytes):
    """This is what makes derived normals give hard edges at real edges."""
    mesh = build_renderable_mesh(step_box_bytes(*BOX), "step")

    # A box merged globally would have 8 corners; per-face it has 6 x 4 = 24.
    assert mesh.vertex_count == 24


def test_a_curved_step_face_is_tessellated_into_many_triangles(step_box_bytes):
    """A cylinder must not come back as a 12-triangle box."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    data = _write_step(BRepPrimAPI_MakeCylinder(5.0, 40.0).Shape())

    mesh = build_renderable_mesh(data, "step")

    assert mesh.triangle_count > 60
    # Chordal tessellation is inscribed, so it slightly under-fills the cylinder.
    true_volume = np.pi * 25.0 * 40.0
    assert enclosed_volume(mesh) == pytest.approx(true_volume, rel=0.01)


def _write_step(shape) -> bytes:
    """Write an OCC shape out as STEP bytes (mirrors the step_box_bytes fixture)."""
    import os
    import tempfile

    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.Message import Message, Message_PrinterOStream
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    Message.DefaultMessenger_s().RemovePrinters(Message_PrinterOStream.get_type_descriptor_s())
    Interface_Static.SetCVal_s("write.step.unit", "MM")

    file_descriptor, path = tempfile.mkstemp(suffix=".step")
    os.close(file_descriptor)
    try:
        writer = STEPControl_Writer()
        writer.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)
        assert writer.Write(path) == IFSelect_ReturnStatus.IFSelect_RetDone
        with open(path, "rb") as step_file:
            return step_file.read()
    finally:
        os.unlink(path)


# --- the metadata is the same metadata costing uses -------------------------


@pytest.mark.parametrize("name", ["box.stl", "box.step"])
def test_mesh_geometry_matches_the_cad_geometry_endpoint_exactly(name, step_box_bytes):
    """The viewer panel must not show a second opinion about the geometry."""
    data = stl_bytes() if name.endswith(".stl") else step_box_bytes(*BOX)

    from_geometry = client.post("/cad/geometry", files={"file": (name, data)}).json()
    from_mesh = post_mesh(name, data).json()["geometry"]

    assert from_mesh == from_geometry


def test_reported_volume_comes_from_exact_geometry_not_from_the_triangles(step_box_bytes):
    """A tessellated cylinder's triangles under-fill it; the reported volume must not."""
    data = _write_step(_cylinder(5.0, 40.0))

    model = build_viewer_model(data, "cyl.step")

    true_volume = np.pi * 25.0 * 40.0
    assert model.geometry.volume == pytest.approx(true_volume, rel=1e-9)
    assert enclosed_volume(model.mesh) < model.geometry.volume


def _cylinder(radius, height):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    return BRepPrimAPI_MakeCylinder(radius, height).Shape()


# --- the endpoint -----------------------------------------------------------


def test_mesh_endpoint_returns_geometry_mesh_and_filename(step_box_bytes):
    response = post_mesh("bracket.step", step_box_bytes(*BOX))

    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "bracket.step"
    assert body["mesh_error"] is None
    assert body["geometry"]["source_format"] == "step"
    assert body["mesh"]["triangle_count"] == 12


def test_mesh_endpoint_reports_the_tessellation_settings_it_used(step_box_bytes):
    body = post_mesh("box.step", step_box_bytes(*BOX)).json()

    tessellation = body["mesh"]["tessellation"]
    assert tessellation["method"] == "opencascade-brepmesh"
    assert tessellation["relative_linear_deflection"] == cad_mesh.RELATIVE_LINEAR_DEFLECTION
    assert tessellation["angular_deflection"] == cad_mesh.ANGULAR_DEFLECTION_RADIANS
    assert tessellation["coarsened"] is False
    assert tessellation["linear_deflection"] > 0


def test_stl_reports_that_it_was_not_re_tessellated():
    body = post_mesh("box.stl", stl_bytes()).json()

    assert body["mesh"]["tessellation"]["method"] == "none"
    assert body["mesh"]["tessellation"]["linear_deflection"] is None


def test_mesh_endpoint_does_not_persist_an_estimate(step_box_bytes):
    """Previewing a model is not a costing event and must not appear in history."""
    before = client.get("/estimates").json()["total"]

    post_mesh("box.step", step_box_bytes(*BOX))

    assert client.get("/estimates").json()["total"] == before


# --- invalid input fails cleanly --------------------------------------------


@pytest.mark.parametrize(
    "name, data, expected",
    [
        ("part.step", b"this is not STEP data", "not valid"),
        ("part.stl", b"this is not STL data", "valid mesh"),
        ("part.stl", b"", "empty"),
        ("part.iges", b"whatever", "not valid IGES data"),
        ("noextension", b"whatever", "Unsupported CAD file format"),
    ],
)
def test_invalid_cad_files_fail_with_a_readable_message(name, data, expected):
    response = post_mesh(name, data)

    assert response.status_code == 400
    assert expected in response.json()["detail"]


def test_an_oversized_upload_is_refused_before_it_is_parsed(monkeypatch):
    # A 12-triangle binary STL is only ~684 bytes, so the cap has to go below that.
    monkeypatch.setattr(cad_mesh, "MAX_UPLOAD_BYTES", 100)

    response = post_mesh("huge.stl", stl_bytes())

    assert response.status_code == 400
    assert "MB limit" in response.json()["detail"]


# --- the triangle budget ----------------------------------------------------


def test_a_dense_step_model_is_re_tessellated_more_coarsely(monkeypatch):
    """Rather than sending an enormous payload, one coarsening pass is tried."""
    data = _write_step(_cylinder(5.0, 40.0))
    fine = build_renderable_mesh(data, "step")
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", fine.triangle_count - 1)

    coarse = build_renderable_mesh(data, "step")

    assert coarse.tessellation.coarsened is True
    assert coarse.triangle_count < fine.triangle_count
    assert "less detailed" in coarse.tessellation.note


def test_a_model_that_cannot_be_previewed_at_all_still_returns_geometry(monkeypatch):
    """A part that cannot be drawn must still be a part you can cost.

    A budget of two triangles is below what any reduction of a box can produce,
    so this exercises the last resort — the preview is withheld — rather than
    the normal over-budget path, which simplifies instead. See
    tests/test_display_simplification.py for that.
    """
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 2)
    data = stl_bytes()

    response = post_mesh("dense.stl", data)

    assert response.status_code == 200
    body = response.json()
    assert body["mesh"] is None
    assert "only the 3D preview is unavailable" in body["mesh_error"]
    assert body["geometry"]["volume"] == pytest.approx(BOX_VOLUME, rel=1e-6)

    # And the costing path is genuinely unaffected by the preview limit.
    estimate = client.post(
        "/cost/cad-part",
        files={"file": ("dense.stl", data, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Dense",
            "quantity": "1",
            "rate_set_id": "poc-machine-shop",
        },
    )
    assert estimate.status_code == 200
    assert estimate.json()["part_cost"]["total_part_cost"] > 0


def test_an_unpreviewable_step_model_raises_rather_than_returning_junk(monkeypatch):
    """Two triangles is below anything a cylinder can be reduced to."""
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 2)

    with pytest.raises(MeshTooLargeError):
        build_renderable_mesh(_write_step(_cylinder(5.0, 40.0)), "step")


def test_a_model_the_viewer_will_not_even_read_degrades_to_geometry_only(monkeypatch):
    """The read bound and the send bound are different limits.

    Past the send bound a model is simplified; past the read bound there is no
    preview at all. Neither touches geometry or costing.
    """
    monkeypatch.setattr(cad_mesh, "MAX_INPUT_TRIANGLES", 4)

    model = build_viewer_model(stl_bytes(), "dense.stl")

    assert model.mesh is None
    assert "the preview will read" in model.mesh_error
    assert "only the 3D preview is unavailable" in model.mesh_error
    assert model.geometry.volume == pytest.approx(BOX_VOLUME, rel=1e-6)


def test_the_default_triangle_budget_is_sane():
    assert 1_000 < MAX_TRIANGLES <= 500_000


# --- assets are served ------------------------------------------------------


@pytest.mark.parametrize("asset", ["viewer.js", "vendor/three.module.min.js"])
def test_viewer_assets_are_served(asset):
    response = client.get(f"/ui/{asset}")

    assert response.status_code == 200
    assert response.content


@pytest.mark.parametrize("asset", ["app.js", "viewer.js", "vendor/three.module.min.js"])
def test_javascript_is_served_as_javascript(asset):
    """A module served as text/plain is refused by the browser and the viewer dies silently."""
    response = client.get(f"/ui/{asset}")

    assert "javascript" in response.headers["content-type"]


def test_viewer_files_exist_on_disk():
    for name in ("viewer.js", "vendor/three.module.min.js", "vendor/three.LICENSE.txt"):
        assert (STATIC_PATH / name).is_file()


def test_the_vendored_library_is_an_es_module_carrying_its_licence():
    source = (STATIC_PATH / "vendor" / "three.module.min.js").read_text(encoding="utf-8")

    assert "SPDX-License-Identifier: MIT" in source
    assert "export" in source
    assert "The MIT License" in (STATIC_PATH / "vendor" / "three.LICENSE.txt").read_text(
        encoding="utf-8"
    )


def test_the_page_loads_the_viewer_and_its_local_library():
    html = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    viewer_js = (STATIC_PATH / "viewer.js").read_text(encoding="utf-8")

    assert 'type="module" src="viewer.js"' in html
    assert 'id="viewer-canvas"' in html
    # Vendored, not fetched from a CDN, so the viewer works offline.
    assert 'from "./vendor/three.module.min.js"' in viewer_js
    assert "http://" not in viewer_js and "https://" not in viewer_js


def test_the_page_offers_fit_and_reset_controls():
    html = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    assert 'id="viewer-fit"' in html
    assert 'id="viewer-reset"' in html


def test_viewer_overlay_states_do_not_collide_with_the_page_error_style():
    """A bare `error` class here would be repainted by the form's `.error` rule.

    That rule is defined later in the stylesheet with equal specificity, so the
    overlay would silently take on the page's error-box background, border and
    padding instead of its own.
    """
    viewer_js = (STATIC_PATH / "viewer.js").read_text(encoding="utf-8")
    styles = (STATIC_PATH / "styles.css").read_text(encoding="utf-8")

    assert "viewer-overlay is-${kind}" in viewer_js
    assert ".viewer-overlay.is-error" in styles
    assert ".viewer-overlay.error" not in styles


def test_the_viewer_reports_loading_and_error_states():
    """The panel has to say something while tessellating, and when it fails."""
    viewer_js = (STATIC_PATH / "viewer.js").read_text(encoding="utf-8")
    app_js = (STATIC_PATH / "app.js").read_text(encoding="utf-8")

    assert "loading:" in viewer_js and "error:" in viewer_js
    assert ".loading(" in app_js and ".error(" in app_js
    # A model that parses but cannot be previewed degrades the viewer only.
    assert "mesh_error" in app_js


def test_the_viewer_contains_no_costing_arithmetic():
    """The viewer draws pixels; it must never take part in costing.

    Matched against costing-specific identifiers rather than loose words like
    "cost", which appear legitimately in the file's own explanation of why it
    does no costing.
    """
    viewer_js = (STATIC_PATH / "viewer.js").read_text(encoding="utf-8")

    for forbidden in [
        "/cost/",
        "/rates",
        "total_part_cost",
        "per_unit_cost",
        "rate_set",
        "cost_per_kg",
        "machine_rate",
        "overhead_rate",
        "scrap_rate",
        "tooling_cost",
        "quantity",
        "currency",
    ]:
        assert forbidden not in viewer_js


def test_the_frontend_asks_the_backend_for_the_mesh_rather_than_parsing_cad():
    """STEP/B-Rep parsing stays server-side."""
    app_js = (STATIC_PATH / "app.js").read_text(encoding="utf-8")

    assert '"/cad/mesh"' in app_js
    for parser_hint in ["STEPControl", "ISO-10303", "parseStep", "STLLoader"]:
        assert parser_hint not in app_js


# --- nothing about costing changed ------------------------------------------


def test_the_costing_endpoints_still_work_alongside_the_viewer(step_box_bytes):
    response = client.post(
        "/cost/cad-part",
        files={"file": ("box.step", step_box_bytes(*BOX), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-001",
            "part_name": "Bracket",
            "quantity": "10",
            "rate_set_id": "poc-machine-shop",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["part_cost"]["currency"] == "INR"
    assert body["geometry"]["volume"] == pytest.approx(BOX_VOLUME, rel=1e-6)


def test_the_mesh_endpoint_does_not_change_what_cad_part_costing_returns(step_box_bytes):
    """Previewing first must not perturb the estimate that follows."""
    data = step_box_bytes(*BOX)
    form = {
        "material": "Steel",
        "source_unit": "mm",
        "part_id": "P-001",
        "part_name": "Bracket",
        "quantity": "4",
        "rate_set_id": "poc-machine-shop",
    }

    def cost():
        return client.post(
            "/cost/cad-part",
            files={"file": ("box.step", data, "application/octet-stream")},
            data=form,
        ).json()["part_cost"]["total_part_cost"]

    before = cost()
    post_mesh("box.step", data)
    assert cost() == before
