import pytest
import trimesh
from fastapi.testclient import TestClient

from cad_costing_agent.main import app

client = TestClient(app)

# Same worked example as tests/test_additional_costs.py:
#   material 8.00 + operations 960.00 = direct base 968.00
#   overhead 10% = 96.80, scrap 5% = 48.40, tooling = 250.00
#   total_part_cost = 1363.20
WORKED_PART = {
    "part_id": "P100",
    "part_name": "Flange",
    "material": "Steel",
    "mass_kg": 2.5,
    "quantity": 4,
}
WORKED_OPERATION = {
    "operation_id": "OP1",
    "operation_name": "Milling",
    "process_type": "Milling",
    "machine_rate_per_hour": 1200.0,
    "labor_rate_per_hour": 600.0,
    "setup_time_minutes": 30.0,
    "cycle_time_minutes": 3.0,
    "quantity": 4,
}


def _cube_stl_bytes() -> bytes:
    return trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")


def test_cost_part_endpoint_with_all_additional_costs():
    response = client.post(
        "/cost/part",
        json={
            "part": WORKED_PART,
            "operations": [WORKED_OPERATION],
            "additional_costs": {
                "overhead_rate_percent": 10.0,
                "scrap_rate_percent": 5.0,
                "tooling_cost_per_batch": 250.0,
            },
        },
    )

    assert response.status_code == 200
    body = response.json()

    assert body["direct_cost_base"] == pytest.approx(968.00)
    assert body["additional_costs"]["overhead_cost"] == pytest.approx(96.80)
    assert body["additional_costs"]["scrap_cost"] == pytest.approx(48.40)
    assert body["additional_costs"]["tooling_cost"] == pytest.approx(250.00)
    assert body["total_additional_cost"] == pytest.approx(395.20)
    assert body["total_part_cost"] == pytest.approx(1363.20)
    # Components stay individually visible, with their formulas and assumptions.
    assert body["additional_costs"]["formulas"]
    assert body["additional_costs"]["assumptions"]


def test_cost_part_endpoint_total_equals_sum_of_components():
    response = client.post(
        "/cost/part",
        json={
            "part": WORKED_PART,
            "operations": [WORKED_OPERATION],
            "additional_costs": {
                "overhead_rate_percent": 12.0,
                "scrap_rate_percent": 3.0,
                "tooling_cost_per_batch": 75.0,
            },
        },
    )

    body = response.json()
    additional = body["additional_costs"]

    assert body["total_part_cost"] == pytest.approx(
        body["total_material_cost"]
        + body["total_operation_cost"]
        + additional["overhead_cost"]
        + additional["scrap_cost"]
        + additional["tooling_cost"]
    )


def test_cost_part_endpoint_without_additional_costs_is_unchanged():
    """Regression: omitting the new field reproduces the pre-Step-8 total."""
    response = client.post(
        "/cost/part",
        json={"part": WORKED_PART, "operations": [WORKED_OPERATION]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_additional_cost"] == 0
    assert body["total_part_cost"] == pytest.approx(
        body["total_material_cost"] + body["total_operation_cost"]
    )


@pytest.mark.parametrize(
    "additional",
    [
        {"overhead_rate_percent": -5.0},
        {"scrap_rate_percent": -5.0},
        {"tooling_cost_per_batch": -5.0},
        {"scrap_rate_percent": 100.0},
        {"scrap_rate_percent": 150.0},
        {"overhead_rate_percent": 5000.0},
    ],
)
def test_cost_part_endpoint_rejects_invalid_additional_costs(additional):
    response = client.post(
        "/cost/part",
        json={"part": WORKED_PART, "operations": [WORKED_OPERATION], "additional_costs": additional},
    )

    assert response.status_code == 422


def test_cost_cad_part_endpoint_with_additional_costs():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", _cube_stl_bytes(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "10",
            "operations": "[]",
            "overhead_rate_percent": "10.0",
            "scrap_rate_percent": "5.0",
            "tooling_cost_per_batch": "100.0",
        },
    )

    assert response.status_code == 200
    part_cost = response.json()["part_cost"]

    # Percentages are taken from the exact base, then each result is rounded, so
    # comparing against the *displayed* base only holds to within a penny.
    base = part_cost["direct_cost_base"]
    assert part_cost["additional_costs"]["overhead_cost"] == pytest.approx(base * 0.10, abs=0.01)
    assert part_cost["additional_costs"]["scrap_cost"] == pytest.approx(base * 0.05, abs=0.01)
    assert part_cost["additional_costs"]["tooling_cost"] == pytest.approx(100.0)
    assert part_cost["total_part_cost"] == pytest.approx(
        base + part_cost["total_additional_cost"], abs=0.02  # independently rounded amounts
    )


def test_cost_cad_part_endpoint_without_additional_costs_is_unchanged():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", _cube_stl_bytes(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "10",
            "operations": "[]",
        },
    )

    assert response.status_code == 200
    part_cost = response.json()["part_cost"]
    assert part_cost["total_additional_cost"] == 0
    assert part_cost["total_part_cost"] == pytest.approx(part_cost["total_material_cost"])


def test_cost_cad_part_endpoint_rejects_invalid_scrap_rate():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", _cube_stl_bytes(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "10",
            "operations": "[]",
            "scrap_rate_percent": "150.0",
        },
    )

    # This endpoint takes multipart form data, so validation surfaces as 400.
    assert response.status_code == 400
