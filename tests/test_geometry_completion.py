"""Costing a CAD file whose geometry the file itself does not contain.

Some real exports simply do not carry enough polygons to enclose a solid, and
no amount of deterministic repair can invent the missing surface. Rather than
stopping there, the system says exactly what is known and what is missing and
asks the user to supply it.

The whole point is that the supplied figure is an **assumption, not a
measurement**, so most of these tests are about that distinction surviving:
through the mass calculation, into the estimate, into the stored snapshot, and
back out of history unchanged.
"""

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cad_costing_agent.cad_geometry import (
    COMPLETE,
    INCOMPLETE,
    MISSING_VOLUME,
    TOTAL_VOLUME,
    GeometryCompletion,
    complete_geometry,
    extract_geometry_from_bytes,
)
from cad_costing_agent.db import get_session, init_db
from cad_costing_agent.main import app
from tests.faceted_fixtures import faceted_plain_block, faceted_step_from_mesh, faceted_tube
from tests.step_fixtures import ALL_FIXTURES
from tests.test_faceted_geometry import CUBE_TRIANGLES, CUBE_VERTICES

client = TestClient(app)


def partly_measurable() -> bytes:
    """A 10 mm cube (measurable) beside an open sheet (not).

    Volume of the closed part is 1000 mm³, so a supplied "missing" figure has
    something real to be added to.
    """
    sheet = np.array([[0.0, 0.0, 50.0], [10.0, 0.0, 50.0], [10.0, 10.0, 50.0]])
    vertices = np.vstack([CUBE_VERTICES * 10.0, sheet])
    triangles = np.vstack([CUBE_TRIANGLES, np.array([[8, 9, 10]])])
    return faceted_step_from_mesh(vertices, triangles)


def cost(data: bytes, filename="part.step", **extra):
    form = {
        "material": "Steel",
        "source_unit": "mm",
        "part_id": "P-001",
        "part_name": "Part",
        "quantity": "10",
        "rate_set_id": "poc-machine-shop",
    }
    form.update(extra)
    return client.post(
        "/cost/cad-part",
        files={"file": (filename, data, "application/octet-stream")},
        data=form,
    )


# --- geometry that needs nothing --------------------------------------------


def test_analytic_geometry_is_complete_and_asks_for_nothing():
    geometry = extract_geometry_from_bytes(ALL_FIXTURES["plain_block"](), ".step")

    assert geometry.geometry_status == COMPLETE
    assert geometry.incomplete is None
    assert cost(ALL_FIXTURES["plain_block"]()).status_code == 200


def test_a_watertight_faceted_model_is_complete_and_asks_for_nothing():
    geometry = extract_geometry_from_bytes(faceted_plain_block(), ".step")

    assert geometry.geometry_status == COMPLETE
    assert geometry.incomplete is None
    assert cost(faceted_plain_block()).status_code == 200


def test_stl_is_complete_and_asks_for_nothing():
    box = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")

    geometry = extract_geometry_from_bytes(box, ".stl")

    assert geometry.geometry_status == COMPLETE
    assert cost(box, filename="box.stl", source_unit="mm").status_code == 200


def test_supplying_an_assumption_for_measurable_geometry_is_refused():
    """An assumption must never quietly override a real measurement."""
    response = cost(
        faceted_plain_block(),
        geometry_completion_method=TOTAL_VOLUME,
        geometry_completion_volume_mm3="123456",
    )

    assert response.status_code == 400
    assert "already complete" in response.json()["detail"] or "was not expected" in str(
        response.json()["detail"]
    )


# --- geometry that needs input ----------------------------------------------


def test_incomplete_geometry_says_what_is_known_and_what_is_missing():
    geometry = extract_geometry_from_bytes(partly_measurable(), ".step")

    assert geometry.geometry_status == INCOMPLETE
    assert geometry.volume == 0.0
    report = geometry.incomplete
    assert report is not None
    assert report.closed_component_count == 1
    assert report.open_component_count == 1
    assert report.known_volume_mm3 == pytest.approx(1000.0, rel=1e-9)
    assert 0.0 < report.known_polygon_share < 1.0
    assert report.unresolved and report.unresolved[0].boundary_edge_count > 0
    assert report.resolution_methods == [TOTAL_VOLUME, MISSING_VOLUME, "upload_corrected_cad"]


def test_costing_incomplete_geometry_returns_a_needs_input_state():
    response = cost(partly_measurable())

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "needs_geometry_input"
    assert detail["geometry"]["known_volume_mm3"] == pytest.approx(1000.0, rel=1e-9)
    assert "measure" in detail["message"]


def test_the_mass_endpoint_asks_for_the_same_input():
    response = client.post(
        "/cad/mass",
        files={"file": ("part.step", partly_measurable(), "application/octet-stream")},
        data={"material": "Steel", "source_unit": "mm"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "needs_geometry_input"


# --- supplying the missing information --------------------------------------


def test_a_supplied_total_volume_is_used_verbatim():
    geometry = extract_geometry_from_bytes(partly_measurable(), ".step")

    completed = complete_geometry(
        geometry, GeometryCompletion(method=TOTAL_VOLUME, volume_mm3=2500.0)
    )

    assert completed.volume == pytest.approx(2500.0, rel=1e-12)
    assert completed.geometry_status == COMPLETE
    assert completed.geometry_source == "faceted_user_completed"
    assert completed.volume_method == "user_supplied_total_volume"
    assert completed.volume_confidence == "user_supplied"
    assert completed.user_supplied_total_volume_mm3 == 2500.0
    assert completed.user_supplied_missing_volume_mm3 is None


def test_a_supplied_missing_volume_is_added_to_what_was_measured():
    geometry = extract_geometry_from_bytes(partly_measurable(), ".step")

    completed = complete_geometry(
        geometry, GeometryCompletion(method=MISSING_VOLUME, volume_mm3=250.0)
    )

    assert completed.volume == pytest.approx(1250.0, rel=1e-12)
    assert completed.volume_method == "known_mesh_plus_user_supplied_missing_volume"
    assert completed.user_supplied_missing_volume_mm3 == 250.0
    assert completed.user_supplied_total_volume_mm3 is None


def test_the_assumption_is_spelled_out_in_the_units_note():
    geometry = extract_geometry_from_bytes(partly_measurable(), ".step")

    completed = complete_geometry(
        geometry, GeometryCompletion(method=MISSING_VOLUME, volume_mm3=250.0)
    )

    assert "250.000 mm³" in completed.units_note
    assert "stated assumption, not a measurement" in completed.units_note


def test_a_completed_volume_is_never_labelled_as_exact_cad_geometry():
    geometry = extract_geometry_from_bytes(partly_measurable(), ".step")

    completed = complete_geometry(
        geometry, GeometryCompletion(method=TOTAL_VOLUME, volume_mm3=2500.0)
    )

    assert completed.geometry_source != "brep"
    assert completed.volume_confidence != "high"


@pytest.mark.parametrize("volume", [0.0, -1.0, -2500.0])
def test_a_non_positive_supplied_volume_is_rejected(volume):
    with pytest.raises(ValueError):
        GeometryCompletion(method=TOTAL_VOLUME, volume_mm3=volume)


def test_an_unknown_method_is_rejected():
    geometry = extract_geometry_from_bytes(partly_measurable(), ".step")

    with pytest.raises(ValueError, match="Unknown geometry completion method"):
        complete_geometry(geometry, GeometryCompletion(method="guess_it", volume_mm3=100.0))


def test_completing_already_complete_geometry_is_rejected():
    geometry = extract_geometry_from_bytes(faceted_plain_block(), ".step")

    with pytest.raises(ValueError, match="already complete"):
        complete_geometry(geometry, GeometryCompletion(method=TOTAL_VOLUME, volume_mm3=1.0))


def test_completion_is_deterministic():
    data = partly_measurable()

    first = complete_geometry(
        extract_geometry_from_bytes(data, ".step"),
        GeometryCompletion(method=MISSING_VOLUME, volume_mm3=250.0),
    )
    second = complete_geometry(
        extract_geometry_from_bytes(data, ".step"),
        GeometryCompletion(method=MISSING_VOLUME, volume_mm3=250.0),
    )

    assert first.model_dump() == second.model_dump()


# --- through mass and costing -----------------------------------------------


def test_mass_uses_the_completed_volume():
    response = client.post(
        "/cad/mass",
        files={"file": ("part.step", partly_measurable(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "geometry_completion_method": TOTAL_VOLUME,
            "geometry_completion_volume_mm3": "2500",
        },
    )

    assert response.status_code == 200
    # 2500 mm^3 = 2.5e-6 m^3 at 7850 kg/m^3.
    assert response.json()["mass_kg"] == pytest.approx(2.5e-6 * 7850.0, rel=1e-9)


@pytest.mark.parametrize(
    "method, supplied, expected_volume",
    [(TOTAL_VOLUME, "2500", 2500.0), (MISSING_VOLUME, "1500", 2500.0)],
)
def test_costing_succeeds_once_the_geometry_is_supplied(method, supplied, expected_volume):
    response = cost(
        partly_measurable(),
        geometry_completion_method=method,
        geometry_completion_volume_mm3=supplied,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["geometry"]["volume"] == pytest.approx(expected_volume, rel=1e-9)
    assert body["geometry"]["volume_confidence"] == "user_supplied"
    assert body["mass"]["mass_kg"] == pytest.approx(expected_volume * 1e-9 * 7850.0, rel=1e-9)
    assert body["part_cost"]["total_part_cost"] > 0


def test_a_completed_estimate_still_uses_the_rate_sets_material_density():
    profile = {
        "rate_set_id": "completion-shop",
        "name": "Completion Shop",
        "version": "1.0.0",
        "currency": "INR",
        "is_illustrative": False,
        "materials": {"steel": {"name": "Steel", "density_kg_m3": 8000.0, "cost_per_kg": 60.0}},
        "operations": {"milling": {"machine_rate_per_hour": 1000.0, "labor_rate_per_hour": 400.0}},
        "additional_costs": {},
    }
    assert client.post("/rates", json=profile).status_code == 201

    body = cost(
        partly_measurable(),
        quantity="1",
        rate_set_id="completion-shop",
        geometry_completion_method=TOTAL_VOLUME,
        geometry_completion_volume_mm3="2500",
    ).json()

    assert body["mass"]["material"]["density_kg_m3"] == 8000.0
    assert body["mass"]["mass_kg"] == pytest.approx(2.5e-6 * 8000.0, rel=1e-9)


def test_a_partial_geometry_supply_is_rejected():
    response = cost(partly_measurable(), geometry_completion_method=TOTAL_VOLUME)

    assert response.status_code == 400
    assert "needs both a method" in response.json()["detail"]


# --- the assumption is recorded ---------------------------------------------


def test_the_assumption_is_persisted_on_the_estimate():
    estimate_id = cost(
        partly_measurable(),
        geometry_completion_method=MISSING_VOLUME,
        geometry_completion_volume_mm3="1500",
    ).json()["part_cost"]["estimate_id"]

    stored = client.get(f"/estimates/{estimate_id}").json()
    request = stored["request_snapshot"]

    assert request["geometry_completion"]["method"] == MISSING_VOLUME
    assert request["geometry_completion"]["volume_mm3"] == 1500.0
    assert request["geometry_completion"]["resulting_volume_mm3"] == pytest.approx(2500.0)
    # And inside the geometry itself, so it cannot be missed.
    assert request["geometry"]["user_supplied_missing_volume_mm3"] == 1500.0
    assert request["geometry"]["volume_confidence"] == "user_supplied"


def test_an_estimate_made_without_an_assumption_records_none():
    estimate_id = cost(faceted_plain_block()).json()["part_cost"]["estimate_id"]

    stored = client.get(f"/estimates/{estimate_id}").json()

    assert stored["request_snapshot"]["geometry_completion"] is None


def test_a_later_assumption_does_not_change_an_earlier_estimate():
    """History is a record of what was quoted, not of what is believed now."""
    data = partly_measurable()
    first_id = cost(
        data, geometry_completion_method=MISSING_VOLUME, geometry_completion_volume_mm3="1500"
    ).json()["part_cost"]["estimate_id"]
    first_total = client.get(f"/estimates/{first_id}").json()["total_part_cost"]

    second = cost(
        data, geometry_completion_method=MISSING_VOLUME, geometry_completion_volume_mm3="9000"
    ).json()
    assert second["geometry"]["volume"] == pytest.approx(10000.0, rel=1e-9)

    unchanged = client.get(f"/estimates/{first_id}").json()
    assert unchanged["total_part_cost"] == first_total
    assert unchanged["request_snapshot"]["geometry_completion"]["volume_mm3"] == 1500.0


def test_the_assumption_survives_a_restart(tmp_path):
    database_path = tmp_path / "completion.db"

    def open_database():
        engine = create_engine(
            f"sqlite:///{database_path}", connect_args={"check_same_thread": False}
        )
        init_db(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)

        def override():
            with factory() as session:
                yield session

        app.dependency_overrides[get_session] = override
        return engine

    engine = open_database()
    estimate_id = cost(
        partly_measurable(),
        geometry_completion_method=TOTAL_VOLUME,
        geometry_completion_volume_mm3="4321",
    ).json()["part_cost"]["estimate_id"]
    engine.dispose()

    engine = open_database()
    stored = client.get(f"/estimates/{estimate_id}").json()
    assert stored["request_snapshot"]["geometry_completion"]["volume_mm3"] == 4321.0
    assert stored["request_snapshot"]["geometry"]["volume"] == pytest.approx(4321.0)
    engine.dispose()


# --- the viewer stays independent -------------------------------------------


def test_the_viewer_still_renders_a_model_whose_geometry_needs_input():
    response = client.post(
        "/cad/mesh", files={"file": ("part.step", partly_measurable())}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mesh"] is not None
    assert body["geometry"]["geometry_status"] == INCOMPLETE


def test_costing_does_not_depend_on_the_viewer(monkeypatch):
    import cad_costing_agent.cad_mesh as cad_mesh

    data = partly_measurable()
    monkeypatch.setattr(cad_mesh, "MAX_TRIANGLES", 2)

    preview = client.post("/cad/mesh", files={"file": ("part.step", data)})
    assert preview.json()["mesh"] is None

    assert (
        cost(
            data,
            geometry_completion_method=TOTAL_VOLUME,
            geometry_completion_volume_mm3="2500",
        ).status_code
        == 200
    )


# --- nothing about costing changed ------------------------------------------


def test_analytic_costing_is_unchanged():
    body = cost(ALL_FIXTURES["block_with_through_hole"]()).json()

    expected = 60.0 * 40.0 * 20.0 - np.pi * 5.0**2 * 20.0
    assert body["geometry"]["volume"] == pytest.approx(expected, rel=1e-9)
    assert body["geometry"]["volume_confidence"] == "high"
    assert body["geometry"]["user_supplied_total_volume_mm3"] is None


# --- the UI panel -----------------------------------------------------------


def test_the_page_has_a_geometry_gap_panel():
    from cad_costing_agent.main import STATIC_PATH

    html = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    for element in ("geometry-gap", "gap-summary", "gap-explanation", "gap-volume", "apply-gap"):
        assert f'id="{element}"' in html
    for method in ("total_volume", "missing_volume", "upload_corrected_cad"):
        assert f'value="{method}"' in html


def test_the_panel_sends_the_assumption_rather_than_computing_a_volume():
    from cad_costing_agent.main import STATIC_PATH

    app_js = (STATIC_PATH / "app.js").read_text(encoding="utf-8")

    assert "geometry_completion_method" in app_js
    assert "geometry_completion_volume_mm3" in app_js
    assert "incomplete_requires_input" in app_js
    # The resulting volume and mass come back from the API, not from arithmetic here.
    for forbidden in ["known_volume_mm3 +", "+ supplied", "* density"]:
        assert forbidden not in app_js


def test_the_result_states_when_a_volume_was_assumed():
    from cad_costing_agent.main import STATIC_PATH

    app_js = (STATIC_PATH / "app.js").read_text(encoding="utf-8")

    assert "Volume assumed, not measured" in app_js
    assert "user_supplied_total_volume_mm3" in app_js
