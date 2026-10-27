"""Turned parts: the bar they are cut from, facing on the lathe, passes from the bar.

Three things changed for a body of revolution, and each is checked for what it
does and for what it must not do:

- a **bar** is proposed from the part's largest diameter and length; material
  is costed from it only once a person confirms it, and the finished part's
  own mass is never replaced;
- **end faces and shoulders** are suggested as turning (facing), not milling —
  under a turning route they used to fall out of the cost entirely;
- **turning passes** are worked out from the bar down to each diameter when a
  bar is given, and from the old fixed allowance when it is not.

Fixtures are the generated STEP parts in `tests/step_fixtures.py`.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.features import recognize_features_from_bytes
from cad_costing_agent.features.rotational import rotational_form
from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import build_plan
from cad_costing_agent.operation_suggestions import suggest_operations
from cad_costing_agent.operation_time import TimeStatus, estimate_operation_time
from cad_costing_agent.raw_stock import propose_bar_stock, record_stock_decision
from tests import step_fixtures
from tests.step_fixtures import PROCESS_FIXTURES

client = TestClient(app)
SHAFT = "stepped_shaft"  # Ø30 × 40 then Ø20 × 30 on one axis


@lru_cache(maxsize=None)
def step(name: str) -> bytes:
    builder = PROCESS_FIXTURES.get(name) or getattr(step_fixtures, name)
    return builder()


def features(name: str):
    return list(recognize_features_from_bytes(step(name), f"{name}.step").features)


# --- the rotational extent ------------------------------------------------------


def test_a_stepped_shaft_is_measured_along_its_axis():
    form = rotational_form(features(SHAFT))
    assert form.observed is True
    assert form.max_diameter_mm == 30.0
    assert form.length_mm == 70.0


def test_a_block_has_no_rotational_extent():
    form = rotational_form(features("plate_with_boss_and_bores"))
    assert form.observed is False
    assert form.length_mm is None


# --- the bar proposal --------------------------------------------------------------


def test_a_bar_is_proposed_from_the_largest_diameter_and_the_length():
    stock = propose_bar_stock(features(SHAFT))
    # Ø30 + 2 × 2.5 = Ø35, already on a 5 mm step; 70 + 2 × 2 + 3 = 77.
    assert (stock.system_diameter_mm, stock.system_length_mm) == (35.0, 77.0)
    assert stock.volume_mm3 == pytest.approx(math.pi * 17.5**2 * 77, rel=1e-6)
    assert stock.confirmed is False
    assert any("illustrative" in line for line in stock.basis)


def test_the_bar_diameter_rounds_up_to_the_size_step():
    stock = propose_bar_stock(features("stepped_shaft_with_axial_hole"))
    assert stock.system_diameter_mm % stock.rule.bar_size_step_mm == 0
    assert stock.system_diameter_mm >= stock.part_max_diameter_mm + 2 * stock.rule.radial_allowance_mm


@pytest.mark.parametrize("name", ["plain_block", "plate_with_boss_and_bores", "bent_sheet_bracket"])
def test_no_bar_is_proposed_for_a_part_that_is_not_turned(name):
    assert propose_bar_stock(features(name)) is None


def test_confirming_keeps_the_proposed_size():
    stock = record_stock_decision(propose_bar_stock(features(SHAFT)), "confirm")
    assert stock.confirmed and stock.source == "system"
    assert stock.diameter_mm == 35.0


def test_entering_a_bar_keeps_the_proposal_beside_it():
    proposed = propose_bar_stock(features(SHAFT))
    stock = record_stock_decision(proposed, "edit", diameter_mm=40.0)
    assert stock.confirmed and stock.source == "user"
    assert (stock.diameter_mm, stock.length_mm) == (40.0, 77.0)
    assert stock.system_diameter_mm == 35.0


def test_entering_the_proposed_size_is_a_confirmation_not_a_correction():
    stock = record_stock_decision(propose_bar_stock(features(SHAFT)), "edit", diameter_mm=35.0)
    assert stock.source == "system" and stock.user_diameter_mm is None


def test_a_bar_smaller_than_the_part_is_refused():
    with pytest.raises(ValueError, match="smaller than the part"):
        record_stock_decision(propose_bar_stock(features(SHAFT)), "edit", diameter_mm=25.0)


def test_withdrawing_returns_to_the_proposal_unconfirmed():
    edited = record_stock_decision(propose_bar_stock(features(SHAFT)), "edit", length_mm=90.0)
    withdrawn = record_stock_decision(edited, "withdraw")
    assert not withdrawn.confirmed and withdrawn.source == "system"


# --- facing on the lathe -------------------------------------------------------------


def test_end_faces_of_a_turned_part_are_suggested_as_facing():
    result = suggest_operations(recognize_features_from_bytes(step(SHAFT), "s.step"))
    faces = [s for s in result.suggestions if s.feature_type == "plane"]
    assert faces
    assert {s.operation_type for s in faces} == {"turning"}
    assert {s.rule_id for s in faces} == {"turned_part_face_to_facing"}
    assert all(s.operation_name.startswith("Face ") for s in faces)
    assert all("milling" in s.alternatives for s in faces)


def test_faces_of_a_part_that_is_not_turned_are_still_milled():
    result = suggest_operations(recognize_features_from_bytes(step("plate_with_boss_and_bores"), "b.step"))
    faces = [s for s in result.suggestions if s.feature_type == "plane"]
    assert faces and {s.operation_type for s in faces} == {"milling"}


def test_a_face_is_timed_as_facing_across_its_radius():
    estimate = estimate_operation_time(
        operation_type="turning", feature_type="plane", dimensions={"length_mm": 40.0, "width_mm": 40.0}
    )
    assert estimate.method == "facing_time_baseline_v1"
    assert estimate.cycle_time_minutes > 0
    assert any("radius" in line for line in estimate.evidence)


def test_a_turned_part_plan_keeps_its_faces_under_a_turning_route():
    """Faces used to be milling, and fell out of a turning route's cost entirely."""
    suggested = client.post(
        "/cad/operations/suggest", files={"file": ("s.step", step(SHAFT), "application/octet-stream")}
    ).json()
    accepted = client.post(
        "/cad/process/decide",
        json={"classification": suggested["process_classification"], "action": "accepted"},
    ).json()
    assert accepted["plan_processes"] == ["turning"]
    plan = build_plan(suggest_operations(recognize_features_from_bytes(step(SHAFT), "s.step")).suggestions)
    routed = plan.model_copy(update={"route_processes": accepted["plan_processes"]})
    assert routed.out_of_route_operations() == []


# --- passes from the bar ---------------------------------------------------------------


def test_turning_passes_come_from_the_bar_when_one_is_given():
    estimate = estimate_operation_time(
        operation_type="turning", dimensions={"diameter_mm": 20.0, "length_mm": 40.0}, stock_diameter_mm=45.0
    )
    # (45 − 20) / 2 = 12.5 mm radial at 1.5 mm a pass: 9 passes.
    assert estimate.method == "turning_time_from_bar_v1"
    assert estimate.parameters["passes"] == 9.0
    assert estimate.parameters["turning_stock_diameter_mm"] == 45.0


def test_without_a_bar_the_old_allowance_stands():
    estimate = estimate_operation_time(operation_type="turning", dimensions={"diameter_mm": 20.0, "length_mm": 40.0})
    assert estimate.method == "turning_time_baseline_v1"
    assert estimate.parameters["passes"] == 2.0


def test_a_bar_smaller_than_the_diameter_gives_no_time():
    estimate = estimate_operation_time(
        operation_type="turning", dimensions={"diameter_mm": 50.0, "length_mm": 40.0}, stock_diameter_mm=45.0
    )
    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT


def test_the_estimate_endpoint_takes_the_bar():
    response = client.post(
        "/operations/estimate-time",
        json={
            "stock_diameter_mm": 45,
            "operations": [{"operation_type": "turning", "dimensions": {"diameter_mm": 20, "length_mm": 40}}],
        },
    ).json()
    assert response["estimates"][0]["estimate"]["method"] == "turning_time_from_bar_v1"


# --- material from the bar ----------------------------------------------------------------

FORM = {
    "material": "Steel",
    "source_unit": "mm",
    "part_id": "P-TURN",
    "part_name": "Turned shaft",
    "quantity": "10",
    "rate_set_id": "poc-default",
}


def cost(stock=None):
    data = dict(FORM)
    if stock is not None:
        data["raw_stock"] = stock if isinstance(stock, str) else json.dumps(stock)
    return client.post("/cost/cad-part", files={"file": ("s.step", step(SHAFT), "application/octet-stream")}, data=data)


def proposed() -> dict:
    return client.post(
        "/cad/operations/suggest", files={"file": ("s.step", step(SHAFT), "application/octet-stream")}
    ).json()["raw_stock"]


def test_the_suggest_response_carries_the_bar_for_a_turned_part_only():
    assert proposed()["system_diameter_mm"] == 35.0
    block = client.post(
        "/cad/operations/suggest",
        files={"file": ("b.step", step("plate_with_boss_and_bores"), "application/octet-stream")},
    ).json()
    assert block["raw_stock"] is None


def test_an_unconfirmed_bar_changes_nothing():
    without, with_proposal = cost(), cost(proposed())
    assert without.status_code == with_proposal.status_code == 200
    assert with_proposal.json()["part_cost"]["total_material_cost"] == without.json()["part_cost"]["total_material_cost"]
    assert with_proposal.json()["material_basis"] == "finished_part"


def test_a_confirmed_bar_costs_material_from_the_bar_and_keeps_the_parts_own_mass():
    confirmed = client.post("/cad/stock/decide", json={"stock": proposed(), "action": "confirm"}).json()
    plain, from_bar = cost().json(), cost(confirmed).json()

    assert from_bar["material_basis"] == "bar_stock"
    assert from_bar["mass"]["mass_kg"] == plain["mass"]["mass_kg"]  # the part is unchanged
    ratio = confirmed["volume_mm3"] / plain["geometry"]["volume"]
    assert from_bar["stock_mass"]["mass_kg"] == pytest.approx(plain["mass"]["mass_kg"] * ratio, rel=1e-9)
    assert from_bar["part_cost"]["total_material_cost"] == pytest.approx(
        plain["part_cost"]["total_material_cost"] * ratio, abs=0.01
    )

    stored = client.get(f"/estimates/{from_bar['estimate_id']}").json()
    assert stored["request_snapshot"]["raw_stock"]["confirmed"] is True
    assert "raw_stock" not in client.get(f"/estimates/{plain['estimate_id']}").json()["request_snapshot"]


def test_a_confirmed_bar_smaller_than_the_part_is_refused():
    stock = proposed()
    stock.update({"confirmed": True, "user_diameter_mm": 10.0})
    response = cost(stock)
    assert response.status_code == 400
    assert "smaller than the part" in response.json()["detail"]


def test_the_stock_endpoint_refuses_an_unknown_action():
    response = client.post("/cad/stock/decide", json={"stock": proposed(), "action": "guess"})
    assert response.status_code == 400


def test_a_malformed_bar_is_refused():
    response = cost("{not json")
    assert response.status_code == 400
    assert "raw_stock" in response.json()["detail"]


# --- the part's own mass is never replaced by the bar's -------------------------


def test_the_assumptions_name_the_bar_beside_the_parts_own_mass():
    confirmed = client.post("/cad/stock/decide", json={"stock": proposed(), "action": "confirm"}).json()
    from_bar = cost(confirmed).json()
    assumptions = client.get(f"/estimates/{from_bar['estimate_id']}/assumptions").json()

    assert assumptions["mass_kg"] == from_bar["mass"]["mass_kg"]  # the finished part
    assert assumptions["material_basis"] == "bar_stock"
    assert assumptions["stock_mass_kg"] == from_bar["stock_mass"]["mass_kg"]
    assert assumptions["stock_description"] == "round bar Ø35 × 77 mm"


def test_the_quotation_carries_the_bar_and_the_parts_mass_separately():
    confirmed = client.post("/cad/stock/decide", json={"stock": proposed(), "action": "confirm"}).json()
    from_bar = cost(confirmed).json()
    quotation = client.get(f"/estimates/{from_bar['estimate_id']}/quotation").json()
    provenance = quotation["provenance"]
    assert provenance["mass_kg"] == from_bar["mass"]["mass_kg"]
    assert provenance["stock_mass_kg"] == from_bar["stock_mass"]["mass_kg"]
    assert provenance["material_basis"] == "bar_stock"


def test_a_comparison_names_the_switch_to_bar_stock():
    confirmed = client.post("/cad/stock/decide", json={"stock": proposed(), "action": "confirm"}).json()
    a, b = cost().json()["estimate_id"], cost(confirmed).json()["estimate_id"]
    comparison = client.get(f"/estimates/compare?baseline={a}&comparison={b}").json()
    changed = {change["field"] for change in comparison["changes"]}
    assert "material_basis" in changed
    assert "mass_kg" not in changed  # the part itself did not change
