import json

import pytest
import trimesh
from fastapi.testclient import TestClient

from cad_costing_agent.main import app

client = TestClient(app)

WORKED_PART = {
    "part_id": "P100",
    "part_name": "Flange",
    "material": "Steel",
    "mass_kg": 2.5,
    "quantity": 4,
}
WORKED_REQUIREMENT = {
    "operation_id": "OP1",
    "operation_name": "Milling",
    "process_type": "Milling",
    "setup_time_minutes": 30.0,
    "cycle_time_minutes": 3.0,
    "quantity": 4,
}


def _cube_stl_bytes() -> bytes:
    return trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")


# --- discovery --------------------------------------------------------------


def test_list_rate_sets():
    response = client.get("/rates")

    assert response.status_code == 200
    body = response.json()
    identities = {(row["rate_set_id"], row["version"]) for row in body}

    assert ("poc-default", "1.0.0") in identities
    assert ("poc-machine-shop", "1.0.0") in identities
    assert ("poc-machine-shop", "2.0.0") in identities
    assert all(row["is_illustrative"] for row in body)


def test_get_rate_set_returns_latest_version_by_default():
    response = client.get("/rates/poc-machine-shop")

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "2.0.0"
    assert body["materials"]["steel"]["cost_per_kg"] == 0.95


def test_get_rate_set_with_pinned_version():
    response = client.get("/rates/poc-machine-shop", params={"version": "1.0.0"})

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "1.0.0"
    assert body["materials"]["steel"]["cost_per_kg"] == 0.80
    assert body["operations"]["milling"]["machine_rate_per_hour"] == 1200.0
    assert body["additional_costs"]["overhead_rate_percent"] == 10.0


def test_get_unknown_rate_set_returns_404():
    response = client.get("/rates/does-not-exist")

    assert response.status_code == 404
    assert "Unknown rate set" in response.json()["detail"]


def test_get_unknown_version_returns_404():
    response = client.get("/rates/poc-machine-shop", params={"version": "9.9.9"})

    assert response.status_code == 404
    assert "no version" in response.json()["detail"]


# --- /cost/part with a rate set --------------------------------------------


def test_cost_part_with_rate_set_reproduces_the_worked_example():
    response = client.post(
        "/cost/part",
        json={
            "part": WORKED_PART,
            "operation_requirements": [WORKED_REQUIREMENT],
            "rate_set_id": "poc-machine-shop",
            "rate_set_version": "1.0.0",
        },
    )

    assert response.status_code == 200
    body = response.json()

    assert body["total_material_cost"] == pytest.approx(8.00)
    assert body["total_operation_cost"] == pytest.approx(960.00)
    assert body["direct_cost_base"] == pytest.approx(968.00)
    assert body["total_additional_cost"] == pytest.approx(395.20)
    assert body["total_part_cost"] == pytest.approx(1363.20)


def test_cost_part_response_carries_rate_set_provenance():
    response = client.post(
        "/cost/part",
        json={
            "part": WORKED_PART,
            "operation_requirements": [WORKED_REQUIREMENT],
            "rate_set_id": "poc-machine-shop",
            "rate_set_version": "1.0.0",
        },
    )

    body = response.json()
    assert body["rate_set"] == {
        "rate_set_id": "poc-machine-shop",
        "name": "POC Machine Shop",
        "version": "1.0.0",
        "currency": "INR",
        "is_illustrative": True,
    }
    assert body["rate_precedence"]


def test_cost_part_without_a_rate_set_reports_none():
    response = client.post("/cost/part", json={"part": WORKED_PART})

    assert response.status_code == 200
    assert response.json()["rate_set"] is None


def test_cost_part_unknown_rate_set_returns_400():
    response = client.post(
        "/cost/part", json={"part": WORKED_PART, "rate_set_id": "does-not-exist"}
    )

    assert response.status_code == 400
    assert "Unknown rate set" in response.json()["detail"]


def test_cost_part_requirements_without_rate_set_returns_400():
    response = client.post(
        "/cost/part",
        json={"part": WORKED_PART, "operation_requirements": [WORKED_REQUIREMENT]},
    )

    assert response.status_code == 400


def test_cost_part_malformed_requirement_returns_422():
    response = client.post(
        "/cost/part",
        json={
            "part": WORKED_PART,
            "operation_requirements": [{**WORKED_REQUIREMENT, "cycle_time_minutes": -3.0}],
            "rate_set_id": "poc-machine-shop",
        },
    )

    assert response.status_code == 422


def test_cost_part_unpriced_process_type_returns_400():
    response = client.post(
        "/cost/part",
        json={
            "part": WORKED_PART,
            "operation_requirements": [{**WORKED_REQUIREMENT, "process_type": "Electroplating"}],
            "rate_set_id": "poc-machine-shop",
            "rate_set_version": "1.0.0",
        },
    )

    assert response.status_code == 400
    assert "no rate for process type" in response.json()["detail"]


def test_cost_part_explicit_additional_costs_override_the_rate_set():
    response = client.post(
        "/cost/part",
        json={
            "part": WORKED_PART,
            "operation_requirements": [WORKED_REQUIREMENT],
            "rate_set_id": "poc-machine-shop",
            "rate_set_version": "1.0.0",
            "additional_costs": {"overhead_rate_percent": 0.0},
        },
    )

    body = response.json()
    assert body["total_additional_cost"] == 0
    assert body["total_part_cost"] == pytest.approx(968.00)


def test_cost_part_legacy_explicit_rate_request_still_works():
    """Backward compatibility: the Step 8 request shape, unchanged."""
    response = client.post(
        "/cost/part",
        json={
            "part": WORKED_PART,
            "operations": [
                {
                    **WORKED_REQUIREMENT,
                    "machine_rate_per_hour": 1200.0,
                    "labor_rate_per_hour": 600.0,
                }
            ],
            "additional_costs": {
                "overhead_rate_percent": 10.0,
                "scrap_rate_percent": 5.0,
                "tooling_cost_per_batch": 250.0,
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["rate_set"] is None
    assert body["total_part_cost"] == pytest.approx(1363.20)


# --- /cost/cad-part with a rate set ----------------------------------------


def test_cost_cad_part_with_rate_set():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", _cube_stl_bytes(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "4",
            "operation_requirements": json.dumps([WORKED_REQUIREMENT]),
            "rate_set_id": "poc-machine-shop",
            "rate_set_version": "1.0.0",
        },
    )

    assert response.status_code == 200
    part_cost = response.json()["part_cost"]

    assert part_cost["rate_set"]["rate_set_id"] == "poc-machine-shop"
    assert part_cost["rate_set"]["version"] == "1.0.0"
    assert part_cost["operations"][0]["machine_rate_per_hour"] == 1200.0
    # The rate set's additional costs apply because none were given on the form.
    assert part_cost["additional_costs"]["overhead_rate_percent"] == 10.0
    assert part_cost["total_part_cost"] == pytest.approx(
        part_cost["direct_cost_base"] + part_cost["total_additional_cost"]
    )


def test_cost_cad_part_form_additional_costs_override_the_rate_set():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", _cube_stl_bytes(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "1",
            "rate_set_id": "poc-machine-shop",
            "rate_set_version": "1.0.0",
            "overhead_rate_percent": "2.0",
        },
    )

    assert response.status_code == 200
    additional = response.json()["part_cost"]["additional_costs"]
    assert additional["overhead_rate_percent"] == 2.0
    # Wholesale replacement: the rate set's scrap and tooling do not leak through.
    assert additional["scrap_rate_percent"] == 0.0
    assert additional["tooling_cost_per_batch"] == 0.0


def test_cost_cad_part_unknown_rate_set_returns_400():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", _cube_stl_bytes(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "1",
            "rate_set_id": "does-not-exist",
        },
    )

    assert response.status_code == 400
    assert "Unknown rate set" in response.json()["detail"]


def test_cost_cad_part_malformed_requirements_json_returns_400():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("cube.stl", _cube_stl_bytes(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P001",
            "part_name": "Disc",
            "quantity": "1",
            "rate_set_id": "poc-machine-shop",
            "operation_requirements": "{not json",
        },
    )

    assert response.status_code == 400


def test_cost_cad_part_without_rate_set_is_unchanged():
    """Regression: the Step 8 form shape still produces the same numbers."""
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
    assert part_cost["rate_set"] is None
    assert part_cost["total_additional_cost"] == 0
    assert part_cost["total_part_cost"] == 0.38  # 0.3768 presented at 2dp
