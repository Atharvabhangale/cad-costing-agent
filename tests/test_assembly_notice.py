"""An assembly sent to a whole-file analysis is said to be one fused body.

Every whole-file endpoint reads an upload as one shape. For a file that
declares an assembly, each response carries the same structured notice or is
refused with it; for a part — including a part of several solids — every
response is exactly what it was, with no key for the notice at all.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent import assembly_notice
from cad_costing_agent.cad_import import declares_assembly, import_cad_document
from cad_costing_agent.main import app
from tests import assembly_fixtures, step_fixtures

client = TestClient(app)

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
STATIC = PACKAGE / "static"
CODE = "assembly_measured_as_one_body"
KEY = "assembly_notice"


@pytest.fixture(scope="module")
def pump() -> bytes:
    return assembly_fixtures.pump_assembly()


@pytest.fixture(scope="module")
def single() -> bytes:
    return step_fixtures.block_with_through_hole()


@pytest.fixture(scope="module")
def multi_body() -> bytes:
    return step_fixtures.multi_body_part()


def _post(path: str, data: bytes, name: str = "model.step", **form):
    return client.post(
        path, files={"file": (name, data, "application/octet-stream")}, data=form or None
    )


def _plan_json(data: bytes) -> str:
    """A plan for the file's own suggestions, as the operations table would hold one."""
    from cad_costing_agent.features import recognize_features_from_bytes
    from cad_costing_agent.manufacturing_plan import build_plan
    from cad_costing_agent.operation_suggestions import suggest_operations

    recognition = recognize_features_from_bytes(data, "model.step")
    return build_plan(suggest_operations(recognition).suggestions, units="mm").model_dump_json()


COST_FORM = {
    "material": "Steel",
    "source_unit": "mm",
    "part_id": "P-1",
    "part_name": "Part",
    "quantity": "1",
    "rate_set_id": "poc-default",
}

# (analysis key, path, form) for every whole-file analysis that answers with a notice.
NOTICED = [
    ("geometry", "/cad/geometry", {}),
    ("mass", "/cad/mass", {"material": "Steel", "source_unit": "mm"}),
    ("features", "/cad/features", {}),
    ("suggestions", "/cad/operations/suggest", {}),
    ("dfm", "/cad/dfm", {}),
    ("wall_thickness", "/cad/wall-thickness", {}),
    ("governing_wall", "/cad/governing-wall", {}),
    ("sheet_metal", "/cad/sheet-metal", {}),
    ("casting_modulus", "/cad/casting-modulus", {}),
    ("cost", "/cost/cad-part", COST_FORM),
]


# ------------------------------------------------------------------ the helper


def test_the_helper_is_true_for_an_assembly_and_false_for_parts(pump, single, multi_body):
    assert declares_assembly(import_cad_document(pump, "pump.step", with_geometry=False))
    assert not declares_assembly(import_cad_document(single, "block.step", with_geometry=False))
    # Two solids under one product: a part, not an assembly.
    assert not declares_assembly(import_cad_document(multi_body, "two.step", with_geometry=False))


def test_the_helper_gives_the_same_answer_with_and_without_geometry(pump, multi_body):
    for data in (pump, multi_body):
        assert declares_assembly(import_cad_document(data, "m.step")) == declares_assembly(
            import_cad_document(data, "m.step", with_geometry=False)
        )


def test_the_helper_agrees_with_what_import_reports(pump, multi_body):
    for data, expected in ((pump, True), (multi_body, False)):
        document = _post("/cad/import", data).json()
        warned = any(w["code"] == "assembly_not_a_part" for w in document["warnings"])
        assert warned is expected
        assert (document["structure_state"] == "structure_available") is expected


def _function_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            node.body = [
                statement
                for statement in node.body
                if not (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                )
            ]
            return ast.unparse(node)
    raise AssertionError(name)


def test_the_helper_reads_the_declaration_and_parses_nothing():
    source = _function_source(PACKAGE / "cad_import" / "__init__.py", "declares_assembly")
    assert "structure_state" in source
    for forbidden in ("read_step_assembly", "decompose_assembly", "solid", "count("):
        assert forbidden not in source


def _code(path: Path) -> str:
    """Python source with docstrings and comments removed."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_one_helper_decides_and_main_asks_only_it():
    main = _code(PACKAGE / "main.py")
    # Asked in exactly the two functions every endpoint goes through, and for the
    # DFM draft rules.
    assert main.count("declares_assembly(") == 3
    assert "def declares_assembly" not in main
    definitions = [
        path.name
        for path in PACKAGE.rglob("*.py")
        if "def declares_assembly" in path.read_text(encoding="utf-8")
    ]
    assert definitions == ["__init__.py"]


def test_the_notice_module_imports_nothing_from_the_package():
    tree = ast.parse((PACKAGE / "assembly_notice.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == {"__future__"}


# ------------------------------------------------------------------ the notice


@pytest.mark.parametrize("analysis,path,form", NOTICED, ids=[row[0] for row in NOTICED])
def test_every_whole_file_analysis_of_an_assembly_carries_the_notice(pump, analysis, path, form):
    response = _post(path, pump, "pump.step", **form)
    assert response.status_code == 200, response.text
    notice = response.json()[KEY]
    assert notice["code"] == CODE
    assert "fused together as one body" in notice["message"]
    assert "not a finding about any part" in notice["message"]
    assert notice["component_placements"] == 6
    assert "Assembly section" in notice["see_instead"]
    available, route = assembly_notice.PER_COMPONENT[analysis]
    assert notice["per_component_available"] is available
    assert notice["per_component"] == route


@pytest.mark.parametrize("analysis,path,form", NOTICED, ids=[row[0] for row in NOTICED])
@pytest.mark.parametrize("fixture", ["single", "multi_body"])
def test_a_part_gets_no_key_at_all(request, fixture, analysis, path, form):
    response = _post(path, request.getfixturevalue(fixture), "part.step", **form)
    assert response.status_code == 200, response.text
    assert KEY not in response.json()
    assert CODE not in response.text


def test_the_notice_only_adds_a_key_to_the_assembly_response(pump):
    """The measurement under the notice is the one the endpoint always returned."""
    from cad_costing_agent.cad_geometry import extract_geometry_from_bytes

    body = _post("/cad/geometry", pump, "pump.step").json()
    body.pop(KEY)
    expected = extract_geometry_from_bytes(pump, "step").model_dump(mode="json")
    expected.pop("elapsed_ms", None)
    body.pop("elapsed_ms", None)
    assert body == expected


def test_a_notice_that_has_no_per_component_route_says_so():
    for analysis, (available, sentence) in assembly_notice.PER_COMPONENT.items():
        if not available:
            assert "There is no per-component" in sentence, analysis


def test_the_part_cost_of_an_assembly_is_unchanged_and_stored(pump):
    response = _post("/cost/cad-part", pump, "pump.step", **COST_FORM)
    body = response.json()
    stored = client.get(f"/estimates/{body['estimate_id']}").json()
    # The figure is the lump's, as before; the stored estimate has no notice key.
    assert (
        stored["result_snapshot"]["part_cost"]["total_part_cost"]
        == body["part_cost"]["total_part_cost"]
    )
    assert KEY not in stored["result_snapshot"]
    assert "different question from the roll-up" in body[KEY]["per_component"]


def test_dfm_does_not_evaluate_draft_on_an_assembly_as_one_body(pump, single):
    report = _post("/cad/dfm", pump, "pump.step", forming_process="casting").json()
    rules = {entry["rule_id"]: entry for entry in report["rules"]}
    for rule in ("dfm_min_draft", "dfm_undercut"):
        assert rules[rule]["evaluated"] is False
        assert "one body" in rules[rule]["skipped_reason"]
    assert report["draw"]["measured"] is False
    assert not [flag for flag in report["flags"] if flag["rule_id"] in ("dfm_min_draft", "dfm_undercut")]

    part = _post("/cad/dfm", single, "block.step", forming_process="casting").json()
    assert not any("one body" in (entry["skipped_reason"] or "") for entry in part["rules"])
    assert part["draw"]["measured"] is True


# ----------------------------------------------------------------- the refusals


def _refused(response, analysis: str):
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == CODE
    assert assembly_notice.REFUSAL_REASONS[analysis] in detail["message"]
    assert detail[KEY]["code"] == CODE
    assert detail[KEY]["per_component"] in detail["message"]
    return detail


def test_a_pull_direction_is_refused_for_an_assembly_as_one_body(pump):
    detail = _refused(_post("/cad/draw-direction", pump, "pump.step"), "draw_direction")
    assert "component_id" in detail["message"]
    assert detail[KEY]["per_component_available"] is True


def test_cores_are_refused_for_an_assembly_and_say_there_is_no_component_route(pump):
    detail = _refused(_post("/cad/cores/propose", pump, "pump.step"), "cores")
    assert detail[KEY]["per_component_available"] is False
    assert "There is no per-component core proposal yet" in detail["message"]


@pytest.mark.parametrize("path", ["/cad/setups/propose", "/cad/setups/apply-proposal"])
def test_setups_are_refused_across_an_assembly_and_offered_per_component(pump, path):
    plan = _plan_json(pump)
    _refused(_post(path, pump, "pump.step", manufacturing_plan=plan), "setups")


def test_a_component_setup_proposal_is_still_answered(pump):
    structure = _post("/cad/assembly", pump, "pump.step").json()
    housing = next(
        d for d in structure["document"]["assembly"]["definitions"] if d["name"] == "HOUSING"
    )
    plans = _post("/cad/assembly/plans", pump, "pump.step", rate_set_id="poc-default").json()
    entry = next(
        e
        for e in plans["plan"]["component_plans"]
        if e["definition_id"] == housing["definition_id"]
    )
    assert entry["plan"] is not None
    response = _post(
        "/cad/setups/propose",
        pump,
        "pump.step",
        manufacturing_plan=json.dumps(entry["plan"]),
        component_id=housing["definition_id"],
    )
    assert response.status_code == 200, response.text
    assert KEY not in response.json()


@pytest.mark.parametrize("fixture", ["single", "multi_body"])
@pytest.mark.parametrize("path", ["/cad/draw-direction", "/cad/cores/propose"])
def test_the_refused_analyses_still_answer_a_part(request, fixture, path):
    response = _post(path, request.getfixturevalue(fixture), "part.step")
    assert response.status_code == 200, response.text
    assert KEY not in response.json()
    assert "component_id" not in response.json()


@pytest.mark.parametrize("fixture", ["single", "multi_body"])
def test_a_part_setup_proposal_is_still_answered(request, fixture):
    data = request.getfixturevalue(fixture)
    response = _post(
        "/cad/setups/propose", data, "part.step", manufacturing_plan=_plan_json(data)
    )
    assert response.status_code == 200, response.text
    assert KEY not in response.json()


# ------------------------------------------------------- one component's figures


def _definition_id(pump: bytes, name: str) -> str:
    structure = _post("/cad/assembly", pump, "pump.step").json()
    return next(
        d["definition_id"]
        for d in structure["document"]["assembly"]["definitions"]
        if d["name"] == name
    )


def _housing_alone() -> bytes:
    return step_fixtures.to_step(assembly_fixtures._box(*assembly_fixtures.HOUSING))


def _comparable(body: dict) -> dict:
    return {
        key: value
        for key, value in body.items()
        if key not in ("filename", "elapsed_ms", "component_id")
    }


def test_a_components_walls_equal_the_same_component_measured_alone(pump):
    housing = _definition_id(pump, "HOUSING")
    component = _post("/cad/wall-thickness", pump, "pump.step", component_id=housing)
    assert component.status_code == 200, component.text
    body = component.json()
    assert body["component_id"] == housing
    assert KEY not in body
    assert body["measured"] is True

    alone = _post("/cad/wall-thickness", _housing_alone(), "housing.step").json()
    assert "component_id" not in alone
    assert _comparable(body) == _comparable(alone)


def test_a_components_pull_direction_equals_the_same_component_measured_alone(pump):
    housing = _definition_id(pump, "HOUSING")
    component = _post("/cad/draw-direction", pump, "pump.step", component_id=housing)
    assert component.status_code == 200, component.text
    body = component.json()
    assert body["component_id"] == housing
    assert KEY not in body

    alone = _post("/cad/draw-direction", _housing_alone(), "housing.step").json()
    assert _comparable(body) == _comparable(alone)


@pytest.mark.parametrize("path", ["/cad/wall-thickness", "/cad/draw-direction"])
def test_an_unknown_component_is_a_404_and_a_sub_assembly_is_refused(pump, path):
    assert _post(path, pump, "pump.step", component_id="no-such").status_code == 404
    sub = _post(path, pump, "pump.step", component_id=_definition_id(pump, "COVER_ASSY"))
    assert sub.status_code == 400
    assert "sub-assembly" in sub.json()["detail"]


# ------------------------------------------------------------------------ the UI


def _js(name: str) -> str:
    """A script with its comments removed, so prose about a name is not the name."""
    text = (STATIC / name).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)(^|[^:'\"`\\])//.*$", r"\1", text)


CARDS = {
    "features.js": "features-assembly-notice",
    "suggestions.js": "suggestions-assembly-notice",
    "dfm.js": "dfm-assembly-notice",
    "draw.js": "draw-assembly-notice",
    "cores.js": "cores-assembly-notice",
    "sheet.js": "sheet-assembly-notice",
    "app.js": "cost-assembly-notice",
}


def test_every_affected_card_has_a_place_for_the_notice_and_shows_the_servers():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    for script, element in CARDS.items():
        assert page.count(f'id="{element}"') == 1, element
        code = _js(script)
        assert f'showAssemblyNotice("{element}"' in code, script


def test_the_notice_is_drawn_by_one_function_from_the_response_and_escaped():
    app_js = _js("app.js")
    assert app_js.count("function assemblyNoticeMarkup(") == 1
    assert app_js.count("function showAssemblyNotice(") == 1
    start = app_js.index("function assemblyNoticeMarkup(")
    body = app_js[start : app_js.index("function showAssemblyNotice(")]
    assert "escapeHtml(notice.message)" in body
    assert "escapeHtml(notice.see_instead)" in body
    assert "escapeHtml(notice.per_component)" in body
    # Drawn from the notice alone: nothing about the loaded file is consulted.
    assert "state." not in body
    assert "structure_state" not in body
    for script in CARDS:
        if script == "app.js":
            continue
        code = _js(script)
        assert "function assemblyNoticeMarkup" not in code, script
        assert "function showAssemblyNotice" not in code, script


def test_no_script_words_the_notice_or_detects_an_assembly_for_it():
    for script in CARDS:
        code = _js(script)
        assert "fused together" not in code, script
        assert "not a finding about any part" not in code, script
        assert "assembly_measured_as_one_body" not in code, script


def test_the_cost_notice_survives_beside_uncosted_operations(pump, single):
    """An uncommitted plan adds its own keys to the response; the notice rides with them."""
    body = _post(
        "/cost/cad-part", pump, "pump.step", manufacturing_plan=_plan_json(pump), **COST_FORM
    ).json()
    assert body["uncosted_operations"]
    assert body["uncosted_note"]
    assert body[KEY]["code"] == CODE

    part = _post(
        "/cost/cad-part", single, "block.step", manufacturing_plan=_plan_json(single), **COST_FORM
    ).json()
    assert part["uncosted_operations"]
    assert KEY not in part
