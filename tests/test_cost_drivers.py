"""The cost-driver table of one stored estimate.

The drivers are read from the stored result, partition the stored total (any
remainder is its own `reconciliation` driver), carry a share each, and come in
build-up order. Nothing is recalculated, looked up, ranked or stored.
"""

from __future__ import annotations

import ast
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent import cost_drivers
from cad_costing_agent.main import app
from tests import assembly_fixtures
from tests import test_stage_costing as stage_tests
from tests import test_timed_welding as weld_tests
from tests import test_turned_parts as turned_tests
from tests.test_manufacturing_plan import code_without_prose
from tests.test_process_classification import FORM, committed_plan, step

client = TestClient(app)
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
STATIC = PACKAGE / "static"


def drivers_of(estimate_id: str) -> dict:
    response = client.get(f"/estimates/{estimate_id}/cost-drivers")
    assert response.status_code == 200, response.text
    return response.json()


def stored_total(estimate_id: str) -> float:
    return client.get(f"/estimates/{estimate_id}").json()["total_part_cost"]


def cents(value: float) -> int:
    return int((Decimal(str(value)) * 100).to_integral_value())


def assert_partitions(breakdown: dict, total: float) -> None:
    """The drivers as shown add up to the stored total as shown, to the paisa."""
    assert breakdown["total"] == total
    amounts = [driver["amount"] for driver in breakdown["drivers"] if driver["amount"] is not None]
    assert sum(cents(amount) for amount in amounts) == cents(total)
    keys = [driver["key"] for driver in breakdown["drivers"]]
    assert len(keys) == len(set(keys))


def assert_shares_are_the_amounts_over_the_total(breakdown: dict) -> None:
    for driver in breakdown["drivers"]:
        if driver["amount"] is None:
            assert driver["share_percent"] is None
            continue
        expected = Decimal(str(driver["amount"])) * 100 / Decimal(str(breakdown["total"]))
        assert driver["share_percent"] == pytest.approx(float(expected), abs=0.006)


def machining_estimate() -> str:
    """A milled and drilled block under the rate set that applies overhead, scrap and tooling."""
    name = stage_tests.NAME
    data = {**FORM, "rate_set_id": "poc-machine-shop",
            "manufacturing_plan": committed_plan(name).model_dump_json()}
    response = client.post(
        "/cost/cad-part",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
        data=data,
    )
    assert response.status_code == 200, response.text
    return response.json()["estimate_id"]


# --------------------------------------------------------------------- single parts


def test_a_machined_part_is_material_operations_by_process_and_additional_costs():
    estimate_id = machining_estimate()
    breakdown = drivers_of(estimate_id)
    assert breakdown["status"] == "available" and breakdown["estimate_kind"] == "part"
    assert_partitions(breakdown, stored_total(estimate_id))
    assert_shares_are_the_amounts_over_the_total(breakdown)

    keys = [driver["key"] for driver in breakdown["drivers"]]
    assert keys[0] == "material"
    processes = [key for key in keys if key.startswith("process:")]
    assert set(processes) == {"process:milling", "process:drilling"}
    assert keys[1 : 1 + len(processes)] == processes
    assert keys[1 + len(processes) : 4 + len(processes)] == ["overhead", "scrap", "tooling"]

    stored = client.get(f"/estimates/{estimate_id}").json()["result_snapshot"]["part_cost"]
    by_key = {driver["key"]: driver for driver in breakdown["drivers"]}
    assert by_key["material"]["amount"] == stored["total_material_cost"]
    assert by_key["overhead"]["amount"] == stored["additional_costs"]["overhead_cost"]
    assert by_key["tooling"]["amount"] == stored["additional_costs"]["tooling_cost"]
    # Each process group is its stored operations' own figures, setup and run kept apart.
    milling = [op for op in stored["operations"] if op["process_type"] == "milling"]
    assert by_key["process:milling"]["amount"] == pytest.approx(
        sum(op["total_operation_cost"] for op in milling), abs=1e-9
    )
    parts = {part["label"]: part["amount"] for part in by_key["process:milling"]["of_which"]}
    assert parts["Setup"] == pytest.approx(sum(op["setup_cost"] for op in milling), abs=1e-9)
    assert parts["Machine (run)"] == pytest.approx(sum(op["machine_cost"] for op in milling), abs=1e-9)


def test_a_cast_part_with_a_yield_shows_each_stage_and_says_the_casting_supplies_the_material():
    assert stage_tests.publish(stage_tests.STAGE_RATES).status_code == 201
    response = stage_tests.cost(
        classification=stage_tests.classification_for("casting_machining"),
        stage_inputs={"casting": {"yield_percent": 80.0}},
    )
    assert response.status_code == 200, response.text
    estimate_id = response.json()["estimate_id"]
    breakdown = drivers_of(estimate_id)
    assert_partitions(breakdown, stored_total(estimate_id))
    assert_shares_are_the_amounts_over_the_total(breakdown)

    keys = [driver["key"] for driver in breakdown["drivers"]]
    assert keys[:3] == ["material", "stage:casting", "stage:fettling"]
    by_key = {driver["key"]: driver for driver in breakdown["drivers"]}
    assert by_key["material"]["amount"] == 0.0
    assert "Casting rate includes the material" in by_key["material"]["note"]
    casting = by_key["stage:casting"]
    stored_line = response.json()["part_cost"]["stage_costs"][0]
    assert casting["amount"] == stored_line["total_cost"]
    assert "yield 80.0%" in casting["note"]
    assert {part["label"] for part in casting["of_which"]} == {"Run", "Batch charge (one-off)"}


def test_a_timed_weld_is_an_operation_driver_and_never_also_a_stage():
    weld_tests.publish_weld_shop()
    response = weld_tests.cost(stage_inputs={"welding": {"pricing": "timed"}})
    assert response.status_code == 200, response.text
    estimate_id = response.json()["estimate_id"]
    breakdown = drivers_of(estimate_id)
    assert_partitions(breakdown, stored_total(estimate_id))

    keys = [driver["key"] for driver in breakdown["drivers"]]
    assert "process:welding" in keys
    assert "stage:welding" not in keys
    assert [key for key in keys if key.startswith("stage:")] == [
        "stage:cutting", "stage:fit_up", "stage:weld_finishing"
    ]


def test_a_part_cut_from_a_confirmed_bar_says_its_material_is_the_bars():
    confirmed = client.post(
        "/cad/stock/decide", json={"stock": turned_tests.proposed(), "action": "confirm"}
    ).json()
    data = {**turned_tests.FORM, "raw_stock": json.dumps(confirmed),
            "manufacturing_plan": committed_plan(turned_tests.SHAFT).model_dump_json()}
    response = client.post(
        "/cost/cad-part",
        files={"file": ("s.step", turned_tests.step(turned_tests.SHAFT), "application/octet-stream")},
        data=data,
    )
    assert response.status_code == 200, response.text
    estimate_id = response.json()["estimate_id"]
    breakdown = drivers_of(estimate_id)
    assert_partitions(breakdown, stored_total(estimate_id))
    material = breakdown["drivers"][0]
    assert material["key"] == "material"
    assert material["amount"] == response.json()["part_cost"]["total_material_cost"]
    assert "confirmed bar stock" in material["note"]
    assert "process:turning" in [driver["key"] for driver in breakdown["drivers"]]


def test_reading_the_drivers_is_deterministic_and_stores_nothing():
    estimate_id = machining_estimate()
    count = client.get("/estimates").json()["total"]
    first = client.get(f"/estimates/{estimate_id}/cost-drivers").json()
    second = client.get(f"/estimates/{estimate_id}/cost-drivers").json()
    assert first == second
    assert client.get("/estimates").json()["total"] == count


def test_an_unknown_estimate_is_404():
    assert client.get("/estimates/nope/cost-drivers").status_code == 404


# --------------------------------------------------------------------- the partition rule


def _snapshot(**overrides) -> dict:
    part_cost = {
        "currency": "INR",
        "material": {"quantity": 2},
        "total_material_cost": 100.0,
        "operations": [
            {"process_type": "milling", "total_operation_cost": 50.0, "setup_cost": 10.0,
             "machine_cost": 30.0, "labor_cost": 10.0},
        ],
        "total_operation_cost": 50.0,
        "additional_costs": {"overhead_cost": 15.0, "overhead_rate_percent": 10.0,
                             "scrap_cost": 0.0, "scrap_rate_percent": 0.0, "tooling_cost": 0.0},
        "total_part_cost": 165.0,
    }
    part_cost.update(overrides)
    return {"part_cost": part_cost}


def test_a_remainder_is_its_own_reconciliation_driver_never_absorbed():
    breakdown = cost_drivers.cost_drivers("x", _snapshot(total_part_cost=165.02)).model_dump(
        mode="json"
    )
    recon = breakdown["drivers"][-1]
    assert recon["key"] == "reconciliation" and recon["amount"] == pytest.approx(0.02)
    assert "rounding" in recon["note"]
    # Every other driver keeps exactly its stored figure.
    assert breakdown["drivers"][0]["amount"] == 100.0
    assert_partitions(breakdown, 165.02)


def test_an_absent_field_is_not_recorded_never_zero_and_its_money_is_reconciled():
    snapshot = _snapshot()
    del snapshot["part_cost"]["additional_costs"]
    del snapshot["part_cost"]["operations"]
    breakdown = cost_drivers.cost_drivers("x", snapshot).model_dump(mode="json")
    by_key = {driver["key"]: driver for driver in breakdown["drivers"]}
    # Per-operation costs absent: one unsplit operations driver from the stored total.
    assert by_key["operations"]["amount"] == 50.0
    assert "not recorded" in by_key["operations"]["note"]
    # No additional split and no total: not recorded, not zero.
    assert by_key["additional"]["amount"] is None and by_key["additional"]["recorded"] is False
    assert by_key["additional"]["share_percent"] is None
    recon = by_key["reconciliation"]
    assert recon["amount"] == pytest.approx(15.0)
    assert "Additional costs" in recon["note"]
    assert_partitions(breakdown, 165.0)


def test_drivers_are_in_build_up_order_not_by_size():
    snapshot = _snapshot(
        total_material_cost=1.0,
        operations=[
            {"process_type": "drilling", "total_operation_cost": 10.0},
            {"process_type": "milling", "total_operation_cost": 500.0},
            {"process_type": "drilling", "total_operation_cost": 5.0},
        ],
        total_operation_cost=515.0,
        total_part_cost=531.0,
    )
    breakdown = cost_drivers.cost_drivers("x", snapshot)
    assert [driver.key for driver in breakdown.drivers] == [
        "material", "process:drilling", "process:milling", "overhead", "scrap", "tooling"
    ]
    assert breakdown.drivers[1].amount == 15.0
    # Shares as rounded need not sum to 100.00; when they do not, it is said.
    assert breakdown.rounding_note == "" or "not 100.00%" in breakdown.rounding_note


def test_shares_that_do_not_round_to_one_hundred_are_stated_not_nudged():
    snapshot = _snapshot(
        total_material_cost=1.0,
        operations=[{"process_type": "milling", "total_operation_cost": 1.0}],
        total_operation_cost=1.0,
        additional_costs={"overhead_cost": 1.0, "scrap_cost": 0.0, "tooling_cost": 0.0},
        total_part_cost=3.0,
    )
    breakdown = cost_drivers.cost_drivers("x", snapshot)
    assert [driver.share_percent for driver in breakdown.drivers[:3]] == [
        pytest.approx(33.333333, abs=1e-4)
    ] * 3
    dumped = breakdown.model_dump(mode="json")
    assert [driver["share_percent"] for driver in dumped["drivers"][:3]] == [33.33] * 3
    assert dumped["share_total_percent"] == 99.99
    assert "99.99%" in dumped["rounding_note"]


def test_an_estimate_with_no_total_or_no_cost_block_is_not_available():
    no_total = _snapshot()
    del no_total["part_cost"]["total_part_cost"]
    # A nested part cost with no total is still a part estimate, with nothing to break down.
    refused = cost_drivers.cost_drivers("x", no_total)
    assert refused.status == "not_available" and refused.drivers == [] and refused.reason
    assert cost_drivers.cost_drivers("x", {"geometry": {}}).status == "not_available"
    unknown = cost_drivers.cost_drivers("x", None)
    assert unknown.status == "not_available" and unknown.reason


# --------------------------------------------------------------------- assemblies


def cost_assembly(plans=None) -> dict:
    data = {"material": "Steel", "source_unit": "mm", "rate_set_id": "poc-default"}
    if plans is not None:
        data["component_plans"] = json.dumps(plans)
    response = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", assembly_fixtures.pump_assembly(), "application/octet-stream")},
        data=data,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_reviewed_assembly_breaks_down_by_category_and_by_component():
    summary = cost_assembly(assembly_fixtures.committed_plans(client, rate_set_id="poc-default"))
    assert summary["is_complete"] is True
    breakdown = drivers_of(summary["estimate_id"])
    assert breakdown["estimate_kind"] == "assembly" and breakdown["status"] == "available"
    assert breakdown["total"] == pytest.approx(summary["total_cost"], abs=0.005)
    assert_partitions(breakdown, breakdown["total"])
    keys = [driver["key"] for driver in breakdown["drivers"]]
    assert keys[:3] == [
        "manufactured:material", "manufactured:operations", "manufactured:additional"
    ]
    assert keys[3:6] == ["purchased", "standard", "excluded"]

    components = breakdown["component_drivers"]
    costed = [line for line in summary["lines"] if not line["is_assembly"]]
    assert [driver["label"] for driver in components if driver["key"] != "reconciliation"] == [
        line["name"] for line in costed
    ]
    total_cents = cents(breakdown["total"])
    assert sum(cents(d["amount"]) for d in components if d["amount"] is not None) == total_cents


def test_an_unreviewed_assembly_is_marked_partial_with_the_roll_ups_own_reason():
    summary = cost_assembly()
    assert summary["is_complete"] is False
    breakdown = drivers_of(summary["estimate_id"])
    assert breakdown["status"] == "partial"
    assert breakdown["reason"] == summary["completeness_note"]


def test_an_assembly_without_a_total_is_not_available_and_says_why():
    result = {"assembly_name": "A", "total_cost": None, "completeness_note": "Nothing costed."}
    breakdown = cost_drivers.cost_drivers("x", result)
    assert breakdown.status == "not_available" and breakdown.reason == "Nothing costed."
    assert breakdown.drivers == []


def test_an_assembly_in_mixed_currencies_is_not_available():
    result = {"assembly_name": "A", "total_cost": 10.0, "is_aggregatable": False,
              "currency_note": "Two currencies."}
    breakdown = cost_drivers.cost_drivers("x", result)
    assert breakdown.status == "not_available" and breakdown.reason == "Two currencies."


# --------------------------------------------------------------------- boundaries


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_module_imports_money_only_from_the_package():
    package = {name for name in _imports(PACKAGE / "cost_drivers.py") if "cad_costing_agent" in name}
    assert package == {"cad_costing_agent.money"}


def test_only_main_imports_the_module():
    importers = sorted(
        path.name
        for path in PACKAGE.rglob("*.py")
        if path.name != "cost_drivers.py"
        and any(
            name == "cad_costing_agent.cost_drivers"
            or (name == "cad_costing_agent" and "cost_drivers" in path.read_text(encoding="utf-8"))
            for name in _imports(path)
        )
    )
    assert importers == ["main.py"]


def test_the_module_calculates_no_cost_and_ranks_nothing():
    code = code_without_prose(cost_drivers)
    for token in (
        "calculate_", "resolve_", "rate_set", "per_hour", "_minutes", "repository", "session",
        "quotation", "estimates", "sorted(", ".sort(", "max(", "min(", "hotspot", "biggest",
        "largest",
    ):
        assert token not in code, token


def test_the_endpoint_is_read_only():
    routes = [
        route for route in app.routes
        if getattr(route, "path", "") == "/estimates/{estimate_id}/cost-drivers"
    ]
    assert len(routes) == 1 and routes[0].methods == {"GET"}


# --------------------------------------------------------------------- the browser


def test_the_card_is_in_the_cost_section_and_the_script_loads_before_the_router():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    order = re.findall(r'<script src="([\w.]+\.js)"', html)
    assert order.index("app.js") < order.index("drivers.js") < order.index("estimateflow.js")
    assert html.count('id="drivers-card"') == 1
    cost_pane = html[html.index('data-pane="cost"') : html.index('data-pane="assembly"')]
    assert 'id="drivers-card"' in cost_pane


def test_the_script_shows_the_servers_figures_and_works_nothing_out():
    source = (STATIC / "drivers.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))
    assert "/cost-drivers" in code
    assert "escapeHtml(" in code
    # The bar is the server's share passed into CSS, and the share is printed too.
    assert "width: ${driver.share_percent}%" in code
    assert "NUMBER.format(driver.share_percent)" in code
    for token in (
        "sort(", "Math.", "reduce(", "toFixed", "parseFloat", "parseInt", " * ", " / ", " - ",
        "+=", "-=", "biggest", "hotspot", "largest", "localStorage",
    ):
        assert token not in code, token
