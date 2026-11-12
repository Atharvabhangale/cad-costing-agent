"""Matrix costing preview: one part under several rate sets at several quantities.

What is checked is the shape of the honesty. Every cell goes through the one pricing
path with the plan exactly as sent, so its figure is the one `/cost/cad-part` would
give for that rate set and quantity; a cell that cannot be priced carries the reason
and no figure; nothing is stored; nothing is ordered by cost or chosen; and currencies
are never converted.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent import main, matrix_preview
from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import build_plan
from cad_costing_agent.features import recognize_features_from_bytes
from cad_costing_agent.operation_suggestions import suggest_operations
from tests.test_process_classification import FORM, committed_plan, step

client = TestClient(app)
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
STATIC = PACKAGE / "static"
NAME = "block_with_counterbore"  # milling + drilling: priced by every seeded rate set
TURNED = "stepped_shaft"  # turning: poc-machine-shop has no rate for it

BASE = {key: value for key, value in FORM.items() if key not in ("quantity", "rate_set_id")}
DEFAULT = {"rate_set_id": "poc-default"}
SHOP_1 = {"rate_set_id": "poc-machine-shop", "version": "1.0.0"}
SHOP_LATEST = {"rate_set_id": "poc-machine-shop"}


def matrix(name=NAME, *, rate_sets=(DEFAULT, SHOP_1), quantities=(1, 10), plan="committed", **extra):
    # A string is sent as it is, so a malformed list can be tried.
    data = {
        **BASE,
        "rate_sets": rate_sets if isinstance(rate_sets, str) else json.dumps(list(rate_sets)),
        "quantities": quantities if isinstance(quantities, str) else json.dumps(list(quantities)),
    }
    if plan == "committed":
        data["manufacturing_plan"] = committed_plan(name).model_dump_json()
    elif plan is not None:
        data["manufacturing_plan"] = plan
    data.update(extra)
    return client.post(
        "/cost/matrix-preview",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
        data=data,
    )


def cost(name, rate_set, quantity, *, plan="committed"):
    data = {**BASE, "quantity": str(quantity), "rate_set_id": rate_set["rate_set_id"]}
    if rate_set.get("version"):
        data["rate_set_version"] = rate_set["version"]
    if plan == "committed":
        data["manufacturing_plan"] = committed_plan(name).model_dump_json()
    elif plan is not None:
        data["manufacturing_plan"] = plan
    return client.post(
        "/cost/cad-part",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
        data=data,
    )


def unaccepted_plan(name: str) -> str:
    """The plan as the system builds it: every time estimated, none accepted."""
    recognition = recognize_features_from_bytes(step(name), f"{name}.step")
    suggestions = suggest_operations(recognition)
    plan = build_plan(suggestions.suggestions, units=suggestions.units, source_filename=f"{name}.step")
    assert any(o.system_suggested_cycle_time is not None for o in plan.operations)
    assert not any(o.system_time_accepted for o in plan.operations)
    return plan.model_dump_json()


def estimate_count() -> int:
    return client.get("/estimates").json()["total"]


def publish_usd():
    base = client.get("/rates/poc-default").json()
    body = {
        "rate_set_id": "usd-shop",
        "name": "Dollar shop",
        "version": "1.0.0",
        "currency": "USD",
        "is_illustrative": False,
        "materials": base["materials"],
        "operations": base["operations"],
        "additional_costs": base["additional_costs"],
    }
    response = client.post("/rates", json=body)
    assert response.status_code == 201, response.text


FIGURES = (
    "total_part_cost", "per_unit_cost", "total_material_cost", "total_operation_cost",
    "total_stage_cost", "direct_cost_base", "overhead_cost", "scrap_cost", "tooling_cost",
    "total_additional_cost",
)


# --- 1. the grid, in the caller's order ---------------------------------------


def test_rows_are_the_rate_sets_and_cells_the_quantities_in_the_order_listed():
    response = matrix(rate_sets=(SHOP_1, DEFAULT, SHOP_LATEST), quantities=(50, 1, 7))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quantities"] == [50, 1, 7]
    assert [(r["rate_set"]["rate_set_id"], r["rate_set"]["version"]) for r in body["rows"]] == [
        ("poc-machine-shop", "1.0.0"),
        ("poc-default", client.get("/rates/poc-default").json()["version"]),
        ("poc-machine-shop", "2.0.0"),
    ]
    for row in body["rows"]:
        assert [cell["quantity"] for cell in row["cells"]] == [50, 1, 7]
        assert all(cell["status"] == "priced" for cell in row["cells"])


def test_a_cell_names_its_rate_set_currency_and_whether_it_is_illustrative():
    body = matrix().json()
    cell = body["rows"][1]["cells"][0]
    stored = client.get("/rates/poc-machine-shop", params={"version": "1.0.0"}).json()
    assert cell["rate_set"]["resolved"] is True
    assert cell["rate_set"]["name"] == stored["name"]
    assert cell["rate_set"]["version"] == "1.0.0"
    assert cell["rate_set"]["currency"] == stored["currency"] == cell["currency"]
    assert cell["rate_set"]["is_illustrative"] is stored["is_illustrative"] is True
    # "Latest" is reported as the version it resolved to, and as having been asked for as latest.
    latest = matrix(rate_sets=(SHOP_LATEST,), quantities=(1,)).json()["rows"][0]["rate_set"]
    assert latest["requested_version"] is None and latest["version"] == "2.0.0"


def test_the_order_does_not_follow_the_cost():
    forward = matrix(rate_sets=(DEFAULT, SHOP_1), quantities=(1, 100)).json()
    backward = matrix(rate_sets=(SHOP_1, DEFAULT), quantities=(100, 1)).json()
    assert [r["rate_set"]["rate_set_id"] for r in forward["rows"]] == ["poc-default", "poc-machine-shop"]
    assert [r["rate_set"]["rate_set_id"] for r in backward["rows"]] == ["poc-machine-shop", "poc-default"]
    assert backward["quantities"] == [100, 1]
    # The same cell, wherever it was put.
    assert forward["rows"][0]["cells"][1] == backward["rows"][1]["cells"][0]
    assert (
        forward["rows"][0]["cells"][0]["total_part_cost"]
        != forward["rows"][1]["cells"][0]["total_part_cost"]
    )


# --- 2. one pricing path ------------------------------------------------------


def test_every_cell_is_exactly_what_cost_cad_part_gives_and_nothing_is_stored():
    rate_sets = (DEFAULT, SHOP_1, SHOP_LATEST)
    quantities = (1, 7, 250)
    before = estimate_count()
    body = matrix(rate_sets=rate_sets, quantities=quantities).json()
    assert estimate_count() == before
    assert body["kind"] == "preview" and body["stored"] is False
    assert "estimate_id" not in json.dumps(body)

    for row, rate_set in zip(body["rows"], rate_sets, strict=True):
        for cell, quantity in zip(row["cells"], quantities, strict=True):
            stored = cost(NAME, rate_set, quantity)
            assert stored.status_code == 200, stored.text
            part = stored.json()["part_cost"]
            where = (rate_set, quantity)
            assert cell["total_part_cost"] > 0
            assert cell["total_operation_cost"] > 0
            assert cell["currency"] == part["currency"], where
            assert cell["rate_set"]["version"] == part["rate_set"]["version"], where
            for figure in ("total_part_cost", "per_unit_cost", "total_material_cost",
                           "total_operation_cost", "total_stage_cost", "direct_cost_base",
                           "total_additional_cost"):
                assert cell[figure] == part[figure], (figure, where)
            for figure in ("overhead_cost", "scrap_cost", "tooling_cost"):
                assert cell[figure] == part["additional_costs"][figure], (figure, where)
            assert cell["material_charged"] is part["material_charged"]
    # Only the nine `/cost/cad-part` calls made by this test were stored.
    assert estimate_count() == before + 9


def test_no_system_time_is_accepted_for_the_preview():
    """A plan whose times are estimated but not accepted prices no operation —
    here exactly as in an estimate. Accepting them is the bulk preview's labelled
    special case and must not happen in this one."""
    plan = unaccepted_plan(NAME)
    cell = matrix(rate_sets=(DEFAULT,), quantities=(3,), plan=plan).json()["rows"][0]["cells"][0]
    assert cell["status"] == "priced"
    assert cell["total_operation_cost"] == 0
    assert cell["operations_costed"] == 0
    assert cell["operations_not_timed"] > 0

    part = cost(NAME, DEFAULT, 3, plan=plan).json()["part_cost"]
    assert cell["total_part_cost"] == part["total_part_cost"]
    assert cell["total_operation_cost"] == part["total_operation_cost"] == 0
    # And the committed plan does price them, so the zero above is the plan's, not the preview's.
    committed = matrix(rate_sets=(DEFAULT,), quantities=(3,)).json()["rows"][0]["cells"][0]
    assert committed["total_operation_cost"] > 0 and committed["operations_not_timed"] == 0


def test_without_a_plan_the_coverage_is_unknown_not_zero():
    cell = matrix(rate_sets=(DEFAULT,), quantities=(1,), plan=None).json()["rows"][0]["cells"][0]
    assert cell["status"] == "priced"
    assert cell["operations_costed"] is None and cell["operations_not_timed"] is None


def test_an_entered_additional_cost_reaches_every_cell_as_it_would_an_estimate():
    body = matrix(quantities=(4,), tooling_cost_per_batch="1234.5").json()
    for row in body["rows"]:
        assert row["cells"][0]["tooling_cost"] == 1234.5
        assert row["cells"][0]["overhead_cost"] == 0


# --- 3. a cell that cannot be priced ------------------------------------------


def test_a_rate_set_with_no_rate_for_a_process_is_refused_in_the_pricing_paths_words():
    body = matrix(TURNED, rate_sets=(DEFAULT, SHOP_LATEST), quantities=(1, 20)).json()
    priced, refused = body["rows"]
    assert [cell["status"] for cell in priced["cells"]] == ["priced", "priced"]
    for cell, quantity in zip(refused["cells"], (1, 20), strict=True):
        assert cell["status"] == "refused"
        assert "turning" in cell["refusal"]["message"]
        endpoint = cost(TURNED, SHOP_LATEST, quantity)
        assert endpoint.status_code == 400
        assert cell["refusal"]["message"] == endpoint.json()["detail"]
        assert cell["refusal"]["code"] == "http_400"
        for figure in (*FIGURES, "currency", "material_charged"):
            assert cell[figure] is None, figure
        # The row is still named: the rate set exists, it just cannot price this plan.
        assert cell["rate_set"]["name"] and cell["rate_set"]["currency"] == "INR"


def test_an_unknown_rate_set_is_a_refused_row_and_the_others_still_run():
    body = matrix(rate_sets=({"rate_set_id": "no-such-shop"}, DEFAULT), quantities=(1,)).json()
    unknown, known = body["rows"]
    assert unknown["rate_set"]["resolved"] is False
    assert "no-such-shop" in unknown["rate_set"]["reason"]
    assert unknown["rate_set"]["currency"] is None and unknown["rate_set"]["name"] is None
    cell = unknown["cells"][0]
    assert cell["status"] == "refused" and "no-such-shop" in cell["refusal"]["message"]
    assert cell["total_part_cost"] is None
    assert known["cells"][0]["status"] == "priced"


def test_a_refused_cell_cannot_carry_a_figure():
    rate_set = matrix_preview.MatrixRateSet(requested_rate_set_id="x", resolved=False, reason="gone")
    refusal = matrix_preview.MatrixRefusal(code="http_400", message="no")
    for figure in ("total_part_cost", "per_unit_cost", "total_material_cost", "overhead_cost"):
        with pytest.raises(ValueError):
            matrix_preview.MatrixCell(
                rate_set=rate_set, quantity=1, status="refused", refusal=refusal, **{figure: 0.0}
            )
    with pytest.raises(ValueError):
        matrix_preview.MatrixCell(rate_set=rate_set, quantity=1, status="refused")
    with pytest.raises(ValueError):
        matrix_preview.MatrixCell(rate_set=rate_set, quantity=1, status="priced")


# --- 4. currencies ------------------------------------------------------------


def test_one_currency_carries_no_currency_note():
    assert matrix().json()["currency_note"] is None


def test_mixed_currencies_are_stated_and_never_converted():
    publish_usd()
    usd = {"rate_set_id": "usd-shop"}
    body = matrix(rate_sets=(DEFAULT, usd), quantities=(5,)).json()
    note = body["currency_note"]
    assert "INR" in note and "USD" in note and "Nothing is converted" in note
    inr_cell, usd_cell = body["rows"][0]["cells"][0], body["rows"][1]["cells"][0]
    assert (inr_cell["currency"], usd_cell["currency"]) == ("INR", "USD")
    # The USD row is the USD rate set's own estimate, untouched by the INR row beside it.
    assert usd_cell["total_part_cost"] == cost(NAME, usd, 5).json()["part_cost"]["total_part_cost"]
    assert matrix_preview.currency_note(["INR", None, "INR"]) is None
    assert matrix_preview.currency_note(["USD", "INR", "USD"]).count("USD, INR") == 1


# --- 5. nothing is chosen ------------------------------------------------------


def test_nothing_is_chosen_and_nothing_is_marked_best():
    body = matrix().json()
    assert body["chosen"] is None
    assert "not estimates" in body["note"] and "not ranked" in body["note"]
    keys = set(body)
    for row in body["rows"]:
        keys |= set(row) | set(row["rate_set"])
        for cell in row["cells"]:
            keys |= set(cell)
    for word in ("cheapest", "best", "rank", "lowest", "winner", "selected", "recommend"):
        assert not any(word in key for key in keys), word


# --- 6. what is refused whole ---------------------------------------------------


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"rate_sets": "{not json"}, "'rate_sets' must be valid JSON"),
        ({"rate_sets": "[]"}, "non-empty"),
        ({"rate_sets": '["poc-default"]'}, "must be an object"),
        ({"rate_sets": '[{"version": "1.0.0"}]'}, "rate_set_id"),
        ({"rate_sets": '[{"rate_set_id": "poc-default"}, {"rate_set_id": "poc-default"}]'}, "listed twice"),
        ({"quantities": "nope"}, "'quantities' must be valid JSON"),
        ({"quantities": "[]"}, "non-empty"),
        ({"quantities": "[1, 0]"}, "at least 1"),
        ({"quantities": "[1, 2.5]"}, "whole number"),
        ({"quantities": "[1, null]"}, "whole number"),
        ({"quantities": "[true]"}, "whole number"),
        ({"quantities": "[5, 5]"}, "listed twice"),
    ],
)
def test_a_request_that_is_not_a_grid_is_refused_with_a_400(override, fragment):
    response = matrix(**override)
    assert response.status_code == 400, response.text
    assert fragment in response.json()["detail"]


def test_a_grid_over_the_cell_budget_is_refused_not_trimmed(monkeypatch):
    monkeypatch.setattr(main, "MAX_MATRIX_CELLS", 4)
    assert matrix(quantities=(1, 2)).status_code == 200
    response = matrix(quantities=(1, 2, 3))
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "6 cells" in detail and "at most 4" in detail


def test_the_cell_budget_is_a_listed_configurable_limit():
    from cad_costing_agent import limits

    assert limits.MAX_MATRIX_CELLS == 24
    assert "CAD_COSTING_MAX_MATRIX_CELLS" in limits.__doc__


# --- 7. determinism -----------------------------------------------------------


def test_the_same_request_gives_the_same_matrix():
    arguments = dict(rate_sets=(DEFAULT, SHOP_LATEST, {"rate_set_id": "no-such-shop"}), quantities=(1, 12))
    assert matrix(TURNED, **arguments).json() == matrix(TURNED, **arguments).json()


# --- 8. structure -------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
    return found


def test_the_matrix_module_imports_only_money():
    own = {m for m in _imports(PACKAGE / "matrix_preview.py") if m.startswith(".") or "cad_costing_agent" in m}
    assert own == {"cad_costing_agent.money"}


def test_only_main_imports_the_matrix_module():
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if path.name in {"main.py", "matrix_preview.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        if any(name.endswith("matrix_preview") for name in _imports(path)) or (
            "matrix_preview" in {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}
        ):
            offenders.append(path.name)
    assert offenders == []


def test_the_matrix_module_cannot_price_or_rank():
    tree = ast.parse((PACKAGE / "matrix_preview.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"sorted", "sort", "min", "max", "sum", "round", "round_money"}
    # One multiplication exists and it counts cells; nothing divides or adds money.
    operators = [type(n.op).__name__ for n in ast.walk(tree) if isinstance(n, ast.BinOp)]
    assert operators.count("Mult") == 1
    assert "Div" not in operators and "Sub" not in operators


def _section() -> str:
    source = (PACKAGE / "main.py").read_text(encoding="utf-8")
    start = source.index("# -------------------------------------------------------------- matrix preview")
    # The section ends at the next section banner, not at the end of the file.
    following = source.find("\n# ---", start + 1)
    section = source[start : following if following != -1 else len(source)]
    # Unparsed, so the prose saying what is absent is not what is searched.
    return ast.unparse(ast.parse(section))


def test_the_matrix_section_stores_nothing_ranks_nothing_and_prices_only_through_price_cad_part():
    code = _section()
    assert "def cost_matrix_preview" in code
    assert code.count("price_cad_part(") == 1
    for forbidden in (
        "EstimateRepository",
        ".save(",
        ".add(",
        "commit(",
        "flush(",
        "calculate_",
        "price_stages",
        "resolve_operation",
        "to_operation_requirements",
        "sorted(",
        ".sort(",
        "min(",
        "max(",
        "sum(",
        "round_money",
        # No time is accepted here: the plan is priced as it was sent.
        "system_time_accepted",
        "system_setup_time_accepted",
        "_accept_for_preview",
        "build_plan",
        "model_copy",
    ):
        assert forbidden not in code, forbidden


def test_the_ui_panel_is_in_the_cost_section_and_its_script_works_nothing_out():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    order = re.findall(r'<script src="([\w.]+\.js)"', html)
    # After the globals it borrows, before the flow router.
    assert order.index("routecompare.js") < order.index("matrix.js") < order.index("estimateflow.js")

    # The card is in the Cost pane and in no other.
    assert html.count('id="matrix-card"') == 1
    cost_pane = html[html.index('data-pane="cost"') : html.index('data-pane="assembly"')]
    assert 'id="matrix-card"' in cost_pane
    assert "nothing is saved" in cost_pane.lower()

    source = (STATIC / "matrix.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))
    assert "/cost/matrix-preview" in code
    assert "/cost/cad-part" not in code
    assert "escapeHtml(" in code
    for token in (
        "sort(", "Math.", "reduce(", "toFixed", "parseFloat", "parseInt", " * ", " / ", " - ",
        "+=", "-=", "cheapest", "lowest", "best", "localStorage",
    ):
        assert token not in code, token
    # It keeps no rate-set list of its own: the picker is drawn from the one app.js loaded.
    assert "state.rateSets" in code
    assert "poc-" not in code
