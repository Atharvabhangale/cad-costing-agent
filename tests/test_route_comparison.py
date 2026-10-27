"""Comparing routes side by side: a preview of each, never an estimate or a choice.

What is checked is the shape of the honesty. Every route goes through the one pricing
path, so its figure is the one `/cost/cad-part` would give for the same decided route;
a route that cannot be priced is listed with its reason, never dropped or zeroed;
nothing is stored; and nothing is ranked or chosen.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.main import app
from tests.test_process_classification import FORM, committed_plan, decide, step, suggested
from tests.test_stage_costing import STAGE_RATES, publish

client = TestClient(app)
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
STATIC = PACKAGE / "static"
NAME = "block_with_counterbore"  # recommended milling + drilling; candidate family: casting


def compare(name=NAME, *, classification=None, rate_set_id="poc-default", stage_inputs=None, plan=True):
    classification = classification or suggested(name)["process_classification"]
    data = {**FORM, "rate_set_id": rate_set_id, "process_classification": json.dumps(classification)}
    if plan:
        data["manufacturing_plan"] = committed_plan(name).model_dump_json()
    if stage_inputs is not None:
        data["stage_inputs"] = json.dumps(stage_inputs)
    return client.post(
        "/cost/route-comparison",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
        data=data,
    )


def estimate_count() -> int:
    return client.get("/estimates").json()["total"]


def by_id(body: dict) -> dict:
    return {row["route_id"]: row for row in body["routes"]}


# --- 1. which routes, in what order -------------------------------------------


def test_the_recommendation_comes_first_then_candidate_routes_in_catalog_order():
    body = compare().json()
    assert [(r["route_id"], r["role"]) for r in body["routes"]] == [
        ("milling_drilling", "recommended"),
        ("casting", "candidate"),
        ("casting_machining", "candidate"),
    ]


def test_with_no_recommendation_the_candidates_alone_are_compared():
    body = compare("plain_block", plan=False).json()
    assert body["routes"], body
    assert {row["role"] for row in body["routes"]} == {"candidate"}
    assert {row["family"] for row in body["routes"]} == {"machining"}


def test_a_bad_classification_is_refused():
    response = client.post(
        "/cost/route-comparison",
        files={"file": ("p.step", step(NAME), "application/octet-stream")},
        data={**FORM, "process_classification": "{not json"},
    )
    assert response.status_code == 400


# --- 2. priced and refused rows -----------------------------------------------


def test_a_route_that_cannot_be_priced_is_listed_with_its_reason_and_no_figures():
    # poc-default has no stage rates, so both casting routes are refused.
    rows = by_id(compare().json())
    assert rows["milling_drilling"]["status"] == "priced"
    assert rows["milling_drilling"]["total_part_cost"] > 0
    for route_id in ("casting", "casting_machining"):
        row = rows[route_id]
        assert row["status"] == "refused"
        assert row["refusal"]["code"] == "route_stages_not_priceable"
        assert "casting" in row["refusal"]["message"].lower()
        assert row["total_part_cost"] is None and row["per_unit_cost"] is None
        assert row["total_material_cost"] is None


def test_with_stage_rates_and_a_mass_the_casting_routes_are_priced_too():
    assert publish(STAGE_RATES).status_code == 201
    body = compare(rate_set_id="client-shop", stage_inputs={"casting": {"mass_kg": 3.0}}).json()
    rows = by_id(body)
    assert [row["status"] for row in body["routes"]] == ["priced"] * 3
    assert rows["casting_machining"]["total_stage_cost"] == pytest.approx(16800.0)
    assert rows["casting_machining"]["material_charged"] is False
    assert rows["milling_drilling"]["total_stage_cost"] == 0.0


def test_a_per_kg_stage_with_no_entered_mass_is_priced_on_the_cad_mass_and_says_so():
    assert publish(STAGE_RATES).status_code == 201
    rows = by_id(compare(rate_set_id="client-shop").json())
    assert rows["casting"]["status"] == "priced"
    [mass] = rows["casting"]["stage_masses"]
    assert mass["stage"] == "casting"
    assert mass["mass_source"] == "cad_finished_mass"
    assert mass["mass_kg"] > 0
    assert rows["milling_drilling"]["status"] == "priced"
    assert rows["milling_drilling"]["stage_masses"] == []


def test_a_row_says_what_its_cost_leaves_out_of_the_plan():
    row = by_id(compare().json())["milling_drilling"]
    assert row["operations_costed"] > 0
    assert row["operations_not_timed"] == 0
    # No plan sent: the counts are unknown, not zero.
    bare = by_id(compare(plan=False).json())["milling_drilling"]
    assert bare["operations_costed"] is None


# --- 3. a preview: nothing stored, nothing chosen -----------------------------


def test_nothing_is_stored():
    before = estimate_count()
    body = compare().json()
    assert body["stored"] is False and body["kind"] == "preview"
    assert "not estimates" in body["note"]
    assert estimate_count() == before


def test_nothing_is_chosen_and_nothing_is_marked_best():
    assert publish(STAGE_RATES).status_code == 201
    body = compare(rate_set_id="client-shop", stage_inputs={"casting": {"mass_kg": 3.0}}).json()
    assert body["chosen"] is None
    keys = {key for row in body["routes"] for key in row} | set(body)
    for word in ("cheapest", "best", "rank", "lowest", "winner", "selected"):
        assert not any(word in key for key in keys), word


def test_the_order_does_not_follow_the_cost():
    assert publish(STAGE_RATES).status_code == 201
    heavy = compare(rate_set_id="client-shop", stage_inputs={"casting": {"mass_kg": 3.0}}).json()
    light = compare(
        rate_set_id="client-shop", stage_inputs={"casting": {"mass_kg": 0.0001}}
    ).json()
    heavy_ids = [r["route_id"] for r in heavy["routes"]]
    assert heavy_ids == [r["route_id"] for r in light["routes"]]
    assert heavy_ids[0] == "milling_drilling"
    # The casting figure moved with the mass; the order did not.
    assert by_id(heavy)["casting"]["total_part_cost"] != by_id(light)["casting"]["total_part_cost"]


def test_a_current_decision_is_reported_but_does_not_change_what_is_compared():
    undecided = compare().json()
    classification = decide(
        suggested(NAME)["process_classification"], "edited", "casting_machining"
    ).json()
    decided = compare(classification=classification).json()
    assert by_id(decided)["casting_machining"]["currently_decided"] is True
    assert by_id(undecided)["casting_machining"]["currently_decided"] is False

    def strip(body):
        return [{k: v for k, v in row.items() if k != "currently_decided"} for row in body["routes"]]

    assert strip(decided) == strip(undecided)


# --- 4. one pricing path ------------------------------------------------------


def _cost(route_id: str, *, stage_inputs=None, rate_set_id="poc-default"):
    classification = decide(suggested(NAME)["process_classification"], "edited", route_id).json()
    data = {
        **FORM,
        "rate_set_id": rate_set_id,
        "process_classification": json.dumps(classification),
        "manufacturing_plan": committed_plan(NAME).model_dump_json(),
    }
    if stage_inputs is not None:
        data["stage_inputs"] = json.dumps(stage_inputs)
    return client.post(
        "/cost/cad-part",
        files={"file": (f"{NAME}.step", step(NAME), "application/octet-stream")},
        data=data,
    )


def test_a_previewed_figure_is_exactly_what_cost_cad_part_gives_for_that_route():
    assert publish(STAGE_RATES).status_code == 201
    inputs = {"casting": {"mass_kg": 3.0}}
    rows = by_id(compare(rate_set_id="client-shop", stage_inputs=inputs).json())
    for route_id in ("milling_drilling", "casting", "casting_machining"):
        cost = _cost(route_id, stage_inputs=inputs, rate_set_id="client-shop")
        assert cost.status_code == 200, cost.text
        part = cost.json()["part_cost"]
        row = rows[route_id]
        assert row["total_part_cost"] == part["total_part_cost"], route_id
        assert row["per_unit_cost"] == part["per_unit_cost"], route_id
        assert row["total_material_cost"] == part["total_material_cost"], route_id
        assert row["total_operation_cost"] == part["total_operation_cost"], route_id
        assert row["total_stage_cost"] == part["total_stage_cost"], route_id


def test_a_refused_route_is_refused_in_the_costing_endpoints_own_words():
    row = by_id(compare().json())["casting_machining"]
    refused = _cost("casting_machining").json()["detail"]
    assert row["refusal"]["code"] == refused["error"]
    assert row["refusal"]["message"] == refused["message"]


# --- 5. determinism -----------------------------------------------------------


def test_the_same_inputs_give_the_same_comparison():
    assert publish(STAGE_RATES).status_code == 201
    inputs = {"casting": {"mass_kg": 3.0}}
    first = compare(rate_set_id="client-shop", stage_inputs=inputs).json()
    second = compare(rate_set_id="client-shop", stage_inputs=inputs).json()
    assert first == second


# --- 6. structure -------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_comparison_module_imports_only_money_and_only_main_reads_it():
    own = {m for m in _imports(PACKAGE / "route_comparison.py") if m.startswith("cad_costing_agent")}
    assert own == {"cad_costing_agent.money"}
    readers = {
        p.name
        for p in PACKAGE.rglob("*.py")
        if "cad_costing_agent.route_comparison" in _imports(p)
    }
    assert readers == {"main.py"}


def _endpoint_code() -> str:
    tree = ast.parse((PACKAGE / "main.py").read_text(encoding="utf-8"))
    node = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "compare_routes"
    )
    # Drop the docstring and bare string statements: the prose says what is absent.
    node.body = [
        s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]
    return ast.unparse(node)


def test_the_endpoint_neither_ranks_nor_stores_nor_prices_on_its_own():
    code = _endpoint_code()
    for token in (
        "sorted(", ".sort(", " min(", " max(", "EstimateRepository", ".save(", "commit(",
        "calculate_", "price_stages",
    ):
        assert token not in code, token
    assert "price_cad_part(" in code


def test_the_comparison_module_cannot_price_or_rank():
    tree = ast.parse((PACKAGE / "route_comparison.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"sorted", "sort", "min", "max", "sum"}


def test_the_ui_script_is_loaded_between_actuals_and_estimateflow_and_does_no_arithmetic():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    order = re.findall(r'<script src="([\w.]+\.js)"', html)
    # After the globals it borrows, before the flow router that may call it.
    assert order.index("actuals.js") < order.index("routecompare.js") < order.index("estimateflow.js")

    source = (STATIC / "routecompare.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))
    assert "/cost/route-comparison" in code
    assert "/cad/process/decide" not in code  # the choice goes through process.js's decideProcess
    assert "decideProcess(" in code
    for token in ("sort(", "Math.min", "Math.max", "reduce(", "toFixed", "Math.round"):
        assert token not in code, token
