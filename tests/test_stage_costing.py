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
    # Route steps that come with the casting and sheet routes.
    "fettling": {"unit_basis": "per_kg", "unit_rate": 10.0},
    "deburring": {"unit_basis": "per_piece", "unit_rate": 5.0},
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


def test_a_per_kg_stage_with_no_entered_and_no_cad_mass_is_refused_not_defaulted():
    pricing = price_stages(
        [StageInput(stage="casting", label="Casting")], rate_set(casting=CASTING), quantity=5
    )
    assert not pricing.priceable
    assert pricing.problems[0].code == "needs_mass"
    assert "no mass was entered or measured" in pricing.problems[0].detail
    assert pricing.lines == [] and pricing.total == 0.0


def test_with_no_entered_mass_the_cad_finished_mass_is_used_and_labelled():
    pricing = price_stages(
        [StageInput(stage="casting", label="Casting")],
        rate_set(casting=CASTING),
        quantity=5,
        material_stage="casting",
        finished_mass_kg=2.5,
    )
    line = pricing.lines[0]
    assert line.mass_kg == 2.5
    assert line.mass_source == "cad_finished_mass"
    assert line.total_cost == pytest.approx(120.0 * 2.5 * 5 + 15000.0)
    # Made as formed: the CAD mass is the right figure, so no "heavier" warning.
    assert "heavier" not in line.mass_note


def test_a_cad_mass_on_a_route_that_machines_afterwards_says_the_blank_is_heavier():
    line = price_stages(
        [StageInput(stage="casting", label="Casting")],
        rate_set(casting=CASTING),
        quantity=1,
        material_stage="casting",
        finished_mass_kg=2.5,
        route_machines=True,
    ).lines[0]
    assert line.mass_source == "cad_finished_mass"
    assert "heavier" in line.mass_note and "does not record" in line.mass_note


def test_an_entered_mass_wins_over_the_cad_mass_and_is_labelled_entered():
    line = price_stages(
        [StageInput(stage="casting", label="Casting", mass_kg=3.0)],
        rate_set(casting=CASTING),
        quantity=1,
        material_stage="casting",
        finished_mass_kg=2.5,
    ).lines[0]
    assert line.mass_kg == 3.0
    assert line.mass_source == "entered"


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
    assert listing["client-shop"]["stage_rate_count"] == 6


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
    line, fettling = part_cost["stage_costs"]
    assert line["stage"] == "casting"
    assert line["mass_kg"] == 3.0
    assert line["supplies_material"] is True
    # 120/kg x 3.0 kg x 5 off + 15,000 pattern.
    assert line["total_cost"] == pytest.approx(16800.0)
    # Fettling works on the same 3.0 kg blank: 10/kg x 3.0 kg x 5 off.
    assert fettling["stage"] == "fettling" and fettling["mass_source"] == "blank_mass"
    assert fettling["total_cost"] == pytest.approx(150.0)
    assert part_cost["total_stage_cost"] == pytest.approx(16950.0)
    assert part_cost["material_charged"] is False
    assert part_cost["total_material_cost"] == 0.0
    assert "Casting rate includes the material" in part_cost["material_note"]
    # The finished part's own mass is still what `mass` reports.
    assert body["mass"]["mass_kg"] < 3.0
    assert part_cost["direct_cost_base"] == pytest.approx(
        part_cost["total_operation_cost"] + 16950.0, abs=0.01
    )
    assert part_cost["total_part_cost"] == pytest.approx(part_cost["direct_cost_base"], abs=0.01)


def test_the_entered_mass_is_recorded_with_the_estimate(client_rates):
    estimate_id = cost(classification=classification_for("casting_machining"),
                       stage_inputs={"casting": {"mass_kg": 3.0}}).json()["estimate_id"]
    stored = client.get(f"/estimates/{estimate_id}").json()
    assert stored["request_snapshot"]["stage_inputs"] == {"casting": {"mass_kg": 3.0}}
    assert stored["total_material_cost"] == 0.0


def test_a_casting_route_without_an_entered_mass_is_priced_on_the_cad_mass(client_rates):
    response = cost(classification=classification_for("casting_machining"))

    assert response.status_code == 200, response.text
    body = response.json()
    line = body["part_cost"]["stage_costs"][0]
    assert line["stage"] == "casting"
    assert line["mass_source"] == "cad_finished_mass"
    assert line["mass_kg"] == pytest.approx(body["mass"]["mass_kg"])
    # Casting followed by machining: the real blank is heavier, and it says so.
    assert "heavier" in line["mass_note"]


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
    assert [line["stage"] for line in part_cost["stage_costs"]] == ["sheet_cutting", "deburring", "bending"]
    # 35 x 5 + (20 x 5 + 500)
    assert part_cost["total_stage_cost"] == pytest.approx(800.0)
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
    assert lines["route_stages"]["amount"] == pytest.approx(16950.0)
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
    # Casting 120 x 1 kg x 5 and fettling, on the same blank, 10 x 1 kg x 5.
    route_stage = [i for i in comparison["impacts"] if i["category"] == "route_stage"]
    assert sum(i["delta"] for i in route_stage) == pytest.approx(650.0)
    assert "unexplained" not in impacts
    assert "material_rate" not in impacts and "material_quantity" not in impacts


# --- 7. an entered yield on the stage that supplies the material ----------------
#
# The engineer's number, never a default: the blank's mass as a percentage of the
# metal poured or billet bought. Absent, a line is exactly what it was.

# The keys of a stage cost line before yields existed. A line with no yield must
# serialize with these and no others.
LINE_KEYS_WITHOUT_A_YIELD = {
    "stage", "label", "unit_basis", "unit_rate", "mass_kg", "quantity", "unit_cost_per_part",
    "run_cost", "batch_charge", "total_cost", "supplies_material", "rate_note", "formula",
    "mass_source", "mass_note",
}
YIELD_KEYS = {"yield_percent", "base_mass_kg", "base_mass_source"}


def casting_line(**stage_input):
    return price_stages(
        [StageInput(stage="casting", label="Casting", **stage_input)],
        rate_set(casting=CASTING),
        quantity=5,
        material_stage="casting",
        finished_mass_kg=2.4,
    ).lines[0]


def test_a_yield_charges_the_cad_mass_divided_by_it_and_states_all_three():
    line = casting_line(yield_percent=60.0)
    assert line.base_mass_kg == 2.4
    assert line.base_mass_source == "cad_finished_mass"
    assert line.yield_percent == 60.0
    assert line.mass_kg == pytest.approx(4.0)
    assert line.mass_source == "yield_applied"
    # 120/kg x 4.0 kg x 5 off + 15,000 pattern.
    assert line.total_cost == pytest.approx(17400.0)
    assert "2.4 kg" in line.mass_note and "60%" in line.mass_note and "4 kg" in line.mass_note
    assert "entered by the engineer" in line.mass_note
    assert "yield_percent" in line.formula


def test_a_yield_applies_to_an_entered_blank_mass_too():
    line = casting_line(mass_kg=3.0, yield_percent=75.0)
    assert line.base_mass_kg == 3.0 and line.base_mass_source == "entered"
    assert line.mass_kg == pytest.approx(4.0)
    assert line.mass_source == "yield_applied"


def test_no_yield_is_no_allowance_and_the_line_keeps_its_old_keys():
    line = casting_line()
    assert line.mass_kg == 2.4 and line.mass_source == "cad_finished_mass"
    assert line.yield_percent is None and line.base_mass_kg is None
    assert set(line.model_dump()) == LINE_KEYS_WITHOUT_A_YIELD
    assert set(line.model_dump(mode="json")) == LINE_KEYS_WITHOUT_A_YIELD
    assert set(casting_line(yield_percent=60.0).model_dump()) == LINE_KEYS_WITHOUT_A_YIELD | YIELD_KEYS


def test_a_yield_of_one_hundred_percent_changes_no_money_but_is_recorded():
    without, with_full = casting_line(), casting_line(yield_percent=100.0)
    assert with_full.total_cost == without.total_cost
    assert with_full.mass_kg == without.mass_kg
    assert with_full.yield_percent == 100.0 and with_full.mass_source == "yield_applied"


@pytest.mark.parametrize("bad", [0.0, -5.0, 100.01, 250.0, float("inf"), float("nan")])
def test_a_yield_must_be_above_zero_and_at_most_one_hundred(bad):
    with pytest.raises(ValidationError):
        StageInput(stage="casting", label="Casting", yield_percent=bad)


def test_a_later_per_kg_step_works_on_the_blank_not_on_the_metal_poured():
    pricing = price_stages(
        [
            StageInput(stage="casting", label="Casting", yield_percent=60.0),
            StageInput(stage="fettling", label="Fettling"),
        ],
        rate_set(casting=CASTING, fettling={"unit_basis": "per_kg", "unit_rate": 10.0}),
        quantity=1,
        material_stage="casting",
        finished_mass_kg=2.4,
    )
    casting, fettling = pricing.lines
    assert casting.mass_kg == pytest.approx(4.0)
    assert fettling.mass_kg == 2.4 and fettling.mass_source == "blank_mass"


def test_a_blank_lighter_than_the_part_is_still_refused_whatever_the_yield():
    pricing = price_stages(
        [StageInput(stage="casting", label="Casting", mass_kg=1.0, yield_percent=50.0)],
        rate_set(casting=CASTING),
        quantity=1,
        material_stage="casting",
        finished_mass_kg=1.4,
    )
    assert pricing.problems[0].code == "needs_mass"
    assert "lighter" in pricing.problems[0].detail


def test_a_yield_on_a_per_piece_stage_is_refused_not_ignored():
    with pytest.raises(ValueError, match="per piece"):
        price_stages(
            [StageInput(stage="casting", label="Casting", yield_percent=60.0)],
            rate_set(casting={"unit_basis": "per_piece", "unit_rate": 400.0}),
            quantity=1,
            material_stage="casting",
            finished_mass_kg=2.4,
        )


def test_a_yield_on_a_batch_only_stage_is_refused():
    with pytest.raises(ValueError, match="per batch only"):
        price_stages(
            [StageInput(stage="casting", label="Casting", yield_percent=60.0)],
            rate_set(casting={"batch_charge": 2500.0}),
            quantity=1,
            material_stage="casting",
            finished_mass_kg=2.4,
        )


def test_a_yield_on_a_per_kg_stage_that_supplies_no_material_is_refused():
    with pytest.raises(ValueError, match="supplies the material"):
        price_stages(
            [StageInput(stage="finishing", label="Finishing", yield_percent=60.0)],
            rate_set(finishing={"unit_basis": "per_kg", "unit_rate": 40.0}),
            quantity=1,
            finished_mass_kg=2.4,
        )


def test_the_endpoint_charges_the_yielded_mass_and_the_part_keeps_its_own(client_rates):
    classification = classification_for("casting_machining")
    plain = cost(classification=classification).json()
    yielded = cost(classification=classification,
                   stage_inputs={"casting": {"yield_percent": 80.0}}).json()

    finished = plain["mass"]["mass_kg"]
    line, fettling = yielded["part_cost"]["stage_costs"]
    assert line["base_mass_kg"] == pytest.approx(finished)
    assert line["base_mass_source"] == "cad_finished_mass"
    assert line["yield_percent"] == 80.0
    assert line["mass_kg"] == pytest.approx(finished / 0.8)
    assert line["mass_source"] == "yield_applied"
    assert "heavier" in line["mass_note"] and "80%" in line["mass_note"]
    assert line["total_cost"] == pytest.approx(120.0 * finished / 0.8 * 5 + 15000.0, abs=0.01)
    # Fettling is charged on the blank, and the part's own mass is untouched.
    assert fettling == plain["part_cost"]["stage_costs"][1]
    assert yielded["mass"] == plain["mass"]
    assert yielded["part_cost"]["total_stage_cost"] > plain["part_cost"]["total_stage_cost"]
    assert yielded["part_cost"]["total_material_cost"] == 0.0


def test_no_yield_leaves_the_response_and_the_stored_estimate_as_they_were(client_rates):
    classification = classification_for("casting_machining")
    inputs = {"casting": {"mass_kg": 3.0}}
    absent = cost(classification=classification, stage_inputs=inputs).json()
    null = cost(classification=classification,
                stage_inputs={"casting": {"mass_kg": 3.0, "yield_percent": None}}).json()

    for line in absent["part_cost"]["stage_costs"]:
        assert set(line) == LINE_KEYS_WITHOUT_A_YIELD
    stored = client.get(f"/estimates/{absent['estimate_id']}").json()
    # Precise tokens: the scrap note's own prose says "yield-based".
    snapshot_text = json.dumps(stored["result_snapshot"])
    assert "yield_percent" not in snapshot_text and "base_mass" not in snapshot_text
    for line in stored["result_snapshot"]["part_cost"]["stage_costs"]:
        assert set(line) == LINE_KEYS_WITHOUT_A_YIELD
    assert stored["request_snapshot"]["stage_inputs"] == inputs
    # A null yield is no yield: the same figures, line for line.
    for body in (absent, null):
        body["part_cost"].pop("estimate_id")
    assert null["part_cost"] == absent["part_cost"]


def test_the_yield_is_recorded_with_the_estimate(client_rates):
    inputs = {"casting": {"mass_kg": 3.0, "yield_percent": 75.0}}
    estimate_id = cost(classification=classification_for("casting_machining"),
                       stage_inputs=inputs).json()["estimate_id"]
    stored = client.get(f"/estimates/{estimate_id}").json()
    assert stored["request_snapshot"]["stage_inputs"] == inputs
    line = stored["result_snapshot"]["part_cost"]["stage_costs"][0]
    assert (line["base_mass_kg"], line["yield_percent"], line["mass_kg"]) == (3.0, 75.0, 4.0)
    assert line["base_mass_source"] == "entered"
    # 120/kg x 4.0 kg x 5 off + 15,000 pattern.
    assert line["total_cost"] == pytest.approx(17400.0)


@pytest.mark.parametrize("bad", [0, -10, 100.5, "60", True, [60]])
def test_a_yield_that_is_not_a_percentage_is_refused_and_nothing_is_stored(client_rates, bad):
    before = estimate_count()
    response = cost(classification=classification_for("casting_machining"),
                    stage_inputs={"casting": {"yield_percent": bad}})
    assert response.status_code == 400
    assert "above 0 and at most 100" in response.json()["detail"]
    assert estimate_count() == before


def test_a_yield_on_a_step_that_supplies_no_material_is_refused_at_the_endpoint(client_rates):
    before = estimate_count()
    response = cost(classification=classification_for("casting_machining"),
                    stage_inputs={"fettling": {"yield_percent": 60.0}})
    assert response.status_code == 400
    assert "Fettling" in response.json()["detail"]
    assert "supplies the material" in response.json()["detail"]
    assert estimate_count() == before


def test_a_yield_on_a_per_piece_stage_is_refused_at_the_endpoint(client_rates):
    response = cost(classification=classification_for("purchased"),
                    stage_inputs={"purchase": {"yield_percent": 60.0}})
    assert response.status_code == 400
    assert "per piece" in response.json()["detail"]


def test_a_yield_for_a_stage_the_route_does_not_have_changes_nothing(client_rates):
    # Stage inputs are shared across the routes a comparison prices, so one for
    # a stage this route lacks is not this route's to refuse.
    classification = classification_for("sheet_cutting_bending")
    plain = cost(classification=classification).json()["part_cost"]
    other = cost(classification=classification,
                 stage_inputs={"casting": {"yield_percent": 60.0}}).json()["part_cost"]
    plain.pop("estimate_id"), other.pop("estimate_id")
    assert other == plain


def test_the_quotation_still_partitions_the_total_with_a_yield(client_rates):
    body = cost(classification=classification_for("casting_machining"),
                stage_inputs={"casting": {"mass_kg": 3.0, "yield_percent": 75.0}}).json()
    totals = client.get(f"/estimates/{body['estimate_id']}/quotation").json()["totals"]
    lines = {line["category"]: line for line in totals["lines"]}
    # Casting 120 x 4.0 x 5 + 15,000, and fettling 10 x 3.0 x 5 on the blank.
    assert lines["route_stages"]["amount"] == pytest.approx(17550.0)
    assert not totals["reconciliation"]


def test_a_comparison_attributes_a_yield_change_and_leaves_nothing_unexplained(client_rates):
    classification = classification_for("casting_machining")
    plain = cost(classification=classification,
                 stage_inputs={"casting": {"mass_kg": 3.0}}).json()["estimate_id"]
    yielded = cost(classification=classification,
                   stage_inputs={"casting": {"mass_kg": 3.0, "yield_percent": 75.0}}
                   ).json()["estimate_id"]
    comparison = client.get(
        f"/estimates/compare?baseline={plain}&comparison={yielded}"
    ).json()
    categories = {impact["category"] for impact in comparison["impacts"]}
    route_stage = [i for i in comparison["impacts"] if i["category"] == "route_stage"]
    moved = [i for i in route_stage if i["delta"]]
    # Only the casting moves: 120 x (4.0 - 3.0) kg x 5. Fettling stays on the blank.
    assert len(moved) == 1
    assert moved[0]["delta"] == pytest.approx(600.0)
    assert "yield none → 75%" in json.dumps(moved[0], ensure_ascii=False)
    assert "unexplained" not in categories
    assert "material_rate" not in categories and "material_quantity" not in categories


def test_the_route_catalog_says_which_stages_may_carry_a_yield():
    assert client.get("/process-routes").json()["yield_stages"] == sorted(MATERIAL_SUPPLYING_STAGES)


def _js_without_comments(name: str) -> str:
    lines = (PACKAGE / "static" / name).read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.strip().startswith("//"))


def test_the_page_sends_a_blank_yield_as_absent_and_works_nothing_out():
    process = _js_without_comments("process.js")
    # One setter for both fields, so typing a mass cannot drop the yield.
    assert (
        'setStageInput(input.dataset.stage, "yield_percent", raw === "" ? null : Number(raw))'
        in process
    )
    assert "if (value === null || value === undefined) delete next[field];" in _js_without_comments("plan.js")
    assert "{ mass_kg: Number(raw) }" not in process
    assert "{ mass_kg: Number(raw) }" not in _js_without_comments("routecompare.js")
    # Which stages take a yield is the server's list, and the yield is never
    # applied in the browser: it appears only as an input and as a label.
    assert "processRoutes.yield_stages" in process
    for name in ("process.js", "plan.js", "routecompare.js", "app.js"):
        code = _js_without_comments(name)
        assert "yield_percent /" not in code and "/ yield" not in code.lower(), name
        assert "enteredYield /" not in code and "/ enteredYield" not in code, name


# --- 8. a tool's one-off charge amortised over the pieces it will make ----------
#
# The engineer states how many pieces the pattern or die is expected to make; the
# estimate then carries only its own share of the one-off charge. No tool life is
# ever assumed: absent, the whole charge falls on the batch, as it always has.

AMORTISATION_KEYS = {
    "full_batch_charge", "amortisation_quantity", "batch_charge_basis", "batch_charge_note",
}


def test_an_amortised_charge_is_the_full_charge_times_this_quantity_over_the_tool_life():
    line = casting_line(mass_kg=3.0, amortisation_quantity=100)
    # 15,000 pattern x 5 off / 100 pieces = 750; 120/kg x 3.0 kg x 5 = 1,800.
    assert line.full_batch_charge == 15000.0
    assert line.amortisation_quantity == 100
    assert line.batch_charge == pytest.approx(750.0)
    assert line.run_cost == pytest.approx(1800.0)
    assert line.total_cost == pytest.approx(2550.0)
    assert line.batch_charge_basis == "amortised_share"
    assert "full_batch_charge x quantity / amortisation_quantity" in line.formula
    note = line.batch_charge_note
    assert "750.00" in note and "15,000.00" in note and "14,250.00" in note
    assert "5 of the 100 pieces" in note
    assert "outside this estimate" in note and "No tool life is assumed" in note


def test_the_share_is_not_rounded_before_it_is_added():
    line = casting_line(mass_kg=3.0, amortisation_quantity=7)
    assert line.batch_charge == 15000.0 * 5 / 7
    assert line.total_cost == 1800.0 + 15000.0 * 5 / 7
    # Rounded once, on the way out.
    assert line.model_dump()["total_cost"] == 12514.29
    assert line.model_dump()["batch_charge"] == 10714.29


def test_no_amortisation_quantity_is_the_whole_charge_and_the_line_keeps_its_old_keys():
    line = casting_line(mass_kg=3.0)
    assert line.batch_charge == 15000.0 and line.total_cost == pytest.approx(16800.0)
    assert set(line.model_dump()) == LINE_KEYS_WITHOUT_A_YIELD
    assert set(line.model_dump(mode="json")) == LINE_KEYS_WITHOUT_A_YIELD
    assert set(casting_line(amortisation_quantity=100).model_dump()) == (
        LINE_KEYS_WITHOUT_A_YIELD | AMORTISATION_KEYS
    )
    assert set(casting_line(yield_percent=60.0, amortisation_quantity=100).model_dump()) == (
        LINE_KEYS_WITHOUT_A_YIELD | YIELD_KEYS | AMORTISATION_KEYS
    )


def test_a_tool_life_equal_to_the_quantity_charges_the_whole_tool_and_says_so():
    without, equal = casting_line(mass_kg=3.0), casting_line(mass_kg=3.0, amortisation_quantity=5)
    assert equal.total_cost == without.total_cost
    assert equal.amortisation_quantity == 5 and equal.batch_charge_basis == "amortised_share"


def test_a_yield_and_an_amortisation_quantity_combine_in_one_formula():
    line = casting_line(mass_kg=3.0, yield_percent=75.0, amortisation_quantity=100)
    # 120 x 4.0 kg x 5 + 750.
    assert line.total_cost == pytest.approx(3150.0)
    assert line.formula == (
        "total_cost = unit_rate x (base_mass_kg / (yield_percent / 100)) x quantity"
        " + full_batch_charge x quantity / amortisation_quantity"
    )


@pytest.mark.parametrize("bad", [0, -3])
def test_an_amortisation_quantity_must_be_at_least_one(bad):
    with pytest.raises(ValidationError):
        StageInput(stage="casting", label="Casting", amortisation_quantity=bad)


def test_a_tool_life_shorter_than_the_quantity_is_refused_not_a_second_tool():
    with pytest.raises(ValueError, match="fewer than the 5 this estimate is for"):
        casting_line(mass_kg=3.0, amortisation_quantity=4)


def test_an_amortisation_quantity_on_a_stage_with_no_one_off_charge_is_refused():
    with pytest.raises(ValueError, match="no one-off batch charge"):
        price_stages(
            [StageInput(stage="purchase", label="Purchased complete", amortisation_quantity=100)],
            rate_set(purchase={"unit_basis": "per_piece", "unit_rate": 900.0}),
            quantity=5,
        )


def test_a_batch_only_stage_can_be_amortised():
    line = price_stages(
        [StageInput(stage="casting", label="Casting", amortisation_quantity=1000)],
        rate_set(casting={"batch_charge": 2500.0}),
        quantity=40,
    ).lines[0]
    # 2,500 x 40 / 1,000.
    assert line.total_cost == pytest.approx(100.0)
    assert line.unit_basis is None and line.full_batch_charge == 2500.0


def test_the_endpoint_charges_the_share_and_records_the_tool_life(client_rates):
    inputs = {"casting": {"mass_kg": 3.0, "amortisation_quantity": 100}}
    body = cost(classification=classification_for("casting_machining"), stage_inputs=inputs).json()
    line, fettling = body["part_cost"]["stage_costs"]
    assert (line["full_batch_charge"], line["amortisation_quantity"]) == (15000.0, 100)
    assert line["batch_charge"] == 750.0 and line["total_cost"] == 2550.0
    assert set(fettling) == LINE_KEYS_WITHOUT_A_YIELD
    # 2,550 casting + 150 fettling on the same blank.
    assert body["part_cost"]["total_stage_cost"] == 2700.0
    stored = client.get(f"/estimates/{body['estimate_id']}").json()
    assert stored["request_snapshot"]["stage_inputs"] == inputs
    assert stored["result_snapshot"]["part_cost"]["stage_costs"][0] == line


def test_no_amortisation_quantity_leaves_the_response_and_the_stored_estimate_as_they_were(
    client_rates,
):
    classification = classification_for("casting_machining")
    absent = cost(classification=classification, stage_inputs={"casting": {"mass_kg": 3.0}}).json()
    null = cost(classification=classification,
                stage_inputs={"casting": {"mass_kg": 3.0, "amortisation_quantity": None}}).json()
    stored = client.get(f"/estimates/{absent['estimate_id']}").json()
    snapshot_text = json.dumps(stored["result_snapshot"])
    assert "amortisation_quantity" not in snapshot_text and "full_batch_charge" not in snapshot_text
    for line in stored["result_snapshot"]["part_cost"]["stage_costs"]:
        assert set(line) == LINE_KEYS_WITHOUT_A_YIELD
    for body in (absent, null):
        body["part_cost"].pop("estimate_id")
    assert null["part_cost"] == absent["part_cost"]
    assert absent["part_cost"]["stage_costs"][0]["batch_charge"] == 15000.0


@pytest.mark.parametrize("bad", [0, -10, 2.5, "100", True, [100]])
def test_a_tool_life_that_is_not_a_whole_number_of_pieces_is_refused(client_rates, bad):
    before = estimate_count()
    response = cost(classification=classification_for("casting_machining"),
                    stage_inputs={"casting": {"amortisation_quantity": bad}})
    assert response.status_code == 400
    assert "whole number of pieces" in response.json()["detail"]
    assert estimate_count() == before


def test_a_whole_float_tool_life_is_read_as_that_many_pieces(client_rates):
    line = cost(classification=classification_for("casting_machining"),
                stage_inputs={"casting": {"mass_kg": 3.0, "amortisation_quantity": 100.0}}
                ).json()["part_cost"]["stage_costs"][0]
    assert line["amortisation_quantity"] == 100 and line["batch_charge"] == 750.0


def test_a_tool_life_below_the_quantity_is_refused_at_the_endpoint_and_nothing_stored(client_rates):
    before = estimate_count()
    response = cost(classification=classification_for("casting_machining"),
                    stage_inputs={"casting": {"amortisation_quantity": 4}})
    assert response.status_code == 400
    assert "fewer than the 5 this estimate is for" in response.json()["detail"]
    assert estimate_count() == before


def test_a_tool_life_on_a_stage_with_no_one_off_charge_is_refused_at_the_endpoint(client_rates):
    response = cost(classification=classification_for("purchased"),
                    stage_inputs={"purchase": {"amortisation_quantity": 100}})
    assert response.status_code == 400
    assert "no one-off batch charge" in response.json()["detail"]


def test_a_tool_life_for_a_stage_the_route_does_not_have_changes_nothing(client_rates):
    # Stage inputs are shared across the routes a comparison prices.
    classification = classification_for("sheet_cutting_bending")
    plain = cost(classification=classification).json()["part_cost"]
    other = cost(classification=classification,
                 stage_inputs={"casting": {"amortisation_quantity": 100}}).json()["part_cost"]
    plain.pop("estimate_id"), other.pop("estimate_id")
    assert other == plain


def test_the_quotation_partitions_the_total_and_says_the_charge_is_a_share(client_rates):
    body = cost(classification=classification_for("casting_machining"),
                stage_inputs={"casting": {"mass_kg": 3.0, "amortisation_quantity": 100}}).json()
    for view in ("customer", "engineering"):
        totals = client.get(
            f"/estimates/{body['estimate_id']}/quotation?view={view}"
        ).json()["totals"]
        lines = {line["category"]: line for line in totals["lines"]}
        assert lines["route_stages"]["amount"] == pytest.approx(2700.0)
        assert "Casting: Amortised share of the tool" in lines["route_stages"]["detail"]
        assert "outside this estimate" in lines["route_stages"]["detail"]
        assert not totals["reconciliation"]


def test_a_quotation_without_a_tool_life_keeps_its_stage_line_wording(client_rates):
    body = cost(classification=classification_for("casting_machining"),
                stage_inputs={"casting": {"mass_kg": 3.0}}).json()
    lines = client.get(f"/estimates/{body['estimate_id']}/quotation").json()["totals"]["lines"]
    route_stages = next(line for line in lines if line["category"] == "route_stages")
    assert route_stages["detail"] == "Priced from the rate set's stage rates."


def test_a_comparison_attributes_a_tool_life_change_and_leaves_nothing_unexplained(client_rates):
    classification = classification_for("casting_machining")
    whole = cost(classification=classification,
                 stage_inputs={"casting": {"mass_kg": 3.0}}).json()["estimate_id"]
    shared = cost(classification=classification,
                  stage_inputs={"casting": {"mass_kg": 3.0, "amortisation_quantity": 100}}
                  ).json()["estimate_id"]
    comparison = client.get(f"/estimates/compare?baseline={whole}&comparison={shared}").json()
    categories = {impact["category"] for impact in comparison["impacts"]}
    moved = [i for i in comparison["impacts"] if i["category"] == "route_stage" and i["delta"]]
    # Only the casting moves: 750 - 15,000.
    assert len(moved) == 1
    assert moved[0]["delta"] == pytest.approx(-14250.0)
    assert "tool charge amortised over this batch only → 100 pieces" in json.dumps(
        moved[0], ensure_ascii=False
    )
    assert "unexplained" not in categories


def test_a_timed_stage_cannot_carry_a_tool_life():
    from tests.test_timed_welding import cost as timed_cost, publish_weld_shop

    publish_weld_shop()
    before = estimate_count()
    response = timed_cost(stage_inputs={"welding": {"pricing": "timed", "amortisation_quantity": 100}})
    assert response.status_code == 400
    assert "priced by time" in response.json()["detail"]
    assert "nothing to amortise" in response.json()["detail"]
    assert estimate_count() == before


def test_route_comparison_previews_the_share_exactly_as_the_estimate_charges_it(client_rates):
    inputs = {"casting": {"mass_kg": 3.0, "amortisation_quantity": 100}}
    data = {**FORM, "rate_set_id": "client-shop",
            "process_classification": json.dumps(suggested(NAME)["process_classification"]),
            "manufacturing_plan": committed_plan(NAME).model_dump_json(),
            "stage_inputs": json.dumps(inputs)}
    response = client.post(
        "/cost/route-comparison",
        files={"file": (f"{NAME}.step", step(NAME), "application/octet-stream")},
        data=data,
    )
    rows = {row["route_id"]: row for row in response.json()["routes"]}
    for route_id in ("casting", "casting_machining"):
        part = cost(classification=classification_for(route_id),
                    stage_inputs=inputs).json()["part_cost"]
        assert rows[route_id]["status"] == "priced", route_id
        assert rows[route_id]["total_stage_cost"] == part["total_stage_cost"], route_id
        assert rows[route_id]["total_part_cost"] == part["total_part_cost"], route_id
    assert rows["casting_machining"]["total_stage_cost"] == pytest.approx(2700.0)


def test_the_matrix_share_follows_each_cells_quantity_and_refuses_beyond_the_tool_life(
    client_rates,
):
    inputs = {"casting": {"mass_kg": 3.0, "amortisation_quantity": 100}}
    classification = json.dumps(classification_for("casting_machining"))
    plan = committed_plan(NAME).model_dump_json()
    upload = {"file": (f"{NAME}.step", step(NAME), "application/octet-stream")}
    base = {key: value for key, value in FORM.items() if key not in ("quantity", "rate_set_id")}
    base.update({"process_classification": classification, "manufacturing_plan": plan,
                 "stage_inputs": json.dumps(inputs)})
    response = client.post("/cost/matrix-preview", files=upload, data={
        **base,
        "rate_sets": json.dumps([{"rate_set_id": "client-shop"}]),
        "quantities": json.dumps([5, 40, 150]),
    })
    assert response.status_code == 200, response.text
    q5, q40, q150 = response.json()["rows"][0]["cells"]
    # Casting 120 x 3.0 x q + 15,000 x q / 100, and fettling 10 x 3.0 x q.
    assert q5["total_stage_cost"] == pytest.approx(1800 + 750 + 150)
    assert q40["total_stage_cost"] == pytest.approx(14400 + 6000 + 1200)
    # The stated tool makes 100: a 150-off cell is refused in the endpoint's words.
    assert q150["status"] == "refused" and q150["total_part_cost"] is None
    assert "fewer than the 150 this estimate is for" in q150["refusal"]["message"]
    # Each priced cell is the figure /cost/cad-part gives at that quantity.
    for cell, quantity in ((q5, 5), (q40, 40)):
        part = client.post("/cost/cad-part", files=upload, data={
            **base, "quantity": str(quantity), "rate_set_id": "client-shop",
        }).json()["part_cost"]
        assert cell["total_part_cost"] == part["total_part_cost"], quantity
        assert cell["total_stage_cost"] == part["total_stage_cost"], quantity


def test_the_page_offers_a_tool_life_from_the_rate_set_and_works_nothing_out():
    process = _js_without_comments("process.js")
    assert (
        'setStageInput(input.dataset.stage, "amortisation_quantity", raw === "" ? null : Number(raw))'
        in process
    )
    # Offered where the published rate has a one-off charge (the rate set says
    # which) and never for a stage priced by time — except moulding, whose timed
    # line still carries the mould (tests/test_moulding_times.py).
    assert "rate.batch_charge && (!timed || materialLine)" in process
    for name in ("process.js", "plan.js", "routecompare.js", "app.js"):
        code = _js_without_comments(name)
        assert "/ amortisation_quantity" not in code and "amortisation_quantity /" not in code, name
        assert "/ enteredToolLife" not in code and "enteredToolLife /" not in code, name
        assert "batch_charge *" not in code and "* line.quantity" not in code, name
