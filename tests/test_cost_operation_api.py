from fastapi.testclient import TestClient

from cad_costing_agent.main import app
from cad_costing_agent.money import round_money

client = TestClient(app)

# Illustrative POC example: CNC Drilling
VALID_OPERATION = {
    "operation_id": "OP1",
    "operation_name": "CNC Drilling",
    "process_type": "Drilling",
    "machine_rate_per_hour": 1800.0,
    "labor_rate_per_hour": 500.0,
    "setup_time_minutes": 20.0,
    "cycle_time_minutes": 2.0,
    "quantity": 10,
}


def test_cost_operation_endpoint_returns_calculation():
    response = client.post("/cost/operation", json=VALID_OPERATION)

    assert response.status_code == 200
    body = response.json()

    expected_machine_cost = 1800.0 * (2.0 * 10 / 60)
    expected_setup_cost = 1800.0 * (20.0 / 60)
    expected_labor_cost = 500.0 * (2.0 * 10 / 60)

    assert body["operation_id"] == "OP1"
    assert body["process_type"] == "Drilling"
    # Each amount is the exact value rounded to 2 decimal places.
    assert body["machine_cost"] == round_money(expected_machine_cost)
    assert body["setup_cost"] == round_money(expected_setup_cost)
    assert body["labor_cost"] == round_money(expected_labor_cost)  # 166.666... -> 166.67
    assert body["total_operation_cost"] == round_money(
        expected_machine_cost + expected_setup_cost + expected_labor_cost
    )
    assert body["is_illustrative_rate"] is True
    assert len(body["formulas"]) == 4


def test_cost_operation_endpoint_negative_cycle_time_rejected():
    response = client.post("/cost/operation", json={**VALID_OPERATION, "cycle_time_minutes": -2.0})

    assert response.status_code == 422


def test_cost_operation_endpoint_negative_quantity_rejected():
    response = client.post("/cost/operation", json={**VALID_OPERATION, "quantity": -10})

    assert response.status_code == 422
