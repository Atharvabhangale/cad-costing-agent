"""Sheet blank nesting, utilisation, and the sheet share as a confirmed material basis.

What is checked, with every expected figure counted by hand beside it:

- the grid nest — parts per sheet in both orientations, the estimate's choice
  (more parts, 0° on a tie), the utilisation exactly, a blank too big refused;
- the sheet share follows the bar-stock contract — proposed, unconfirmed it
  changes nothing at all, confirmed it costs material from thickness x sheet
  area / parts per sheet while `mass` stays the finished part's own;
- the engineer's own parts per sheet sits beside the estimate's, and a count
  the sheet cannot hold is refused;
- the contradictions refused: a confirmed bar and sheet together, a sheet
  share on a route whose first stage supplies the material, a sheet share on
  a part `/cad/sheet-metal` does not call a sheet, a share for another blank;
- assumptions, quotation, comparison (nothing unexplained) and the cost-driver
  note read it back; determinism; the module boundaries and the browser.
"""

from __future__ import annotations

import ast
import json
import math
import re
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.cad_part_cost import calculate_cad_part_cost
from cad_costing_agent.main import app
from cad_costing_agent.raw_stock import record_sheet_stock_decision
from cad_costing_agent.sheet_nesting import SheetSize, nest_blank
from tests import sheet_fixtures, step_fixtures
from tests.step_fixtures import PROCESS_FIXTURES

client = TestClient(app)
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
STATIC = PACKAGE / "static"

PLATE = "flat_plate_two_holes"  # 100 x 60 x 2, two Ø10 holes
BRACKET = "l_bracket"  # 40 wide, 50 + 30 flanges, r 3, t 2
SHEET = {"sheet_length_mm": 1000.0, "sheet_width_mm": 500.0, "spacing_mm": 5.0, "edge_margin_mm": 10.0}
PLATE_NET_AREA = 6000.0 - 2 * math.pi * 5.0**2
FORM = {
    "material": "Steel",
    "source_unit": "mm",
    "part_id": "P-NEST",
    "part_name": "Nested plate",
    "quantity": "10",
    "rate_set_id": "poc-default",
}


@lru_cache(maxsize=None)
def step(name: str) -> bytes:
    if name in sheet_fixtures.SHEET_FIXTURES:
        return sheet_fixtures.SHEET_FIXTURES[name]()
    return (PROCESS_FIXTURES.get(name) or getattr(step_fixtures, name))()


def measure(name: str = PLATE, k: float | None = 0.4) -> dict:
    response = client.post(
        "/cad/sheet-metal",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
        data={} if k is None else {"k_factor": str(k)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def propose(name: str = PLATE, sheet: dict = SHEET, k: float | None = 0.4):
    return client.post("/cad/sheet-stock/propose", json={"analysis": measure(name, k), **sheet})


def proposed(name: str = PLATE, sheet: dict = SHEET) -> dict:
    response = propose(name, sheet)
    assert response.status_code == 200, response.text
    return response.json()


def decide(stock: dict, action: str, **extra):
    return client.post("/cad/sheet-stock/decide", json={"stock": stock, "action": action, **extra})


def confirmed(name: str = PLATE, sheet: dict = SHEET) -> dict:
    response = decide(proposed(name, sheet), "confirm")
    assert response.status_code == 200, response.text
    return response.json()


def cost(name: str = PLATE, *, sheet_stock=None, raw_stock=None, **extra):
    data = {**FORM, **extra}
    if sheet_stock is not None:
        data["sheet_stock"] = sheet_stock if isinstance(sheet_stock, str) else json.dumps(sheet_stock)
    if raw_stock is not None:
        data["raw_stock"] = json.dumps(raw_stock)
    return client.post(
        "/cost/cad-part",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
        data=data,
    )


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def without_ids(body: dict) -> dict:
    """The result with its per-call estimate ids removed, as every determinism test does."""
    body = json.loads(json.dumps(body))
    body.pop("estimate_id", None)
    body["part_cost"].pop("estimate_id", None)
    return body


# --- 1. the grid nest, counted by hand -----------------------------------------------


def test_a_flat_plate_nests_sixty_three_to_the_sheet_at_zero_degrees():
    stock = proposed()
    nest = stock["nest"]
    zero, ninety = nest["orientations"]
    # Usable 980 x 480. 0°: floor(985 / 105) = 9 along, floor(485 / 65) = 7 across.
    assert (zero["orientation"], zero["count_along"], zero["count_across"]) == ("0", 9, 7)
    assert zero["parts_per_sheet"] == 63
    # 90°: the 60 along: floor(985 / 65) = 15; the 100 across: floor(485 / 105) = 4.
    assert (ninety["count_along"], ninety["count_across"], ninety["parts_per_sheet"]) == (15, 4, 60)
    assert nest["chosen_orientation"] == "0" and nest["parts_per_sheet"] == 63
    # (6000 - 50π) mm² x 63 / 500,000 mm².
    assert nest["utilisation_percent"] == pytest.approx(100 * PLATE_NET_AREA * 63 / 500000.0, abs=1e-5)
    assert stock["utilisation_percent"] == pytest.approx(73.6208, abs=1e-4)
    assert stock["confirmed"] is False and stock["source"] == "system"
    # 2 mm x 500,000 mm² / 63.
    assert stock["volume_mm3"] == pytest.approx(2.0 * 500000.0 / 63, abs=1e-3)
    assert stock["description"] == "2.0 mm sheet 1000 × 500, 63 per sheet, 73.6% utilisation"
    assert "estimate's choice" in nest["choice_note"]
    assert any("bounding-rectangle grid" in line for line in nest["limitations"])
    assert any("remnant" in line for line in nest["limitations"])


def test_an_l_bracket_blank_nests_one_hundred_and_five_to_the_sheet():
    stock = proposed(BRACKET)
    nest = stock["nest"]
    # Flat 40 x (80 + (π/2)(3 + 0.8)) = 40 x 85.969026.
    assert nest["blank_length_mm"] == 40.0
    assert nest["blank_width_mm"] == pytest.approx(80 + (math.pi / 2) * 3.8, abs=1e-6)
    zero, ninety = nest["orientations"]
    # 0°: floor(985 / 45) = 21 along, floor(485 / 90.969) = 5 across.
    assert (zero["count_along"], zero["count_across"], zero["parts_per_sheet"]) == (21, 5, 105)
    # 90°: floor(985 / 90.969) = 10 along, floor(485 / 45) = 10 across.
    assert (ninety["count_along"], ninety["count_across"], ninety["parts_per_sheet"]) == (10, 10, 100)
    assert nest["parts_per_sheet"] == 105 and nest["chosen_orientation"] == "0"
    assert nest["utilisation_percent"] == pytest.approx(
        100 * nest["blank_net_area_mm2"] * 105 / 500000.0, abs=1e-6
    )


def test_a_tie_keeps_zero_degrees_and_an_exact_fit_is_not_lost_to_rounding():
    # 1000 x 1000, no spacing or margin: 10 x 16 either way, and 100 x 10 fills exactly.
    nest = nest_blank(100.0, 60.0, 6000.0, SheetSize(
        sheet_length_mm=1000.0, sheet_width_mm=1000.0, spacing_mm=0.0, edge_margin_mm=0.0))
    assert [o.parts_per_sheet for o in nest.orientations] == [160, 160]
    assert nest.chosen_orientation == "0"
    # 960 x 600 holds exactly 9 x 10 = 90 of them, never 80 for a floating-point hair.
    nest = nest_blank(960.0 / 9, 60.0, 6400.0, SheetSize(
        sheet_length_mm=960.0, sheet_width_mm=600.0, spacing_mm=0.0, edge_margin_mm=0.0))
    assert nest.orientations[0].parts_per_sheet == 90


def test_the_orientation_with_more_parts_wins():
    # A 300 x 100 blank on 1000 x 350: 0° gives 3 x 3 = 9; 90° gives 10 x 1 = 10.
    nest = nest_blank(300.0, 100.0, 30000.0, SheetSize(
        sheet_length_mm=1000.0, sheet_width_mm=350.0, spacing_mm=0.0, edge_margin_mm=0.0))
    assert [o.parts_per_sheet for o in nest.orientations] == [9, 10]
    assert nest.chosen_orientation == "90" and nest.parts_per_sheet == 10


def test_a_blank_too_big_for_the_sheet_is_refused_with_the_reason():
    response = propose(sheet={**SHEET, "sheet_length_mm": 90.0, "sheet_width_mm": 90.0})
    assert response.status_code == 400
    assert "does not fit" in response.json()["detail"]
    assert "either orientation" in response.json()["detail"]
    margin = propose(sheet={**SHEET, "edge_margin_mm": 250.0})
    assert margin.status_code == 400 and "leaves nothing" in margin.json()["detail"]


def test_no_sheet_input_is_assumed():
    response = client.post(
        "/cad/sheet-stock/propose",
        json={"analysis": measure(), "sheet_length_mm": 1000.0, "sheet_width_mm": 500.0},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "needs_sheet_input"
    assert detail["needs_input"] == ["spacing_mm", "edge_margin_mm"]
    negative = propose(sheet={**SHEET, "spacing_mm": -1.0})
    assert negative.status_code == 400


def test_no_k_factor_and_not_a_sheet_are_refused():
    no_k = propose(k=None)
    assert no_k.status_code == 400 and "no flat pattern" in no_k.json()["detail"]
    block = propose("thick_block")
    assert block.status_code == 400 and "calls a sheet" in block.json()["detail"]


# --- 2. the decision: the bar-stock contract ------------------------------------------


def test_the_engineers_parts_per_sheet_sits_beside_the_estimates():
    edited = ok(decide(proposed(), "edit", parts_per_sheet=70))
    assert edited["confirmed"] and edited["source"] == "user"
    assert (edited["parts_per_sheet"], edited["system_parts_per_sheet"]) == (70, 63)
    assert edited["nest"]["parts_per_sheet"] == 63  # the estimate is never changed
    assert edited["volume_mm3"] == pytest.approx(2.0 * 500000.0 / 70, abs=1e-3)
    assert edited["description"].endswith("70 per sheet (entered), 81.8% utilisation")
    same = ok(decide(proposed(), "edit", parts_per_sheet=63))
    assert same["source"] == "system" and same["user_parts_per_sheet"] is None
    withdrawn = ok(decide(edited, "withdraw"))
    assert withdrawn["confirmed"] is False and withdrawn["user_parts_per_sheet"] is None


def test_more_parts_than_the_sheet_holds_is_refused():
    # 86 x 5842.92 mm² = 502,491 mm² > 500,000 mm².
    response = decide(proposed(), "edit", parts_per_sheet=86)
    assert response.status_code == 400 and "need more than" in response.json()["detail"]
    assert decide(proposed(), "edit", parts_per_sheet=2.5).status_code == 400
    assert decide(proposed(), "guess").status_code == 400


def test_an_unconfirmed_sheet_share_changes_nothing_at_all():
    plain, with_proposal = ok(cost()), ok(cost(sheet_stock=proposed()))
    assert without_ids(with_proposal) == without_ids(plain)
    assert plain["material_basis"] == "finished_part"


def test_a_confirmed_sheet_share_costs_material_from_the_sheet_and_keeps_the_parts_mass():
    stock = confirmed()
    plain, from_sheet = ok(cost()), ok(cost(sheet_stock=stock))

    assert from_sheet["material_basis"] == "sheet_stock"
    assert from_sheet["mass"] == plain["mass"]  # the finished part is unchanged
    material = from_sheet["part_cost"]["material"]
    density = material["material"]["density_kg_m3"]
    per_kg = material["material_cost_per_kg"]
    share_mm3 = round(2.0 * 1000.0 * 500.0 / 63, 3)  # 15873.016
    assert from_sheet["stock_mass"]["source_volume"] == share_mm3
    share_kg = share_mm3 * 1e-9 * density
    assert from_sheet["stock_mass"]["mass_kg"] == pytest.approx(share_kg, rel=1e-12)
    assert material["mass_kg"] == pytest.approx(share_kg, rel=1e-12)
    assert from_sheet["part_cost"]["total_material_cost"] == pytest.approx(
        round(share_kg * per_kg * 10, 2), abs=0.005
    )
    # More than the finished part: the spacing, the margin, the holes and the skeleton.
    assert from_sheet["part_cost"]["total_material_cost"] > plain["part_cost"]["total_material_cost"]

    stored = client.get(f"/estimates/{from_sheet['estimate_id']}").json()
    assert stored["request_snapshot"]["sheet_stock"]["confirmed"] is True
    assert stored["request_snapshot"]["sheet_stock"]["parts_per_sheet"] == 63
    assert "sheet_stock" not in client.get(f"/estimates/{plain['estimate_id']}").json()["request_snapshot"]


def test_the_engineers_count_is_what_is_costed():
    edited = ok(decide(proposed(), "edit", parts_per_sheet=70))
    body = ok(cost(sheet_stock=edited))
    assert body["stock_mass"]["source_volume"] == round(2.0 * 500000.0 / 70, 3)


# --- 3. contradictions --------------------------------------------------------------


def test_a_confirmed_bar_and_a_confirmed_sheet_are_refused_together():
    bar = client.post(
        "/cad/operations/suggest",
        files={"file": ("s.step", step("stepped_shaft"), "application/octet-stream")},
    ).json()["raw_stock"]
    bar = ok(client.post("/cad/stock/decide", json={"stock": bar, "action": "confirm"}))
    response = cost(sheet_stock=confirmed(), raw_stock=bar)
    assert response.status_code == 400
    assert "Both a bar and a sheet share" in response.json()["detail"]
    # Unconfirmed, the sheet share is only recorded, so the bar alone is used.
    unconfirmed_sheet = cost("stepped_shaft", sheet_stock=proposed(), raw_stock=bar)
    assert ok(unconfirmed_sheet)["material_basis"] == "bar_stock"


def test_a_sheet_share_on_a_part_that_is_not_a_sheet_is_refused():
    response = cost("thick_block", sheet_stock=confirmed())
    assert response.status_code == 400
    assert "not one /cad/sheet-metal calls a sheet" in response.json()["detail"]


def test_a_sheet_share_proposed_for_another_blank_is_refused_as_stale():
    response = cost(BRACKET, sheet_stock=confirmed(PLATE))
    assert response.status_code == 400
    assert "Propose and confirm it again" in response.json()["detail"]
    tampered = confirmed()
    tampered["nest"]["parts_per_sheet"] = 80
    assert cost(sheet_stock=tampered).status_code == 400
    assert cost(sheet_stock="{not json").status_code == 400


def test_a_sheet_share_on_a_route_that_supplies_its_own_material_is_refused():
    from tests.test_process_classification import decide as decide_route
    from tests.test_stage_costing import STAGE_RATES, publish

    assert publish(STAGE_RATES).status_code == 201
    suggestion = client.post(
        "/cad/operations/suggest",
        files={"file": (f"{PLATE}.step", step(PLATE), "application/octet-stream")},
    ).json()["process_classification"]
    casting = ok(decide_route(suggestion, "edited", "casting_machining"))
    response = cost(
        sheet_stock=confirmed(),
        rate_set_id="client-shop",
        process_classification=json.dumps(casting),
        stage_inputs=json.dumps({"casting": {"mass_kg": 0.2}}),
    )
    assert response.status_code == 400, response.text
    assert "sheet share was confirmed" in response.json()["detail"]
    assert "rate includes the material" in response.json()["detail"]


def test_the_calculator_names_the_stock_it_refuses():
    from cad_costing_agent.cad_geometry import extract_geometry_from_bytes

    geometry = extract_geometry_from_bytes(step(PLATE), "step")
    with pytest.raises(ValueError, match="A sheet share was confirmed"):
        calculate_cad_part_cost(
            geometry, "Steel", "mm", "p", "p", 1,
            material_stock_volume_mm3=20000.0, material_supplied_by="casting",
            material_stock_basis="sheet_stock",
        )
    with pytest.raises(ValueError, match="Unknown material stock basis"):
        calculate_cad_part_cost(geometry, "Steel", "mm", "p", "p", 1, material_stock_basis="plate")


# --- 4. read back: assumptions, quotation, comparison, drivers -----------------------


def test_the_assumptions_and_quotation_name_the_sheet_beside_the_parts_own_mass():
    body = ok(cost(sheet_stock=confirmed()))
    assumptions = client.get(f"/estimates/{body['estimate_id']}/assumptions").json()
    assert assumptions["mass_kg"] == body["mass"]["mass_kg"]
    assert assumptions["material_basis"] == "sheet_stock"
    assert assumptions["stock_mass_kg"] == body["stock_mass"]["mass_kg"]
    assert assumptions["stock_description"] == "2.0 mm sheet 1000 × 500, 63 per sheet, 73.6% utilisation"
    provenance = client.get(f"/estimates/{body['estimate_id']}/quotation").json()["provenance"]
    assert provenance["mass_kg"] == body["mass"]["mass_kg"]
    assert provenance["stock_mass_kg"] == body["stock_mass"]["mass_kg"]
    assert provenance["material_basis"] == "sheet_stock"
    assert provenance["stock_description"] == assumptions["stock_description"]


def _compare(a: str, b: str) -> dict:
    return client.get(f"/estimates/compare?baseline={a}&comparison={b}").json()


def test_a_comparison_attributes_the_switch_to_the_sheet_with_nothing_unexplained():
    a = ok(cost())["estimate_id"]
    b = ok(cost(sheet_stock=confirmed()))["estimate_id"]
    comparison = _compare(a, b)
    changed = {change["field"] for change in comparison["changes"]}
    assert {"material_basis", "stock_mass_kg", "stock_description"} <= changed
    assert "mass_kg" not in changed and "volume_mm3" not in changed
    categories = {impact["category"] for impact in comparison["impacts"]}
    assert "unexplained" not in categories
    explained = sum(impact["delta"] for impact in comparison["impacts"])
    assert explained == pytest.approx(comparison["total_part_cost"]["delta"], abs=0.011)
    material = [i for i in comparison["impacts"] if i["category"] == "material_quantity"]
    assert material and material[0]["delta"] > 0


def test_a_comparison_between_two_sheet_counts_is_explained_too():
    edited = ok(decide(proposed(), "edit", parts_per_sheet=70))
    a = ok(cost(sheet_stock=confirmed()))["estimate_id"]
    b = ok(cost(sheet_stock=edited))["estimate_id"]
    comparison = _compare(a, b)
    changed = {change["field"] for change in comparison["changes"]}
    assert {"stock_mass_kg", "stock_description"} <= changed and "material_basis" not in changed
    assert "unexplained" not in {impact["category"] for impact in comparison["impacts"]}
    assert sum(i["delta"] for i in comparison["impacts"]) == pytest.approx(
        comparison["total_part_cost"]["delta"], abs=0.011
    )


def test_the_cost_driver_table_carries_the_sheet_basis_on_the_material_line():
    body = ok(cost(sheet_stock=confirmed()))
    breakdown = client.get(f"/estimates/{body['estimate_id']}/cost-drivers").json()
    material = breakdown["drivers"][0]
    assert material["key"] == "material"
    assert material["amount"] == body["part_cost"]["total_material_cost"]
    assert "confirmed sheet share" in material["note"]
    assert "63 per sheet, 73.6% utilisation" in material["note"]


def test_previews_take_the_sheet_share_too():
    stock = json.dumps(confirmed())
    estimate = ok(cost(sheet_stock=stock))
    matrix = client.post(
        "/cost/matrix-preview",
        files={"file": (f"{PLATE}.step", step(PLATE), "application/octet-stream")},
        data={**{k: v for k, v in FORM.items() if k not in ("quantity", "rate_set_id")},
              "rate_sets": json.dumps([{"rate_set_id": "poc-default"}]),
              "quantities": json.dumps([10]), "sheet_stock": stock},
    )
    assert matrix.status_code == 200, matrix.text
    cell = matrix.json()["rows"][0]["cells"][0]
    assert cell["total_part_cost"] == estimate["part_cost"]["total_part_cost"]


# --- 5. determinism and boundaries -------------------------------------------------


def test_proposing_and_costing_are_deterministic():
    assert proposed() == proposed()
    one, two = ok(cost(sheet_stock=confirmed())), ok(cost(sheet_stock=confirmed()))
    assert without_ids(one) == without_ids(two)


def test_the_record_function_never_changes_the_nest():
    from cad_costing_agent.raw_stock import SheetStock

    stock = SheetStock.model_validate(proposed())
    edited = record_sheet_stock_decision(stock, "edit", parts_per_sheet=40)
    assert edited.nest == stock.nest


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _code_without_prose(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_the_nest_imports_nothing_from_the_package_and_reads_no_money():
    assert {m for m in _imports(PACKAGE / "sheet_nesting.py") if m.startswith("cad_costing_agent")} == set()
    code = _code_without_prose(PACKAGE / "sheet_nesting.py")
    for forbidden in ("density", "cost", "price", "rate_set", "RateSet"):
        assert forbidden not in code
    readers = {p.name for p in PACKAGE.rglob("*.py") if "cad_costing_agent.sheet_nesting" in _imports(p)}
    assert readers == {"raw_stock.py", "main.py"}


def test_the_stock_module_still_imports_no_costing():
    own = {m for m in _imports(PACKAGE / "raw_stock.py") if m.startswith("cad_costing_agent")}
    assert own == {
        "cad_costing_agent.features.model",
        "cad_costing_agent.features.rotational",
        "cad_costing_agent.features.sheet_metal",
        "cad_costing_agent.sheet_nesting",
    }


def _js_code(name: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", (STATIC / name).read_text(encoding="utf-8"), flags=re.S)
    return "\n".join(line for line in source.split("\n") if not line.strip().startswith("//"))


def test_the_sheet_card_shows_the_servers_nest_and_works_nothing_out():
    code = _js_code("sheet.js")
    assert "/cad/sheet-stock/propose" in code and "/cad/sheet-stock/decide" in code
    for forbidden in ("Math.", " * ", "_mm2 /", "_mm /", "_mm3 /", "parts_per_sheet /", "/ stock.", "/ nest."):
        assert forbidden not in code
    assert not re.search(r"^(?:async )?function ", code, re.M)
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    for element in ("sheet-length", "sheet-width", "sheet-spacing", "sheet-margin",
                    "sheet-nest", "sheet-stock-confirm", "sheet-stock-withdraw"):
        assert f'id="{element}"' in markup
    # The cost request and both previews carry the sheet share.
    for name in ("app.js", "routecompare.js", "matrix.js"):
        assert 'body.append("sheet_stock", JSON.stringify(state.sheetStock))' in _js_code(name)


def test_the_material_line_names_the_sheet_as_it_names_the_bar():
    for name in ("app.js", "impact.js", "quotation.js"):
        assert '"sheet_stock"' in _js_code(name), name
