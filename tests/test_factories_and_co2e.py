"""Iteration 9: a rate set's factory label and per-machine overhead, and a CO2e estimate.

Overhead: absent leaves every figure (and every snapshot key) as it was; present it
is `(direct + indirect) x run hours`, it reaches the cost through the ordinary
calculator, and a comparison attributes it with nothing unexplained.

CO2e: kilograms, read from a stored estimate's own snapshots against a versioned,
cited catalog. A line with no basis or no factor is `not_estimated` with its
reason, never zero; nothing reaches money.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cad_costing_agent import co2e, emission_factors
from cad_costing_agent.emission_factors import (
    BASIS_NONE_FOUND,
    BASIS_PUBLISHED,
    SEED_EMISSION_CATALOGS,
    EmissionFactorCatalog,
    MaterialFactor,
)
from cad_costing_agent.main import app
from cad_costing_agent.operation import ManufacturingOperation
from cad_costing_agent.operation_costing import calculate_operation_cost
from cad_costing_agent.rate_sets import REGION_CODE_PATTERN, SEED_RATE_SETS, RateSet
from tests import test_stage_costing as stage_tests
from tests.test_manufacturing_plan import code_without_prose
from tests.test_process_classification import FORM, committed_plan, step

client = TestClient(app)
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
STATIC = PACKAGE / "static"

POC_DEFAULT = next(r for r in SEED_RATE_SETS if r.rate_set_id == "poc-default")
FACTORY = {"region": "IN-MH", "factory_name": "Pune Plant 2", "site_notes": "Two shifts."}


# ------------------------------------------------------------------ helpers


def publish(version: str, *, factory=None, direct=None, indirect=None, rate_set_id="test-factory"):
    body = json.loads(POC_DEFAULT.model_dump_json())
    body.pop("rate_set_id")
    body["version"] = version
    body["name"] = "Test factory"
    if factory is not None:
        body["factory"] = factory
    for rate in body["operations"].values():
        if direct is not None:
            rate["direct_overhead_per_hour"] = direct
        if indirect is not None:
            rate["indirect_overhead_per_hour"] = indirect
    return client.post(f"/rates/{rate_set_id}/versions", json=body)


def estimate(rate_set_id: str, version: str | None = None) -> dict:
    name = stage_tests.NAME
    data = {**FORM, "rate_set_id": rate_set_id,
            "manufacturing_plan": committed_plan(name).model_dump_json()}
    if version:
        data["rate_set_version"] = version
    response = client.post(
        "/cost/cad-part",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
        data=data,
    )
    assert response.status_code == 200, response.text
    return response.json()


def stored(estimate_id: str) -> dict:
    return client.get(f"/estimates/{estimate_id}").json()


def without_identity(part_cost: dict) -> dict:
    return {k: v for k, v in part_cost.items() if k not in ("estimate_id", "rate_set")}


# ------------------------------------------------------------------ overhead, unit


def operation(**overhead) -> ManufacturingOperation:
    return ManufacturingOperation(
        operation_id="OP-1", operation_name="Mill", process_type="milling",
        machine_rate_per_hour=1200.0, labor_rate_per_hour=600.0,
        setup_time_minutes=30.0, cycle_time_minutes=6.0, quantity=10, **overhead,
    )


def test_absent_overhead_is_todays_operation_exactly_with_no_new_keys():
    result = calculate_operation_cost(operation())
    dumped = result.model_dump(mode="json")
    # 1 run hour: machine 1200 + labour 600 + setup 0.5 h x 1200.
    assert dumped["total_operation_cost"] == 2400.0
    for key in ("direct_overhead_cost", "indirect_overhead_cost",
                "direct_overhead_per_hour", "indirect_overhead_per_hour"):
        assert key not in dumped
    assert "direct_overhead_per_hour" not in operation().model_dump()
    assert len(dumped["formulas"]) == 4


def test_overhead_is_per_run_hour_and_never_on_setup():
    result = calculate_operation_cost(operation(direct_overhead_per_hour=150.0,
                                                indirect_overhead_per_hour=90.0))
    # Run time 6 min x 10 = 1 h; setup's 30 min carries none.
    assert result.direct_overhead_cost == pytest.approx(150.0)
    assert result.indirect_overhead_cost == pytest.approx(90.0)
    assert result.setup_cost == pytest.approx(600.0)
    assert result.total_operation_cost == pytest.approx(2400.0 + 240.0)
    assert any("direct_overhead_cost" in f for f in result.formulas)


def test_an_overhead_rate_cannot_be_negative():
    with pytest.raises(ValidationError):
        operation(direct_overhead_per_hour=-1.0)


def test_a_factory_region_is_an_iso_code_and_both_patterns_agree():
    assert REGION_CODE_PATTERN == emission_factors.REGION_CODE_PATTERN
    with pytest.raises(ValidationError):
        RateSet(**{**POC_DEFAULT.model_dump(), "factory": {"region": "Pune", "factory_name": "X"}})
    assert "factory" not in POC_DEFAULT.model_dump()


# ------------------------------------------------------------------ overhead, API


def test_a_factory_label_without_overhead_leaves_the_estimate_identical():
    assert publish("1.0.0", factory=FACTORY).status_code == 201
    baseline = stored(estimate("poc-default")["estimate_id"])
    labelled = stored(estimate("test-factory", "1.0.0")["estimate_id"])
    assert without_identity(labelled["result_snapshot"]["part_cost"]) == without_identity(
        baseline["result_snapshot"]["part_cost"]
    )
    assert labelled["total_part_cost"] == baseline["total_part_cost"]
    assert labelled["rate_set_snapshot"]["factory"] == FACTORY
    assert "factory" not in baseline["rate_set_snapshot"]


def test_listing_carries_the_label_only_where_one_was_stated():
    publish("1.0.0", factory=FACTORY)
    rows = client.get("/rates").json()
    mine = next(r for r in rows if r["rate_set_id"] == "test-factory")
    assert (mine["region"], mine["factory_name"]) == ("IN-MH", "Pune Plant 2")
    seeded = next(r for r in rows if r["rate_set_id"] == "poc-default")
    assert "region" not in seeded and "factory_name" not in seeded


def test_a_published_factory_version_is_immutable():
    assert publish("1.0.0", factory=FACTORY).status_code == 201
    assert publish("1.0.0", factory=FACTORY, direct=5.0).status_code == 409
    bad = publish("1.0.1", factory={"region": "Maharashtra", "factory_name": "X"})
    assert bad.status_code == 422


def test_overhead_reaches_the_cost_hand_computed_and_is_attributed_exactly():
    publish("1.0.0", factory=FACTORY)
    publish("1.1.0", factory=FACTORY, direct=120.0, indirect=60.0)
    before = estimate("test-factory", "1.0.0")
    after = estimate("test-factory", "1.1.0")
    before_ops = stored(before["estimate_id"])["result_snapshot"]["part_cost"]["operations"]
    after_ops = stored(after["estimate_id"])["result_snapshot"]["part_cost"]["operations"]
    assert before_ops and len(before_ops) == len(after_ops)
    for op in after_ops:
        # Every committed cycle is 2.0 min at quantity 5: 1/6 h of run time.
        assert op["cycle_time_minutes"] == 2.0 and op["quantity"] == 5
        assert op["direct_overhead_cost"] == pytest.approx(20.0)
        assert op["indirect_overhead_cost"] == pytest.approx(10.0)
    for op in before_ops:
        assert "direct_overhead_cost" not in op
    # poc-default's copy has no overhead %, scrap or tooling, so the whole
    # difference is the overhead: 30.00 per operation.
    expected = 30.0 * len(after_ops)
    delta = stored(after["estimate_id"])["total_part_cost"] - stored(before["estimate_id"])["total_part_cost"]
    assert delta == pytest.approx(expected, abs=0.011)

    comparison = client.get(
        "/estimates/compare",
        params={"baseline": before["estimate_id"], "comparison": after["estimate_id"]},
    ).json()
    categories = {impact["category"]: impact["delta"] for impact in comparison["impacts"]}
    assert categories["machine_overhead_rate"] == pytest.approx(expected, abs=0.011)
    assert "unexplained" not in categories
    assert set(categories) <= {"machine_overhead_rate", "rounding"}

    drivers = client.get(f"/estimates/{after['estimate_id']}/cost-drivers").json()
    parts = [p["label"] for d in drivers["drivers"] for p in d.get("of_which") or []]
    assert "Direct overhead (run)" in parts and "Indirect overhead (run)" in parts
    before_parts = [
        p["label"]
        for d in client.get(f"/estimates/{before['estimate_id']}/cost-drivers").json()["drivers"]
        for p in d.get("of_which") or []
    ]
    assert not any("overhead (run)" in label for label in before_parts)

    quote = client.get(f"/estimates/{after['estimate_id']}/quotation").json()
    lines = {line["category"]: line["amount"] for line in quote["totals"]["lines"]}
    assert lines["operation_machine_overhead"] == pytest.approx(expected, abs=0.011)
    plain = client.get(f"/estimates/{before['estimate_id']}/quotation").json()
    assert "operation_machine_overhead" not in {l["category"] for l in plain["totals"]["lines"]}


# ------------------------------------------------------------------ CO2e, pure


def catalog(**changes) -> EmissionFactorCatalog:
    return SEED_EMISSION_CATALOGS[0].model_copy(update=changes)


SNAPSHOT = {
    "material_basis": "finished_part",
    "part_cost": {
        "material_charged": True,
        "material": {"material": {"name": "Steel"}, "mass_kg": 0.5, "quantity": 4},
        "operations": [
            {"process_type": "milling", "cycle_time_minutes": 3.0, "quantity": 4,
             "setup_time_minutes": 30.0},
            {"process_type": "grinding", "cycle_time_minutes": 1.0, "quantity": 4},
        ],
        "stage_costs": [
            {"stage": "heat_treatment", "label": "Heat treatment", "unit_basis": "per_piece",
             "quantity": 4},
        ],
    },
}


def run(snapshot=SNAPSHOT, region="IN", cat=None):
    return co2e.estimate_co2e(
        estimate_id="e1", result_snapshot=snapshot, catalog=cat or catalog(),
        region=region, region_source="request" if region else None,
    )


def test_hand_computed_lines_and_a_partial_total():
    result = run()
    lines = {line.key: line for line in result.lines}
    # Steel: 0.5 kg x 4 x 1.91 = 3.82.
    assert lines["steel"].kg_co2e == pytest.approx(3.82)
    # Milling: 3 min x 4 = 0.2 run h (setup excluded) x 7.5 kWh = 1.5 kWh x 0.727 = 1.0905.
    assert lines["milling"].run_hours == pytest.approx(0.2)
    assert lines["milling"].kwh == pytest.approx(1.5)
    assert lines["milling"].kg_co2e == pytest.approx(1.0905)
    # Grinding has no electricity factor; heat treatment is per piece.
    assert lines["grinding"].status == co2e.NOT_ESTIMATED and lines["grinding"].kg_co2e is None
    assert "no electricity factor" in lines["grinding"].reason
    assert lines["heat_treatment"].status == co2e.NOT_ESTIMATED
    assert "no mass or time" in lines["heat_treatment"].reason
    assert result.status == co2e.PARTIAL
    assert result.total_kg_co2e == pytest.approx(3.82 + 1.0905)
    assert result.per_unit_kg_co2e == pytest.approx((3.82 + 1.0905) / 4)
    assert result.not_estimated_line_count == 2
    assert any("Partial" in note for note in result.notes)


def test_no_region_leaves_electricity_not_estimated_never_zero():
    result = run(region=None)
    milling = next(line for line in result.lines if line.key == "milling")
    assert milling.status == co2e.NOT_ESTIMATED and milling.kg_co2e is None
    assert milling.kwh == pytest.approx(1.5)
    assert "No region" in milling.reason
    unknown = next(line for line in run(region="FR").lines if line.key == "milling")
    assert "no grid factor for region 'FR'" in unknown.reason


def test_a_material_supplied_per_piece_by_a_stage_has_no_basis():
    snapshot = {"part_cost": {
        "material_charged": False,
        "material": {"material": {"name": "Steel"}, "mass_kg": 0.5, "quantity": 4},
        "operations": [],
        "stage_costs": [{"stage": "purchase", "label": "Purchase", "unit_basis": "per_piece",
                         "quantity": 4, "supplies_material": True}],
    }}
    result = run(snapshot)
    material = result.lines[0]
    assert material.status == co2e.NOT_ESTIMATED and "per piece" in material.reason
    assert result.status == co2e.NONE_ESTIMATED and result.total_kg_co2e is None


def test_a_cast_blank_uses_the_stage_mass_and_a_stage_factor_when_one_exists():
    snapshot = {"part_cost": {
        "material_charged": False,
        "material": {"material": {"name": "Aluminium"}, "mass_kg": 0.3, "quantity": 2},
        "operations": [],
        "stage_costs": [{"stage": "casting", "label": "Casting", "unit_basis": "per_kg",
                         "mass_kg": 0.8, "quantity": 2, "supplies_material": True,
                         "mass_source": "entered"}],
    }}
    cat = catalog(stage_factors={"casting": emission_factors.StageFactor(
        kg_co2e_per_kg=1.5, basis=BASIS_NONE_FOUND)})
    result = run(snapshot, cat=cat)
    material, stage = result.lines
    # The poured mass, not the finished 0.3 kg: 0.8 x 2 x 14.8 = 23.68; stage 0.8 x 2 x 1.5 = 2.4.
    assert material.kg_co2e == pytest.approx(23.68) and material.mass_source.startswith("stage:casting")
    assert stage.kg_co2e == pytest.approx(2.4)
    assert result.status == co2e.COMPLETE


def test_an_assembly_roll_up_is_not_estimated_with_a_reason():
    result = run({"lines": []})
    assert result.status == co2e.NONE_ESTIMATED and result.lines == [] and result.notes


def test_deterministic():
    assert run().model_dump_json() == run().model_dump_json()


def test_a_published_factor_must_cite_title_url_and_quote():
    with pytest.raises(ValidationError):
        MaterialFactor(kg_co2e_per_kg=2.0, basis=BASIS_PUBLISHED, source_title="T",
                       source_url="https://example.org")
    with pytest.raises(ValidationError):
        MaterialFactor(kg_co2e_per_kg=2.0, basis="ecoinvent")
    MaterialFactor(kg_co2e_per_kg=2.0, basis=BASIS_NONE_FOUND)


def test_every_seeded_factor_is_cited_or_says_none_was_found_and_the_catalog_is_illustrative():
    seed = SEED_EMISSION_CATALOGS[0]
    assert seed.is_illustrative and "ILLUSTRATIVE" in seed.description
    for table in (seed.materials, seed.process_electricity, seed.grid):
        for factor in table.values():
            assert factor.basis in (BASIS_PUBLISHED, BASIS_NONE_FOUND)
            if factor.basis == BASIS_PUBLISHED:
                assert factor.source_url.startswith("https://") and factor.source_quote


# ------------------------------------------------------------------ CO2e, API


def test_the_estimate_endpoint_hand_check_and_region_from_the_rate_set():
    publish("1.0.0", factory={"region": "IN", "factory_name": "Pune"})
    made = estimate("test-factory", "1.0.0")
    snapshot = stored(made["estimate_id"])["result_snapshot"]["part_cost"]
    response = client.get(f"/estimates/{made['estimate_id']}/co2e")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["region"] == "IN" and result["region_source"] == "rate_set_factory"
    material = result["lines"][0]
    expected_material = snapshot["material"]["mass_kg"] * snapshot["material"]["quantity"] * 1.91
    assert material["kg_co2e"] == pytest.approx(expected_material, abs=0.0006)
    # Every committed operation here is 2 min x 5; per process, hours x kWh x 0.727.
    draw = SEED_EMISSION_CATALOGS[0].process_electricity
    for line in result["lines"][1:]:
        count = sum(1 for op in snapshot["operations"] if op["process_type"] == line["key"])
        assert line["kg_co2e"] == pytest.approx(
            count * (10 / 60) * draw[line["key"]].kwh_per_run_hour * 0.727, abs=0.0006
        )
    gb = client.get(f"/estimates/{made['estimate_id']}/co2e", params={"region": "GB"}).json()
    assert gb["region_source"] == "request" and gb["lines"][1]["grid_factor_kg_co2e_per_kwh"] == 0.20705
    # The cost is untouched by asking.
    assert stored(made["estimate_id"])["total_part_cost"] == made["part_cost"]["total_part_cost"]
    # A rate set without a factory states no region.
    plain = client.get(f"/estimates/{estimate('poc-default')['estimate_id']}/co2e").json()
    assert plain["region"] is None and plain["status"] == "partial"
    assert client.get(f"/estimates/{made['estimate_id']}/co2e", params={"region": "india"}).status_code == 422
    assert client.get("/estimates/nope/co2e").status_code == 404


def test_catalogs_publish_and_are_immutable():
    listing = client.get("/emission-factors").json()
    assert [row["emission_catalog_id"] for row in listing] == ["illustrative-co2e"]
    assert listing[0]["regions"] == ["GB", "IN"]
    body = {"name": "Mine", "version": "1.0.0",
            "materials": {"steel": {"kg_co2e_per_kg": 2.0, "basis": BASIS_PUBLISHED,
                                    "source_title": "T", "source_url": "https://x.org"}}}
    assert client.post("/emission-factors/mine/versions", json=body).status_code == 422
    body["materials"]["steel"]["source_quote"] = "quoted"
    assert client.post("/emission-factors/mine/versions", json=body).status_code == 201
    assert client.post("/emission-factors/mine/versions", json=body).status_code == 409
    assert client.get("/emission-factors/mine").json()["materials"]["steel"]["source_quote"] == "quoted"


# ------------------------------------------------------------------ structure


def _package_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("cad_costing_agent"):
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name.startswith("cad_costing_agent"))
    return names


def test_co2e_imports_only_the_catalog_and_the_catalog_imports_nothing():
    assert {n for n in _package_imports(PACKAGE / "co2e.py") if n.count(".") == 1} == {
        "cad_costing_agent.emission_factors"
    }
    assert _package_imports(PACKAGE / "emission_factors.py") == set()


def test_only_main_imports_the_co2e_module():
    importers = [
        path.name
        for path in PACKAGE.rglob("*.py")
        if path.name != "co2e.py"
        and "cad_costing_agent.co2e" in _package_imports(path)
    ]
    assert importers == ["main.py"]


def test_co2e_holds_no_money_and_no_rate():
    code = code_without_prose(co2e)
    # Precise tokens: the user-facing disclaimer says "not money" in words.
    for token in ("cad_costing_agent.money", "import money", "round_money", "rate_set_id",
                  "cost_per_kg", "machine_rate", "total_part_cost"):
        assert token not in code, token


def test_the_card_renders_the_servers_figures_and_works_nothing_out():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    order = re.findall(r'<script src="([\w.]+\.js)"', html)
    assert order.index("drivers.js") < order.index("co2e.js") < order.index("estimateflow.js")
    cost_pane = html[html.index('data-pane="cost"') : html.index('data-pane="assembly"')]
    assert 'id="co2e-card"' in cost_pane
    source = (STATIC / "co2e.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))
    assert "/co2e" in code
    for token in ("sort(", "Math.", "reduce(", "toFixed", "parseFloat", " * ", " / ", " - ",
                  "+=", "-=", "money("):
        assert token not in code, token
