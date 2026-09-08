from fastapi.testclient import TestClient

from cad_costing_agent.main import app

client = TestClient(app)

VALID_PART = {
    "part_id": "P1",
    "part_name": "Bracket",
    "material": "Steel",
    "mass_kg": 2.0,
    "quantity": 3,
}


def test_cost_material_endpoint_returns_calculation():
    response = client.post("/cost/material", json=VALID_PART)

    assert response.status_code == 200
    body = response.json()
    assert body["part_id"] == "P1"
    assert body["material"]["name"] == "Steel"
    # Money is presented at 2 decimal places.
    assert body["material_cost"] == 1.60
    assert body["total_material_cost"] == 4.80
    assert body["is_illustrative_rate"] is True
    assert "formula" in body


def test_cost_material_endpoint_unknown_material():
    response = client.post("/cost/material", json={**VALID_PART, "material": "Unobtainium"})

    assert response.status_code == 400
    assert "Unknown material" in response.json()["detail"]


def test_cost_material_endpoint_negative_mass():
    response = client.post("/cost/material", json={**VALID_PART, "mass_kg": -5})

    assert response.status_code == 422


def test_cost_material_endpoint_negative_quantity():
    response = client.post("/cost/material", json={**VALID_PART, "quantity": -1})

    assert response.status_code == 422
