"""Non-machining route stages priced from the rate set's stage rates.

What is checked: a stage rate states its basis and nothing defaults it; every
stage of a decided route is priced or the estimate is refused with each
stage's reason; a per-kg stage is charged on a mass a person entered, never on
the CAD mass; a route that starts by casting, forging or buying the part does
not charge the material twice; the quotation still partitions the stored
total; a comparison attributes a stage change rather than calling it
unexplained; and an estimate without stages is exactly what it was.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cad_costing_agent.main import app
from cad_costing_agent.process_classification import STAGES
from cad_costing_agent.rate_sets import RateSet, StageRate
from cad_costing_agent.stage_costing import (
    MATERIAL_SUPPLYING_STAGES,
    StageInput,
    material_supplying_stage,
    price_stages,
)
from tests.test_process_classification import FORM, committed_plan, decide, step, suggested

client = TestClient(app)
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"

NAME = "block_with_counterbore"
CASTING = {"unit_basis": "per_kg", "unit_rate": 120.0, "batch_charge": 15000.0,
           "note": "Foundry quote, includes metal"}
STAGE_RATES = {
    "casting": CASTING,
    "purchase": {"unit_basis": "per_piece", "unit_rate": 900.0},
    "sheet_cutting": {"unit_basis": "per_piece", "unit_rate": 35.0},
    "bending": {"unit_basis": "per_piece", "unit_rate": 20.0, "batch_charge": 500.0},
}


def rate_set(**stage_rates) -> RateSet:
    return RateSet(
        rate_set_id="client-shop",
        name="Client shop",
        version="1.0.0",
        materials={"steel": {"name": "Steel", "cost_per_kg": 75.0, "density_kg_m3": 7850}},
        stage_rates=stage_rates,
    )


# --- 1. a stage rate states its basis ----------------------------------------


def test_a_unit_rate_needs_its_basis_and_a_basis_needs_its_rate():
    with pytest.raises(ValidationError):
        StageRate(unit_rate=10.0)
    with pytest.raises(ValidationError):
        StageRate(unit_basis="per_kg")


def test_a_stage_rate_with_nothing_to_charge_is_refused():
    with pytest.raises(ValidationError):
        StageRate()


def test_a_batch_charge_alone_is_a_stage_rate():
    assert StageRate(batch_charge=2500.0).unit_rate is None


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_a_unit_rate_must_be_positive_and_finite(bad):
    with pytest.raises(ValidationError):
        StageRate(unit_basis="per_piece", unit_rate=bad)


# --- 2. pricing ---------------------------------------------------------------


def test_a_per_kg_stage_is_rate_times_entered_mass_times_quantity_plus_batch():
    pricing = price_stages(
        [StageInput(stage="casting", label="Casting", mass_kg=2.5)],
        rate_set(casting=CASTING),
        quantity=5,
    )
    line = pricing.lines[0]
    assert pricing.priceable
    assert line.unit_cost_per_part == pytest.approx(300.0)
    assert line.run_cost == pytest.approx(1500.0)
    assert line.batch_charge == pytest.approx(15000.0)
    assert line.total_cost == pytest.approx(16500.0)
    assert pricing.total == pytest.approx(16500.0)
    assert line.rate_note == "Foundry quote, includes metal"


def test_a_per_piece_stage_ignores_mass():
    pricing = price_stages(
        [StageInput(stage="purchase", label="Purchased complete", mass_kg=9.0)],
        rate_set(purchase={"unit_basis": "per_piece", "unit_rate": 900.0}),
        quantity=5,
    )
    assert pricing.lines[0].total_cost == pytest.approx(4500.0)
    assert pricing.lines[0].mass_kg is None


def test_a_per_kg_stage_without_a_mass_is_refused_not_defaulted():
    pricing = price_stages(
        [StageInput(stage="casting", label="Casting")], rate_set(casting=CASTING), quantity=5
    )
    assert not pricing.priceable
    assert pricing.problems[0].code == "needs_mass"
    assert "not used in its place" in pricing.problems[0].detail
    assert pricing.lines == [] and pricing.total == 0.0


def test_a_blank_lighter_than_the_finished_part_is_refused():
    pricing = price_stages(
        [StageInput(stage="casting", label="Casting", mass_kg=1.0)],
        rate_set(casting=CASTING),
        quantity=1,
        material_stage="casting",
        finished_mass_kg=1.4,
    )
    assert pricing.problems[0].code == "needs_mass"
    assert "lighter" in pricing.problems[0].detail


def test_a_finishing_mass_below_the_part_is_not_a_blank_and_is_accepted():
    pricing = price_stages(
        [StageInput(stage="finishing", label="Finishing", mass_kg=1.0)],
        rate_set(finishing={"unit_basis": "per_kg", "unit_rate": 40.0}),
        quantity=1,
        finished_mass_kg=1.4,
    )
    assert pricing.priceable


def test_every_stage_is_priced_or_none_is():
    pricing = price_stages(
        [
            StageInput(stage="casting", label="Casting", mass_kg=2.0),
            StageInput(stage="finishing", label="Finishing"),
        ],
        rate_set(casting=CASTING),
        quantity=2,
    )
    assert [problem.code for problem in pricing.problems] == ["no_stage_rate"]
    assert pricing.lines == [] and pricing.total == 0.0


def test_no_rate_set_is_its_own_reason():
    pricing = price_stages([StageInput(stage="casting", label="Casting")], None, quantity=1)
    assert pricing.problems[0].code == "no_rate_set"


@pytest.mark.parametrize(
    "route, expected",
    [
        (["casting", "milling", "drilling"], "casting"),
        (["forging", "milling"], "forging"),
        (["purchase"], "purchase"),
        (["milling", "drilling"], None),
        (["sheet_cutting", "bending"], None),
        (["cutting", "welding", "finishing"], None),
        ([], None),
    ],
)
def test_only_a_route_that_starts_with_a_material_supplying_stage_carries_the_material(
    route, expected
):
    assert material_supplying_stage(route) == expected


def test_the_stage_vocabularies_agree():
    non_machining = {stage for stage, (_, process) in STAGES.items() if process is None}
    assert MATERIAL_SUPPLYING_STAGES <= non_machining


# --- 3. structure -------------------------------------------------------------


def _imports(module: str) -> set[str]:
    tree = ast.parse((PACKAGE / module).read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module.startswith("cad_costing_agent")
    }


def test_stage_costing_imports_only_money_and_rate_sets():
    assert _imports("stage_costing.py") == {
        "cad_costing_agent.money",
        "cad_costing_agent.rate_sets",
    }


@pytest.mark.parametrize("module", ["part_cost.py", "cad_part_cost.py", "rate_sets.py"])
def test_costing_modules_still_do_not_reach_the_classifier(module):
    assert "cad_costing_agent.process_classification" not in _imports(module)


# --- 4. publishing stage rates ------------------------------------------------


def publish(stage_rates: dict, *, rate_set_id: str = "client-shop", version: str = "1.0.0"):
    base = client.get("/rates/poc-default").json()
    body = {
        "rate_set_id": rate_set_id,
        "name": "Client shop",
        "version": version,
        "currency": "INR",
        "is_illustrative": False,
        "materials": base["materials"],
        "operations": base["operations"],
        "additional_costs": {"overhead_rate_percent": 0, "scrap_rate_percent": 0,
                             "tooling_cost_per_batch": 0},
        "stage_rates": stage_rates,
    }
    return client.post("/rates", json=body)


def test_stage_rates_are_published_and_read_back():
    response = publish(STAGE_RATES)
    assert response.status_code == 201, response.text
    stored = client.get("/rates/client-shop").json()["stage_rates"]
    assert stored["casting"]["unit_rate"] == 120.0
    assert stored["casting"]["batch_charge"] == 15000.0
    listing = {row["rate_set_id"]: row for row in client.get("/rates").json()}
    assert listing["client-shop"]["stage_rate_count"] == 4


def test_a_seeded_rate_set_has_no_stage_rates():
    assert client.get("/rates/poc-default").json()["stage_rates"] == {}


@pytest.mark.parametrize(
    "key, fragment", [("laser_unicorn", "Unknown route stage"), ("milling", "machining stage")]
)
def test_a_stage_rate_for_a_stage_that_is_not_one_is_refused(key, fragment):
    response = publish({key: {"unit_basis": "per_piece", "unit_rate": 5.0}})
    assert response.status_code == 422
    assert fragment in response.json()["detail"]
    assert client.get("/rates/client-shop").status_code == 404


def test_validate_reports_a_bad_stage_key_without_publishing():
    base = client.get("/rates/poc-default").json()
    body = {**base, "rate_set_id": "client-shop", "version": "1.0.0",
            "stage_rates": {"milling": {"unit_basis": "per_piece", "unit_rate": 5.0}}}
    verdict = client.post("/rates/validate", json=body).json()
    assert verdict["valid"] is False
    assert any("machining stage" in message for message in verdict["messages"])


# --- 5. costing a decided route -----------------------------------------------


def classification_for(route_id: str, name: str = NAME) -> dict:
    response = decide(suggested(name)["process_classification"], "edited", route_id)
    assert response.status_code == 200, response.text
    return response.json()


def cost(name=NAME, *, classification, stage_inputs=None, rate_set_id="client-shop",
         plan=True, raw_stock=None):
    data = {**FORM, "rate_set_id": rate_set_id,
            "process_classification": json.dumps(classification)}
    if plan:
        data["manufacturing_plan"] = committed_plan(name).model_dump_json()
    if stage_inputs is not None:
        data["stage_inputs"] = json.dumps(stage_inputs)
    if raw_stock is not None:
        data["raw_stock"] = json.dumps(raw_stock)
    return client.post(
        "/cost/cad-part",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
        data=data,
    )


def estimate_count() -> int:
    return client.get("/estimates").json()["total"]


@pytest.fixture
def client_rates():
    assert publish(STAGE_RATES).status_code == 201


def test_a_decided_casting_route_is_priced_and_the_material_is_not_charged_twice(client_rates):
    response = cost(classification=classification_for("casting_machining"),
                    stage_inputs={"casting": {"mass_kg": 3.0}})

    assert response.status_code == 200, response.text
    body = response.json()
    part_cost = body["part_cost"]
    [line] = part_cost["stage_costs"]
    assert line["stage"] == "casting"
    assert line["mass_kg"] == 3.0
    assert line["supplies_material"] is True
    # 120/kg x 3.0 kg x 5 off + 15,000 pattern.
    assert line["total_cost"] == pytest.approx(16800.0)
    assert part_cost["total_stage_cost"] == pytest.approx(16800.0)
    assert part_cost["material_charged"] is False
    assert part_cost["total_material_cost"] == 0.0
    assert "Casting rate includes the material" in part_cost["material_note"]
    # The finished part's own mass is still what `mass` reports.
    assert body["mass"]["mass_kg"] < 3.0
    assert part_cost["direct_cost_base"] == pytest.approx(
        part_cost["total_operation_cost"] + 16800.0, abs=0.01
    )
    assert part_cost["total_part_cost"] == pytest.approx(part_cost["direct_cost_base"], abs=0.01)


def test_the_entered_mass_is_recorded_with_the_estimate(client_rates):
    estimate_id = cost(classification=classification_for("casting_machining"),
                       stage_inputs={"casting": {"mass_kg": 3.0}}).json()["estimate_id"]
    stored = client.get(f"/estimates/{estimate_id}").json()
    assert stored["request_snapshot"]["stage_inputs"] == {"casting": {"mass_kg": 3.0}}
    assert stored["total_material_cost"] == 0.0


def test_a_casting_route_without_the_blank_mass_is_refused_and_nothing_is_stored(client_rates):
    before = estimate_count()
    response = cost(classification=classification_for("casting_machining"))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "route_stages_not_priceable"
    assert [stage["code"] for stage in detail["stages"]] == ["needs_mass"]
    assert estimate_count() == before


def test_a_blank_lighter_than_the_part_is_refused_at_the_endpoint(client_rates):
    response = cost(classification=classification_for("casting_machining"),
                    stage_inputs={"casting": {"mass_kg": 0.01}})
    assert response.status_code == 422
    assert "lighter" in response.json()["detail"]["message"]


def test_a_rate_set_without_the_stage_rate_refuses_with_that_reason():
    response = cost(classification=classification_for("casting_machining"),
                    stage_inputs={"casting": {"mass_kg": 3.0}}, rate_set_id="poc-default")
    assert response.status_code == 422
    assert response.json()["detail"]["stages"][0]["code"] == "no_stage_rate"


def test_a_purchased_part_is_priced_per_piece_with_nothing_machined(client_rates):
    body = cost(classification=classification_for("purchased")).json()
    part_cost = body["part_cost"]
    assert part_cost["total_stage_cost"] == pytest.approx(4500.0)
    assert part_cost["total_operation_cost"] == 0.0
    assert part_cost["material_charged"] is False
    assert part_cost["total_part_cost"] == pytest.approx(4500.0)


def test_a_sheet_route_prices_both_stages_and_still_charges_the_material(client_rates):
    body = cost(classification=classification_for("sheet_cutting_bending")).json()
    part_cost = body["part_cost"]
    assert [line["stage"] for line in part_cost["stage_costs"]] == ["sheet_cutting", "bending"]
    # 35 x 5 + (20 x 5 + 500)
    assert part_cost["total_stage_cost"] == pytest.approx(775.0)
    assert part_cost["material_charged"] is True
    assert part_cost["total_material_cost"] > 0


def test_an_unreviewed_recommendation_needing_stage_rates_is_still_refused(client_rates):
    classification = suggested("bent_sheet_bracket")["process_classification"]
    response = cost("bent_sheet_bracket", classification=classification, plan=False)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "process_route_not_costable"


def test_a_confirmed_bar_contradicts_a_casting_route(client_rates):
    stock = suggested("stepped_shaft")["raw_stock"]
    confirmed = client.post("/cad/stock/decide", json={"stock": stock, "action": "confirm"})
    assert confirmed.status_code == 200, confirmed.text
    response = cost("stepped_shaft", classification=classification_for("casting_machining",
                    "stepped_shaft"), stage_inputs={"casting": {"mass_kg": 9.0}},
                    raw_stock=confirmed.json(), plan=False)
    assert response.status_code == 400
    assert "rate includes the material" in response.json()["detail"]


def test_a_machining_route_with_a_stage_rate_set_has_no_stages(client_rates):
    body = cost(classification=classification_for("milling_drilling")).json()
    part_cost = body["part_cost"]
    assert part_cost["stage_costs"] == []
    assert part_cost["total_stage_cost"] == 0.0
    assert part_cost["material_charged"] is True


def test_pricing_a_route_is_deterministic(client_rates):
    classification = classification_for("casting_machining")
    first = cost(classification=classification, stage_inputs={"casting": {"mass_kg": 3.0}}).json()
    second = cost(classification=classification, stage_inputs={"casting": {"mass_kg": 3.0}}).json()
    # Every call persists its own estimate, so only the id may differ.
    first["part_cost"].pop("estimate_id")
    second["part_cost"].pop("estimate_id")
    assert first["part_cost"] == second["part_cost"]


# --- 6. the documents built on the stored estimate ----------------------------


def test_the_quotation_lines_still_partition_the_stored_total(client_rates):
    estimate_id = cost(classification=classification_for("casting_machining"),
                       stage_inputs={"casting": {"mass_kg": 3.0}}).json()["estimate_id"]
    quote = client.get(f"/estimates/{estimate_id}/quotation").json()
    totals = quote["totals"]
    lines = {line["category"]: line for line in totals["lines"]}
    assert lines["route_stages"]["amount"] == pytest.approx(16800.0)
    assert lines["material"]["amount"] == 0.0
    assert "includes the material" in lines["material"]["detail"]
    assert not totals["reconciliation"]


def test_a_quotation_without_stages_has_no_stage_line():
    estimate_id = cost(classification=classification_for("milling_drilling"),
                       rate_set_id="poc-default").json()["estimate_id"]
    lines = client.get(f"/estimates/{estimate_id}/quotation").json()["totals"]["lines"]
    assert "route_stages" not in {line["category"] for line in lines}


def test_a_comparison_attributes_a_stage_change_and_leaves_nothing_unexplained(client_rates):
    classification = classification_for("casting_machining")
    lighter = cost(classification=classification,
                   stage_inputs={"casting": {"mass_kg": 3.0}}).json()["estimate_id"]
    heavier = cost(classification=classification,
                   stage_inputs={"casting": {"mass_kg": 4.0}}).json()["estimate_id"]
    comparison = client.get(
        f"/estimates/compare?baseline={lighter}&comparison={heavier}"
    ).json()
    impacts = {impact["category"]: impact for impact in comparison["impacts"]}
    assert impacts["route_stage"]["delta"] == pytest.approx(600.0)  # 120 x 1 kg x 5
    assert "unexplained" not in impacts
    assert "material_rate" not in impacts and "material_quantity" not in impacts
