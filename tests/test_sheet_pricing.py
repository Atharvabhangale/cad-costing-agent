"""Sheet cutting and bending priced by time, from operations the measurement proposes.

What is checked: the cut and bend lines a measured part adds to its plan — what
they carry, that their times are proposals nobody has accepted, and that no
K-factor means a cutting line that needs input; that choosing time pricing
takes each sheet stage out of stage pricing so it is charged once, by the hour,
at a hand-worked figure; that a timed stage with no committed time is refused
rather than priced at zero; that leaving the pricing choice out is the stage
rates exactly as before; and the boundaries — the line builder imports no
costing, and only `main.py` reads it.
"""

from __future__ import annotations

import ast
import json
import re
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.main import app
from cad_costing_agent.manufacturing_plan import ManufacturingPlan
from cad_costing_agent.sheet_operations import (
    BEND_OPERATION_ID,
    BEND_SETUP_ID,
    CUT_OPERATION_ID,
    CUT_SETUP_ID,
)
from tests import sheet_fixtures
from tests.test_process_classification import decide

client = TestClient(app)
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
NAME = "l_bracket"

TABLE = [
    {"thickness_mm": 1.0, "cut_speed_mm_per_min": 6000.0, "pierce_seconds": 0.6},
    {"thickness_mm": 3.0, "cut_speed_mm_per_min": 3000.0, "pierce_seconds": 1.8},
]
SHEET_TIMES = dict(
    sheet_cutting_table=TABLE,
    sheet_cutting_setup_minutes=10.0,
    bending_minutes_per_bend=0.25,
    bending_setup_minutes=20.0,
)
STAGE_RATES = {
    "sheet_cutting": {"unit_basis": "per_piece", "unit_rate": 35.0},
    "deburring": {"unit_basis": "per_piece", "unit_rate": 5.0},
    "bending": {"unit_basis": "per_piece", "unit_rate": 20.0},
}
HOURLY = {
    "sheet_cutting": {"machine_rate_per_hour": 900.0, "labor_rate_per_hour": 300.0},
    "bending": {"machine_rate_per_hour": 600.0, "labor_rate_per_hour": 300.0},
}
FORM = {
    "material": "Steel",
    "source_unit": "mm",
    "part_id": "P-SHEET",
    "part_name": "Sheet bracket",
    "quantity": "5",
}


@lru_cache(maxsize=None)
def step(name: str = NAME) -> bytes:
    return sheet_fixtures.SHEET_FIXTURES[name]()


def measure(name: str = NAME, k: float | None = 0.4) -> dict:
    response = client.post(
        "/cad/sheet-metal",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
        data={} if k is None else {"k_factor": str(k)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def publish_profile():
    baseline = client.get("/time-profiles/poc-baseline-time").json()["assumptions"]
    response = client.post(
        "/time-profiles",
        json={"time_profile_id": "sheet-shop-time", "name": "Sheet shop", "version": "1.0.0",
              "description": "test", "is_illustrative": True,
              "assumptions": {**baseline, **SHEET_TIMES}},
    )
    assert response.status_code == 201, response.text


def publish_rates(*, hourly=HOURLY):
    base = client.get("/rates/poc-default").json()
    response = client.post(
        "/rates",
        json={"rate_set_id": "sheet-shop", "name": "Sheet shop", "version": "1.0.0",
              "currency": "INR", "is_illustrative": False,
              "materials": base["materials"], "operations": {**base["operations"], **hourly},
              "additional_costs": {"overhead_rate_percent": 0, "scrap_rate_percent": 0,
                                   "tooling_cost_per_batch": 0},
              "stage_rates": STAGE_RATES},
    )
    assert response.status_code == 201, response.text


def lines(analysis: dict, *, profile: str | None = "sheet-shop-time") -> dict:
    query = f"?time_profile_id={profile}" if profile else ""
    response = client.post(f"/operations/sheet-metal-lines{query}", json={"analysis": analysis})
    assert response.status_code == 200, response.text
    return response.json()


def plan_of(body: dict, *, accept: bool = True) -> ManufacturingPlan:
    plan = ManufacturingPlan(
        setups=body["setups"], operations=body["operations"], units="mm",
        source_filename=f"{NAME}.step",
    )
    if accept:
        for operation in plan.operations:
            plan = plan.replacing(operation.accepted(use_estimated_time=True))
        for setup in plan.setups:
            plan = plan.replacing_setup(setup.accepted(use_estimated_time=True))
    return plan


@lru_cache(maxsize=None)
def _classification() -> str:
    suggested = client.post(
        "/cad/operations/suggest",
        files={"file": (f"{NAME}.step", step(), "application/octet-stream")},
    ).json()
    response = decide(suggested["process_classification"], "accepted")
    assert response.status_code == 200, response.text
    return response.text


def classification() -> dict:
    body = json.loads(_classification())
    assert [s["stage"] for s in body["route"]["stages"]] == ["sheet_cutting", "deburring", "bending"]
    return body


def cost(plan: ManufacturingPlan, stage_inputs=None):
    data = {**FORM, "rate_set_id": "sheet-shop",
            "process_classification": json.dumps(classification()),
            "manufacturing_plan": plan.model_dump_json()}
    if stage_inputs is not None:
        data["stage_inputs"] = json.dumps(stage_inputs)
    return client.post(
        "/cost/cad-part",
        files={"file": (f"{NAME}.step", step(), "application/octet-stream")},
        data=data,
    )


TIMED_BOTH = {"sheet_cutting": {"pricing": "timed"}, "bending": {"pricing": "timed"}}


# --- 1. the lines -----------------------------------------------------------------


def test_a_measured_part_proposes_one_cut_and_one_bend_line_unaccepted():
    publish_profile()
    analysis = measure()
    body = lines(analysis)
    cut, bend = body["operations"]
    assert cut["operation_id"] == CUT_OPERATION_ID and bend["operation_id"] == BEND_OPERATION_ID
    assert cut["system_suggested_operation"] == "sheet_cutting"
    assert bend["system_suggested_operation"] == "bending"
    assert cut["system_suggestion_rule"] == "sheet_flat_pattern_cut_v1"
    assert bend["system_suggestion_rule"] == "sheet_bend_count_v1"
    assert cut["feature_ids"] == [] and cut["origin"] == "system" and cut["user_action"] == "pending"
    # The measured figures it was timed from, the K-factor among them.
    assert cut["dimensions"] == {
        "thickness_mm": 2.0,
        "pierce_count": 1.0,
        "cut_perimeter_mm": analysis["flat_pattern"]["cut_perimeter_mm"],
        "k_factor": 0.4,
    }
    assert bend["dimensions"] == {"bend_count": 1.0, "thickness_mm": 2.0}
    # Estimated, never committed. Cut: 251.938 / 4500 + 1 x 1.2 / 60 = 0.0760 -> 0.08 min.
    assert cut["system_suggested_cycle_time"] == 0.08 and cut["system_suggested_setup_time"] == 10.0
    assert bend["system_suggested_cycle_time"] == 0.25 and bend["system_suggested_setup_time"] == 20.0
    assert cut["system_time_accepted"] is False and bend["system_time_accepted"] is False
    assert cut["system_time_profile_id"] == "sheet-shop-time"
    # One setup each, their times proposals too.
    assert [s["setup_id"] for s in body["setups"]] == [CUT_SETUP_ID, BEND_SETUP_ID]
    assert [s["system_setup_time_minutes"] for s in body["setups"]] == [10.0, 20.0]
    assert all(s["system_setup_time_accepted"] is False for s in body["setups"])
    plan = plan_of(body, accept=False)
    assert plan.to_operation_requirements(5) == []


def test_without_a_k_factor_the_cutting_line_needs_input_and_says_why():
    publish_profile()
    body = lines(measure(k=None))
    cut, bend = body["operations"]
    assert cut["system_time_status"] == "needs_user_input"
    assert cut["system_suggested_cycle_time"] is None
    assert cut["system_time_missing"] == ["cut_perimeter_mm"]
    assert "K-factor" in cut["system_time_reason"]
    assert "cut_perimeter_mm" not in cut["dimensions"]
    # Bending does not depend on K.
    assert bend["system_time_status"] == "estimated"
    assert any("No K-factor" in warning for warning in body["warnings"])


def test_a_flat_part_proposes_no_bend_line():
    publish_profile()
    body = lines(measure("flat_plate_two_holes"))
    assert [o["operation_id"] for o in body["operations"]] == [CUT_OPERATION_ID]
    assert body["operations"][0]["dimensions"]["pierce_count"] == 3.0


def test_the_seeded_profile_proposes_the_lines_but_times_nothing():
    body = lines(measure(), profile=None)
    assert all(o["system_time_status"] == "needs_user_input" for o in body["operations"])


def test_a_part_that_is_not_a_sheet_gets_no_lines():
    analysis = measure("thick_block")
    response = client.post("/operations/sheet-metal-lines", json={"analysis": analysis})
    assert response.status_code == 400
    assert "not measured as a sheet" in response.json()["detail"]


def test_measuring_again_replaces_rather_than_adds():
    publish_profile()
    first = lines(measure(k=0.33))
    second = lines(measure(k=0.5))
    assert [o["operation_id"] for o in first["operations"]] == [o["operation_id"] for o in second["operations"]]
    assert first["operations"][0]["dimensions"]["cut_perimeter_mm"] < second["operations"][0]["dimensions"]["cut_perimeter_mm"]


# --- 2. costing ---------------------------------------------------------------------


def test_without_a_pricing_choice_the_sheet_stages_are_their_stage_rates_as_before():
    publish_profile()
    publish_rates()
    response = cost(plan_of(lines(measure())))
    assert response.status_code == 200, response.text
    part_cost = response.json()["part_cost"]
    assert [line["stage"] for line in part_cost["stage_costs"]] == ["sheet_cutting", "deburring", "bending"]
    assert part_cost["total_stage_cost"] == pytest.approx(5 * (35 + 5 + 20))
    # The accepted cut and bend lines are outside a route that prices no stage by time.
    assert part_cost["total_operation_cost"] == 0.0
    assert part_cost["operations"] == []


def test_timed_sheet_stages_are_costed_by_the_hour_and_never_also_by_their_stage_rates():
    publish_profile()
    publish_rates()
    response = cost(plan_of(lines(measure())), stage_inputs=TIMED_BOTH)
    assert response.status_code == 200, response.text
    body = response.json()
    part_cost = body["part_cost"]
    assert [line["stage"] for line in part_cost["stage_costs"]] == ["deburring"]
    assert part_cost["total_stage_cost"] == pytest.approx(25.0)
    by_process = {line["process_type"]: line for line in part_cost["operations"]}
    assert set(by_process) == {"sheet_cutting", "bending"}
    # Cutting: 900/h machine on 10 min setup + 5 x 0.08 min run, 300/h labour on the run:
    # 150 + 6 + 2 = 158. Bending: 600/h on 20 min + 5 x 0.25 min, 300/h on the run:
    # 200 + 12.5 + 6.25 = 218.75.
    assert by_process["sheet_cutting"]["cycle_time_minutes"] == 0.08
    assert by_process["sheet_cutting"]["setup_time_minutes"] == 10.0
    assert by_process["sheet_cutting"]["total_operation_cost"] == pytest.approx(158.0, abs=0.01)
    assert by_process["bending"]["total_operation_cost"] == pytest.approx(218.75, abs=0.01)
    assert part_cost["total_operation_cost"] == pytest.approx(376.75, abs=0.01)
    assert not any("sheet" in note or "bending" in note for note in body.get("route_differences", []))


def test_one_sheet_stage_may_be_timed_without_the_other():
    publish_profile()
    publish_rates()
    response = cost(plan_of(lines(measure())), stage_inputs={"bending": {"pricing": "timed"}})
    assert response.status_code == 200, response.text
    part_cost = response.json()["part_cost"]
    assert [line["stage"] for line in part_cost["stage_costs"]] == ["sheet_cutting", "deburring"]
    assert [line["process_type"] for line in part_cost["operations"]] == ["bending"]


def test_a_timed_sheet_stage_with_no_accepted_time_is_refused_not_zero():
    publish_profile()
    publish_rates()
    before = client.get("/estimates").json()["total"]
    response = cost(plan_of(lines(measure()), accept=False), stage_inputs=TIMED_BOTH)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "timed_stage_not_costed"
    assert detail["stages"] == ["sheet_cutting", "bending"]
    assert "cut and bend lines from the measured flat pattern" in detail["message"]
    assert client.get("/estimates").json()["total"] == before


def test_timed_cutting_without_an_hourly_rate_is_refused_with_that_reason():
    publish_profile()
    publish_rates(hourly={"bending": HOURLY["bending"]})
    response = cost(plan_of(lines(measure())), stage_inputs=TIMED_BOTH)
    assert response.status_code == 400
    assert "no rate for process type 'sheet_cutting'" in response.json()["detail"]


def test_the_pricing_choice_and_the_measured_figures_are_stored_with_the_estimate():
    publish_profile()
    publish_rates()
    estimate_id = cost(plan_of(lines(measure())), stage_inputs=TIMED_BOTH).json()["estimate_id"]
    stored = client.get(f"/estimates/{estimate_id}").json()
    assert stored["request_snapshot"]["stage_inputs"] == TIMED_BOTH
    operations = stored["plan_snapshot"]["plan"]["operations"]
    cut = next(o for o in operations if o["operation_id"] == CUT_OPERATION_ID)
    assert cut["dimensions"]["k_factor"] == 0.4
    feedback = {entry["operation_id"]: entry for entry in stored["plan_snapshot"]["feedback"]}
    assert feedback[CUT_OPERATION_ID]["costed"] is True
    assert "sheet-shop-time@1.0.0" in stored["time_profile_snapshot"]["profiles"]


# --- 3. boundaries ------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_line_builder_imports_no_costing_and_only_main_reads_it():
    own = {m for m in _imports(PACKAGE / "sheet_operations.py") if m.startswith("cad_costing_agent")}
    assert own == {
        "cad_costing_agent.features.model",
        "cad_costing_agent.features.sheet_metal",
        "cad_costing_agent.manufacturing_plan",
        "cad_costing_agent.operation_suggestions",
        "cad_costing_agent.operation_time",
    }
    readers = {
        path.name for path in PACKAGE.rglob("*.py")
        if "cad_costing_agent.sheet_operations" in _imports(path)
    }
    assert readers == {"main.py"}


# --- 4. the browser -----------------------------------------------------------------

STATIC = PACKAGE / "static"


def _js_code(name: str) -> str:
    """A static script with its comments removed, so a grep sees code, not prose."""
    source = re.sub(r"/\*.*?\*/", "", (STATIC / name).read_text(encoding="utf-8"), flags=re.S)
    return "\n".join(line for line in source.split("\n") if not line.strip().startswith("//"))


def test_the_sheet_panel_is_served_and_loads_after_the_plan_it_adds_to():
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="([a-z]+\.js)"></script>', markup)
    assert scripts.index("sheet.js") > scripts.index("plan.js")
    assert scripts.index("sheet.js") > scripts.index("process.js")
    assert 'id="sheet-card"' in markup and 'id="sheet-k"' in markup
    assert client.get("/ui/sheet.js").status_code == 200


def test_the_sheet_panel_measures_nothing_and_commits_nothing():
    code = _js_code("sheet.js")
    # It shows the server's figures and works none out.
    for forbidden in ("Math.", " * ", "_mm *", "_mm /"):
        assert forbidden not in code
    # A blank K-factor is not sent, so the server's "no default" holds.
    assert 'if (k !== "") body.append("k_factor", k);' in code
    # The proposed lines arrive unaccepted and stay so.
    assert "system_time_accepted" not in code
    assert "/cad/sheet-metal" in code and "/operations/sheet-metal-lines" in code
    # It declares no global function another script could replace.
    assert not re.search(r"^(?:async )?function ", code, re.M)


def test_the_route_rows_offer_the_flat_pattern_lines_for_a_timed_sheet_stage():
    code = _js_code("plan.js")
    work = code.split("const TIMED_STAGE_WORK = ", 1)[1].split("\n};", 1)[0]
    assert "add-sheet-lines" in work and "add-weld" in work
    assert set(re.findall(r"^  (\w+): \{", work, re.M)) == {"welding", "sheet_cutting", "bending"}
