import json

import trimesh
import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.main import app
from cad_costing_agent.materials import MATERIALS

client = TestClient(app)

STEEL_DENSITY_KG_M3 = MATERIALS["steel"].density_kg_m3


def _cube_stl_bytes(x: float = 10.0, y: float = 20.0, z: float = 30.0) -> bytes:
    box = trimesh.creation.box(extents=(x, y, z))
    return box.export(file_type="stl")


def test_cad_mass_endpoint_known_cube():
    data = _cube_stl_bytes(10.0, 20.0, 30.0)

    response = client.post(
        "/cad/mass",
        files={"file": ("cube.stl", data, "application/octet-stream")},
        data={"material": "Steel", "source_unit": "mm"},
    )

    assert response.status_code == 200
    body = response.json()
    expected_mass_kg = 6000.0 * (0.001**3) * STEEL_DENSITY_KG_M3
    assert body["mass_kg"] == pytest.approx(expected_mass_kg)
    assert body["source_unit"] == "mm"
    assert body["material"]["name"] == "Steel"
    assert body["is_illustrative_material"] is True


@pytest.mark.parametrize("unit", ["mm", "cm", "m", "inch"])
def test_cad_mass_endpoint_supports_each_unit(unit):
    data = _cube_stl_bytes(10.0, 20.0, 30.0)

    response = client.post(
        "/cad/mass",
        files={"file": ("cube.stl", data, "application/octet-stream")},
        data={"material": "Steel", "source_unit": unit},
    )

    assert response.status_code == 200
    assert response.json()["source_unit"] == unit


def test_cad_mass_endpoint_unknown_material():
    data = _cube_stl_bytes()

    response = client.post(
        "/cad/mass",
        files={"file": ("cube.stl", data, "application/octet-stream")},
        data={"material": "Unobtainium", "source_unit": "mm"},
    )

    assert response.status_code == 400
    assert "Unknown material" in response.json()["detail"]


def test_cad_mass_endpoint_unsupported_unit():
    data = _cube_stl_bytes()

    response = client.post(
        "/cad/mass",
        files={"file": ("cube.stl", data, "application/octet-stream")},
        data={"material": "Steel", "source_unit": "furlong"},
    )

    assert response.status_code == 400
    assert "Unsupported length unit" in response.json()["detail"]


def test_cad_mass_endpoint_empty_file_rejected():
    response = client.post(
        "/cad/mass",
        files={"file": ("cube.stl", b"", "application/octet-stream")},
        data={"material": "Steel", "source_unit": "mm"},
    )

    assert response.status_code == 400


def test_cad_mass_endpoint_invalid_stl_rejected():
    response = client.post(
        "/cad/mass",
        files={"file": ("cube.stl", b"not a valid stl", "application/octet-stream")},
        data={"material": "Steel", "source_unit": "mm"},
    )

    assert response.status_code == 400


def test_cost_cad_part_endpoint_material_only():
    data = _cube_stl_bytes(10.0, 20.0, 30.0)

    response = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", data, "application/octet-stream")},
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
    assert body["geometry"]["volume"] == pytest.approx(6000.0)
    expected_mass_kg = 6000.0 * (0.001**3) * STEEL_DENSITY_KG_M3
    assert body["mass"]["mass_kg"] == pytest.approx(expected_mass_kg)
    assert body["part_cost"]["material"]["mass_kg"] == pytest.approx(expected_mass_kg)
    assert body["part_cost"]["operations"] == []
    assert body["part_cost"]["total_part_cost"] == body["part_cost"]["total_material_cost"]


def test_cost_cad_part_endpoint_with_operations():
    data = _cube_stl_bytes(10.0, 20.0, 30.0)
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
        files={"file": ("cube.stl", data, "application/octet-stream")},
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
    assert body["part_cost"]["total_part_cost"] == pytest.approx(
        body["part_cost"]["total_material_cost"] + body["part_cost"]["total_operation_cost"],
        abs=0.02,  # each displayed amount is independently rounded to 2dp
    )


def test_cost_cad_part_endpoint_quantity_scales_material_cost_but_not_mass():
    data = _cube_stl_bytes(10.0, 20.0, 30.0)

    single_response = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", data, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "1",
            "operations": "[]",
        },
    )
    bulk_response = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", data, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "10",
            "operations": "[]",
        },
    )

    single_body = single_response.json()
    bulk_body = bulk_response.json()

    assert single_body["mass"]["mass_kg"] == pytest.approx(bulk_body["mass"]["mass_kg"])
    # Displayed material cost is rounded, so scaling the rounded single-unit figure
    # only matches the rounded batch figure to within the rounding step.
    assert bulk_body["part_cost"]["total_material_cost"] == pytest.approx(
        single_body["part_cost"]["total_material_cost"] * 10, abs=0.10
    )
