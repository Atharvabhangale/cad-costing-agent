import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.main import app

client = TestClient(app)

PART = {"part_id": "P001", "part_name": "Disc", "material": "Steel", "mass_kg": 2.0, "quantity": 10}


def make_operation(operation_id: str, operation_name: str, quantity: int = 10) -> dict:
    return {
        "operation_id": operation_id,
        "operation_name": operation_name,
        "process_type": operation_name,
        "machine_rate_per_hour": 1800.0,
        "labor_rate_per_hour": 500.0,
        "setup_time_minutes": 20.0,
        "cycle_time_minutes": 2.0,
        "quantity": quantity,
    }


def test_cost_part_endpoint_material_and_operations():
    response = client.post(
        "/cost/part",
        json={
            "part": PART,
            "operations": [
                make_operation("OP1", "Cutting"),
                make_operation("OP2", "Facing"),
                make_operation("OP3", "Drilling"),
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()

    assert body["part_id"] == "P001"
    assert len(body["operations"]) == 3
    assert body["total_material_cost"] == body["material"]["total_material_cost"]
    # Totals are rounded from the exact value, not from the rounded components,
    # so a sum of displayed components can differ by up to a penny per component.
    operations = body["operations"]
    expected_total_operation_cost = sum(op["total_operation_cost"] for op in operations)
    assert body["total_operation_cost"] == pytest.approx(
        expected_total_operation_cost, abs=0.01 * len(operations)
    )
    assert body["total_part_cost"] == pytest.approx(
        body["total_material_cost"] + body["total_operation_cost"], abs=0.02
    )
    assert "quantity_assumption" in body
    assert "formula" in body


def test_cost_part_endpoint_material_only():
    response = client.post("/cost/part", json={"part": PART, "operations": []})

    assert response.status_code == 200
    body = response.json()
    assert body["operations"] == []
    assert body["total_operation_cost"] == 0
    assert body["total_part_cost"] == body["total_material_cost"]


def test_cost_part_endpoint_operation_quantity_mismatch():
    response = client.post(
        "/cost/part",
        json={"part": PART, "operations": [make_operation("OP1", "Cutting", quantity=5)]},
    )

    assert response.status_code == 400
    assert "must match part quantity" in response.json()["detail"]


def test_cost_part_endpoint_invalid_part_input():
    response = client.post(
        "/cost/part",
        json={"part": {**PART, "mass_kg": -2.0}, "operations": []},
    )

    assert response.status_code == 422


def test_cost_part_endpoint_invalid_operation_input():
    response = client.post(
        "/cost/part",
        json={"part": PART, "operations": [make_operation("OP1", "Cutting") | {"cycle_time_minutes": -2.0}]},
    )

    assert response.status_code == 422
