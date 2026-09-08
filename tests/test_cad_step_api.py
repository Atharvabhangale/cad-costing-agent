import json

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.main import app
from cad_costing_agent.materials import MATERIALS

client = TestClient(app)

STEEL_DENSITY_KG_M3 = MATERIALS["steel"].density_kg_m3
EXPECTED_MASS_KG = 6000.0 * (0.001**3) * STEEL_DENSITY_KG_M3


def test_cad_geometry_endpoint_accepts_step(step_box_bytes):
    response = client.post(
        "/cad/geometry",
        files={"file": ("box.step", step_box_bytes(10.0, 20.0, 30.0), "application/octet-stream")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source_format"] == "step"
    assert body["volume"] == pytest.approx(6000.0)
    assert body["surface_area"] == pytest.approx(2200.0)
    assert body["bounding_box"] == pytest.approx({"x": 10.0, "y": 20.0, "z": 30.0})
    assert body["detected_unit"] == "mm"


def test_cad_geometry_endpoint_accepts_stp_extension(step_box_bytes):
    response = client.post(
        "/cad/geometry",
        files={"file": ("box.stp", step_box_bytes(), "application/octet-stream")},
    )

    assert response.status_code == 200
    assert response.json()["source_format"] == "step"


def test_cad_geometry_endpoint_invalid_step_rejected():
    response = client.post(
        "/cad/geometry",
        files={"file": ("box.step", b"not a step file", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert "not valid STEP" in response.json()["detail"]


def test_cad_geometry_endpoint_empty_step_rejected():
    response = client.post(
        "/cad/geometry",
        files={"file": ("box.step", b"", "application/octet-stream")},
    )

    assert response.status_code == 400


def test_cad_mass_endpoint_accepts_step(step_box_bytes):
    response = client.post(
        "/cad/mass",
        files={"file": ("box.step", step_box_bytes(10.0, 20.0, 30.0), "application/octet-stream")},
        data={"material": "Steel", "source_unit": "mm"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mass_kg"] == pytest.approx(EXPECTED_MASS_KG)
    assert body["detected_unit"] == "mm"
    assert body["material"]["name"] == "Steel"


def test_cad_mass_endpoint_rejects_conflicting_unit_for_step(step_box_bytes):
    response = client.post(
        "/cad/mass",
        files={"file": ("box.step", step_box_bytes(), "application/octet-stream")},
        data={"material": "Steel", "source_unit": "inch"},
    )

    assert response.status_code == 400
    assert "conflicts with the unit detected" in response.json()["detail"]


def test_cad_mass_endpoint_unknown_material_with_step(step_box_bytes):
    response = client.post(
        "/cad/mass",
        files={"file": ("box.step", step_box_bytes(), "application/octet-stream")},
        data={"material": "Unobtainium", "source_unit": "mm"},
    )

    assert response.status_code == 400
    assert "Unknown material" in response.json()["detail"]


def test_cost_cad_part_endpoint_accepts_step_material_only(step_box_bytes):
    response = client.post(
        "/cost/cad-part",
        files={"file": ("box.step", step_box_bytes(10.0, 20.0, 30.0), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "1",
            "operations": "[]",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["geometry"]["source_format"] == "step"
    assert body["mass"]["mass_kg"] == pytest.approx(EXPECTED_MASS_KG)
    assert body["part_cost"]["operations"] == []
    assert body["part_cost"]["total_part_cost"] == body["part_cost"]["total_material_cost"]


def test_cost_cad_part_endpoint_accepts_step_with_operations(step_box_bytes):
    operations = [
        {
            "operation_id": "OP1",
            "operation_name": "Cutting",
            "process_type": "Cutting",
            "machine_rate_per_hour": 1800.0,
            "labor_rate_per_hour": 500.0,
            "setup_time_minutes": 20.0,
            "cycle_time_minutes": 2.0,
            "quantity": 10,
        }
    ]

    response = client.post(
        "/cost/cad-part",
        files={"file": ("box.step", step_box_bytes(10.0, 20.0, 30.0), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "10",
            "operations": json.dumps(operations),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["part_cost"]["operations"]) == 1
    # Reuses the existing operation cost formula unchanged.
    assert body["part_cost"]["operations"][0]["total_operation_cost"] == 1366.67  # 2dp
    assert body["part_cost"]["total_part_cost"] == pytest.approx(
        body["part_cost"]["total_material_cost"] + body["part_cost"]["total_operation_cost"],
        abs=0.02,  # independently rounded amounts
    )


def test_step_and_stl_uploads_produce_equal_part_cost(step_box_bytes):
    """Same physical box, two CAD formats, identical cost — no format-specific costing."""
    import trimesh

    stl_bytes = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")
    form = {
        "material": "Steel",
        "source_unit": "mm",
        "part_id": "P001",
        "part_name": "Disc",
        "quantity": "10",
        "operations": "[]",
    }

    step_response = client.post(
        "/cost/cad-part",
        files={"file": ("box.step", step_box_bytes(10.0, 20.0, 30.0), "application/octet-stream")},
        data=form,
    )
    stl_response = client.post(
        "/cost/cad-part",
        files={"file": ("box.stl", stl_bytes, "application/octet-stream")},
        data=form,
    )

    assert step_response.status_code == stl_response.status_code == 200
    assert step_response.json()["part_cost"]["total_part_cost"] == pytest.approx(
        stl_response.json()["part_cost"]["total_part_cost"]
    )
