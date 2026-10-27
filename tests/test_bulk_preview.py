"""Bulk costing preview: priced from unreviewed system times, and stored nowhere.

What is checked is the honesty of the preview, not a list of constants:

- every row, the response and the exported CSV say it is not an estimate;
- nothing is stored (the estimate count does not move);
- a preview row costs exactly what `/cost/cad-part` costs for the same part with
  the same times accepted — there is one pricing path;
- an operation the system cannot time is counted as omitted, never zeroed;
- a failed row shows no cost and never stops the others;
- currencies are never summed together.
"""

from __future__ import annotations

import ast
import csv
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent import bulk_preview
from cad_costing_agent.bulk_preview import BatchRefused, parse_batch_csv
from cad_costing_agent.features import recognize_features_from_bytes
from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import build_plan
from cad_costing_agent.operation_suggestions import suggest_operations
from tests import step_fixtures
from tests.assembly_fixtures import commit_every_time

client = TestClient(app)
SRC = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
HEADER = "file,part_id,part_name,material,quantity,rate_set_id,rate_set_version,route_id\n"


def post(rows: str, files: dict[str, bytes], *, header: str = HEADER, params=None, **data):
    parts = [("batch_csv", ("batch.csv", (header + rows).encode("utf-8"), "text/csv"))]
    parts += [("files", (name, content, "application/octet-stream")) for name, content in files.items()]
    return client.post("/cad/bulk/preview", files=parts, data=data, params=params)


def estimate_count() -> int:
    return client.get("/estimates").json()["total"]


@pytest.fixture(scope="module")
def pocket() -> bytes:
    return step_fixtures.block_with_pocket()


@pytest.fixture(scope="module")
def holes() -> bytes:
    return step_fixtures.plate_with_four_holes()


@pytest.fixture(scope="module")
def plain() -> bytes:
    return step_fixtures.plain_block()


# ----------------------------------------------------------------- behaviour


def test_priced_refused_and_error_rows_in_one_batch(pocket, holes):
    rows = (
        "pocket.step,P1,Pocket block,Steel,2,poc-default,,\n"
        "holes.step,P2,Hole plate,Aluminium,1,poc-default,,\n"
        "missing.step,P3,Not uploaded,Steel,1,poc-default,,\n"
        "pocket.step,P4,Bad quantity,Steel,0,poc-default,,\n"
        "pocket.step,P5,Unknown material,Unobtainium,1,poc-default,,\n"
        "pocket.step,P6,Needs stage rates,Steel,1,poc-default,,casting_machining\n"
    )
    response = post(rows, {"pocket.step": pocket, "holes.step": holes})
    assert response.status_code == 200, response.text
    body = response.json()
    by_part = {row["part_id"]: row for row in body["rows"]}

    for part in ("P1", "P2"):
        assert by_part[part]["status"] == "priced"
        assert by_part[part]["totals"]["total_cost"] > 0
        assert by_part[part]["currency"] == "INR"
    assert by_part["P3"]["status"] == "error" and "was not uploaded" in by_part["P3"]["reason"]
    assert by_part["P4"]["status"] == "error" and "at least 1" in by_part["P4"]["reason"]
    assert by_part["P5"]["status"] == "error" and "Unobtainium" in by_part["P5"]["reason"]
    assert by_part["P6"]["status"] == "refused"
    for part in ("P3", "P4", "P5", "P6"):
        assert by_part[part]["totals"] is None
        assert by_part[part]["currency"] is None
        assert by_part[part]["reason"]

    assert body["summary"]["priced"] == 2
    assert body["summary"]["error"] == 3
    assert body["summary"]["refused"] == 1


def test_nothing_is_stored_and_everything_is_labelled(pocket):
    before = estimate_count()
    response = post("pocket.step,P1,Pocket,Steel,1,poc-default,,\n", {"pocket.step": pocket})
    assert response.status_code == 200
    assert estimate_count() == before

    body = response.json()
    assert body["stored"] is False
    assert body["preview_label"] == bulk_preview.PREVIEW_LABEL
    assert "not an estimate, not quotable" in body["preview_label"]
    assert all(row["preview_label"] == bulk_preview.PREVIEW_LABEL for row in body["rows"])
    assert "estimate_id" not in json.dumps(body)
    assert "not quotable" in body["notice"] or "nothing here may be quoted" in body["notice"]

    exported = list(csv.DictReader(io.StringIO(body["csv"])))
    assert exported and all(r["preview_label"] == bulk_preview.PREVIEW_LABEL for r in exported)


def test_a_preview_row_costs_what_cost_cad_part_costs_for_the_same_times(pocket):
    quantity = 3
    row = post(
        f"pocket.step,P1,Pocket,Steel,{quantity},poc-default,,\n", {"pocket.step": pocket}
    ).json()["rows"][0]
    assert row["status"] == "priced" and row["operations_costed"] > 0

    recognition = recognize_features_from_bytes(pocket, "pocket.step")
    suggestions = suggest_operations(recognition)
    plan = build_plan(suggestions.suggestions, units=suggestions.units, source_filename="pocket.step")
    committed = commit_every_time(plan.model_dump(mode="json"))
    stored = client.post(
        "/cost/cad-part",
        files={"file": ("pocket.step", pocket, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P1",
            "part_name": "Pocket",
            "quantity": str(quantity),
            "rate_set_id": "poc-default",
            "manufacturing_plan": json.dumps(committed),
        },
    )
    assert stored.status_code == 200, stored.text
    cost = stored.json()["part_cost"]
    assert row["totals"]["total_cost"] == cost["total_part_cost"]
    assert row["totals"]["per_unit_cost"] == cost["per_unit_cost"]
    assert row["totals"]["operations_cost"] == cost["total_operation_cost"]
    assert row["totals"]["material_cost"] == cost["total_material_cost"]


def test_an_operation_the_system_cannot_time_is_counted_omitted_not_zeroed(holes):
    row = post("holes.step,P1,Holes,Steel,1,poc-default,,\n", {"holes.step": holes}).json()["rows"][0]
    assert row["status"] == "priced"
    # Costed + omitted accounts for every active operation; none is silently dropped.
    suggestions = suggest_operations(recognize_features_from_bytes(holes, "holes.step"))
    assert row["operations_costed"] + row["operations_omitted"] == len(suggestions.suggestions)
    for omitted in row["omitted_operations"]:
        assert omitted["reason"]


def test_missing_time_is_reported_with_reason_when_units_are_not_millimetres(monkeypatch, pocket):
    """A plan whose estimates all come back `needs_user_input` prices material only and says so."""
    import cad_costing_agent.main as main

    real = main.build_plan

    def untimed(suggestions, **kwargs):
        plan = real(suggestions, **kwargs)
        return plan.model_copy(
            update={
                "operations": [
                    o.model_copy(update={"system_suggested_cycle_time": None})
                    for o in plan.operations
                ]
            }
        )

    monkeypatch.setattr(main, "build_plan", untimed)
    row = post("pocket.step,P1,Pocket,Steel,1,poc-default,,\n", {"pocket.step": pocket}).json()["rows"][0]
    assert row["status"] == "priced"
    assert row["operations_costed"] == 0
    assert row["operations_omitted"] > 0
    assert row["totals"]["operations_cost"] == 0.0
    assert any("material cost only" in note for note in row["notes"])


def test_a_chosen_route_that_needs_stage_rates_is_refused_with_its_reason(pocket):
    row = post(
        "pocket.step,P1,Pocket,Steel,1,poc-default,,casting_machining\n", {"pocket.step": pocket}
    ).json()["rows"][0]
    assert row["status"] == "refused"
    assert row["totals"] is None
    assert row["reason"]


def test_a_decided_machining_route_is_applied(pocket):
    row = post(
        "pocket.step,P1,Pocket,Steel,1,poc-default,,cnc_milling\n", {"pocket.step": pocket}
    ).json()["rows"][0]
    assert row["status"] == "priced", row
    assert row["route"]


def test_unknown_route_is_an_error_on_that_row_only(pocket):
    rows = (
        "pocket.step,P1,A,Steel,1,poc-default,,no_such_route\n"
        "pocket.step,P2,B,Steel,1,poc-default,,\n"
    )
    result = post(rows, {"pocket.step": pocket}).json()["rows"]
    assert result[0]["status"] == "error" and "no_such_route" in result[0]["reason"]
    assert result[1]["status"] == "priced"


def test_batch_defaults_and_unreferenced_files(pocket, plain):
    response = post(
        "pocket.step,P1,A,Steel,1,,,\n",
        {"pocket.step": pocket, "spare.step": plain},
        rate_set_id="poc-default",
    )
    body = response.json()
    assert body["rows"][0]["status"] == "priced"
    assert body["summary"]["unreferenced_files"] == ["spare.step"]


def test_an_unknown_rate_set_is_an_error_row(pocket):
    row = post("pocket.step,P1,A,Steel,1,nope,,\n", {"pocket.step": pocket}).json()["rows"][0]
    assert row["status"] == "error" and "nope" in row["reason"]


def test_a_unreadable_file_is_one_error_row(pocket):
    rows = "junk.step,P1,A,Steel,1,poc-default,,\npocket.step,P2,B,Steel,1,poc-default,,\n"
    result = post(rows, {"junk.step": b"not a cad file", "pocket.step": pocket}).json()["rows"]
    assert result[0]["status"] == "error" and result[0]["totals"] is None
    assert result[1]["status"] == "priced"


def test_currencies_are_summed_only_within_one_currency(pocket):
    rate_set = client.get("/rates/poc-default").json()
    rate_set["currency"] = "USD"
    rate_set["name"] = "usd copy"
    for key in ("is_illustrative", "created_at"):
        rate_set.pop(key, None)
    rate_set["rate_set_id"] = "usd-set"
    created = client.post("/rates", json=rate_set)
    assert created.status_code == 201, created.text
    rows = (
        "pocket.step,P1,A,Steel,1,poc-default,,\n"
        "pocket.step,P2,B,Steel,1,usd-set,,\n"
        "pocket.step,P3,C,Steel,2,poc-default,,\n"
    )
    summary = post(rows, {"pocket.step": pocket}).json()["summary"]
    currencies = {entry["currency"]: entry for entry in summary["by_currency"]}
    assert set(currencies) == {"INR", "USD"}
    assert currencies["INR"]["priced_rows"] == 2 and currencies["USD"]["priced_rows"] == 1
    assert "never converted" in summary["currency_note"]


def test_csv_export_matches_the_rows_and_carries_the_label(pocket):
    rows = "pocket.step,P1,=cmd,Steel,1,poc-default,,\nmissing.step,P2,B,Steel,1,poc-default,,\n"
    body = post(rows, {"pocket.step": pocket}).json()
    exported = list(csv.DictReader(io.StringIO(body["csv"])))
    assert list(exported[0].keys()) == list(bulk_preview.CSV_COLUMNS)
    assert exported[0]["status"] == "priced"
    assert exported[0]["total_cost"] == f"{body['rows'][0]['totals']['total_cost']:.2f}"
    assert exported[0]["part_name"] == "'=cmd", "a formula in a text cell is made inert"
    assert exported[1]["status"] == "error" and exported[1]["total_cost"] == ""

    raw = post(rows, {"pocket.step": pocket}, params={"format": "csv"})
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/csv")
    assert "UNREVIEWED" in raw.headers["content-disposition"]
    assert raw.content.decode("utf-8-sig") == body["csv"]


def test_the_batch_is_deterministic(pocket, holes):
    rows = "pocket.step,P1,A,Steel,2,poc-default,,\nholes.step,P2,B,Aluminium,1,poc-default,,\n"
    files = {"pocket.step": pocket, "holes.step": holes}
    assert post(rows, files).json() == post(rows, files).json()


# -------------------------------------------------------------------- limits


def test_header_problems_refuse_the_whole_batch(pocket):
    unknown = post("a,b,c,d,1,x\n", {"pocket.step": pocket}, header="file,part_id,part_name,material,quantity,colour\n")
    assert unknown.status_code == 422 and "colour" in unknown.json()["detail"]["message"]
    missing = post("a,b,c\n", {"pocket.step": pocket}, header="file,part_id,part_name\n")
    assert missing.status_code == 422 and "material" in missing.json()["detail"]["message"]
    assert estimate_count() == 0


def test_row_and_file_limits(monkeypatch, pocket):
    import cad_costing_agent.main as main

    monkeypatch.setattr(main, "MAX_BULK_ROWS", 2)
    rows = "pocket.step,P,A,Steel,1,poc-default,,\n" * 3
    assert post(rows, {"pocket.step": pocket}).status_code == 422

    monkeypatch.setattr(main, "MAX_BULK_FILES", 1)
    assert post("pocket.step,P,A,Steel,1,poc-default,,\n", {"a.step": pocket, "b.step": pocket}).status_code == 413

    monkeypatch.setattr(main, "MAX_BULK_FILES", 50)
    monkeypatch.setattr(main, "MAX_BULK_TOTAL_BYTES", 10)
    assert post("pocket.step,P,A,Steel,1,poc-default,,\n", {"pocket.step": pocket}).status_code == 413


def test_the_same_file_uploaded_twice_is_refused(pocket):
    assert post("pocket.step,P,A,Steel,1,poc-default,,\n", {}).status_code == 422
    parts = [
        ("batch_csv", ("b.csv", (HEADER + "pocket.step,P,A,Steel,1,poc-default,,\n").encode(), "text/csv")),
        ("files", ("pocket.step", pocket, "application/octet-stream")),
        ("files", ("pocket.step", pocket, "application/octet-stream")),
    ]
    assert client.post("/cad/bulk/preview", files=parts).status_code == 422


# ---------------------------------------------------------------- csv parsing


def test_parser_reads_rows_and_keeps_row_errors_on_the_row():
    text = HEADER + "a.step,P1,A,Steel,2,poc-default,,\n\n" + "a.step,P2,B,Steel,x,,,\n"
    parsed = parse_batch_csv(text, max_rows=10)
    assert [r.line for r in parsed.rows] == [2, 4]
    assert parsed.rows[0].errors == [] and parsed.rows[0].quantity == 2
    assert "whole number" in parsed.rows[1].errors[0]


@pytest.mark.parametrize("text", ["", "file,file,part_id,part_name,material,quantity\n", HEADER])
def test_parser_refuses_unreadable_batches(text):
    with pytest.raises(BatchRefused):
        parse_batch_csv(text, max_rows=10)


def test_parser_header_is_case_insensitive_and_strips_a_bom():
    parsed = parse_batch_csv("﻿File, Part_ID,PART_NAME,Material,Quantity\nf,1,n,Steel,1\n", max_rows=5)
    assert parsed.rows[0].file == "f" and parsed.rows[0].source_unit == "mm"


def test_a_failed_row_result_cannot_carry_a_cost():
    with pytest.raises(ValueError):
        bulk_preview.BulkRowResult(
            line=2, file="f", part_id="p", part_name="n", material="m", status="error",
            reason="x", currency="INR",
        )
    with pytest.raises(ValueError):
        bulk_preview.BulkRowResult(line=2, file="f", part_id="p", part_name="n", material="m", status="priced")


# ----------------------------------------------------------------- structure


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
    return found


def test_bulk_preview_imports_nothing_from_this_package():
    imported = _imports(SRC / "bulk_preview.py")
    assert not [name for name in imported if name.startswith(".") or "cad_costing_agent" in name]


def test_only_main_imports_bulk_preview():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name in {"main.py", "bulk_preview.py"}:
            continue
        if any(name.endswith("bulk_preview") for name in _imports(path)):
            offenders.append(path.name)
    assert offenders == []


def test_the_bulk_section_stores_nothing_and_prices_only_through_price_cad_part():
    source = (SRC / "main.py").read_text(encoding="utf-8")
    start = source.index("# ---------------------------------------------------------------- bulk preview")
    # The section ends at the next section banner, not at the end of the file:
    # other features' sections may follow it (the DFM section publishes profiles).
    following = source.find("\n# ---", start + 1)
    section = source[start : following if following != -1 else len(source)]
    tree = ast.parse(section.replace("from fastapi import Response  # noqa: E402", ""))
    code = ast.unparse(tree)
    assert "price_cad_part(" in code
    for forbidden in (
        "EstimateRepository",
        ".save(",
        "session.commit",
        "calculate_cad_part_cost",
        "calculate_operation_cost",
        "to_operation_requirements",
    ):
        assert forbidden not in code, forbidden
    # The only place a time is marked accepted is the in-memory copy.
    assert code.count("system_time_accepted") == 1


def test_the_ui_page_computes_nothing_and_says_what_it_is():
    static = SRC / "static"
    js = (static / "bulk.js").read_text(encoding="utf-8")
    code = "\n".join(line.split("//")[0] for line in js.splitlines())
    for fragment in (" * ", "reduce(", "toFixed", "parseFloat", "Math."):
        assert fragment not in code, fragment
    index = (static / "index.html").read_text(encoding="utf-8")
    assert index.index('src="actuals.js"') < index.index('src="bulk.js"') < index.index('src="estimateflow.js"')
    assert 'data-tab="bulk"' in index and 'id="tab-bulk"' in index
    assert "not an estimate, not quotable" in index.lower()
    assert "/cad/bulk/preview" in code
