"""Sheet metal measured from the B-Rep: thickness, bends, cut contours, flat pattern.

What is checked: every fixture's thickness, bends, flat size, cut perimeter and
pierce count against a hand calculation from the numbers it was built with;
that the K-factor is an input with no default (absent: no flat size, every
K-independent figure still reported); that a part sheet cannot make is refused
with its reason, and a sheet this measurement cannot unfold is reported as
unsupported rather than approximated; that the same file always gives the same
answer wherever the part sits; and the boundaries — the measurement imports no
OpenCASCADE and nothing above the feature layer, and the endpoint stores nothing.
"""

from __future__ import annotations

import ast
import io
import math
import tokenize
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.features.sheet_metal import (
    MAX_THICKNESS_RATIO,
    analyse_sheet_metal,
    validate_k_factor,
)
from cad_costing_agent.features.topology import build_topology
from cad_costing_agent.main import app
from tests import sheet_fixtures, step_fixtures
from tests.sheet_fixtures import INNER_RADIUS, THICKNESS, WIDTH

client = TestClient(app)
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"

T, R, W = THICKNESS, INNER_RADIUS, WIDTH


def ba(k: float, degrees: float = 90.0) -> float:
    """The hand bend allowance: θ (r + K t)."""
    return math.radians(degrees) * (R + k * T)


@lru_cache(maxsize=None)
def step(name: str) -> bytes:
    builder = sheet_fixtures.SHEET_FIXTURES.get(name) or getattr(step_fixtures, name)
    return builder()


def measure(name: str, k: float | None = None) -> dict:
    data = {} if k is None else {"k_factor": str(k)}
    response = client.post(
        "/cad/sheet-metal",
        files={"file": (f"{name}.step", step(name), "application/octet-stream")},
        data=data,
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- 1. exact answers ---------------------------------------------------------


def test_a_flat_plate_with_two_holes_has_no_bends_and_three_contours():
    body = measure("flat_plate_two_holes", 0.4)
    assert body["status"] == "sheet" and body["is_sheet"] is True
    assert body["thickness_mm"] == 2.0
    assert body["bends"] == [] and body["bend_count"] == 0
    flat = body["flat_pattern"]
    # 2 x (100 + 60) + 2 x π x 10.
    assert flat["cut_perimeter_mm"] == pytest.approx(320.0 + 20.0 * math.pi, abs=1e-6)
    assert flat["outer_perimeter_mm"] == pytest.approx(320.0, abs=1e-6)
    assert flat["inner_perimeter_mm"] == pytest.approx(20.0 * math.pi, abs=1e-6)
    # The outline and two holes, each started with one pierce.
    assert flat["contour_count"] == 3 and flat["pierce_count"] == 3
    assert body["pierce_count"] == 3
    assert flat["outline_area_mm2"] == pytest.approx(6000.0, abs=1e-6)
    assert flat["net_area_mm2"] == pytest.approx(6000.0 - 50.0 * math.pi, abs=1e-6)
    assert (flat["rectangle_length_mm"], flat["rectangle_width_mm"]) == pytest.approx((100.0, 60.0))


@pytest.mark.parametrize("k", [0.33, 0.4, 0.5])
def test_an_l_bracket_unfolds_to_its_flanges_plus_one_bend_allowance(k):
    body = measure("l_bracket", k)
    assert body["status"] == "sheet"
    assert body["thickness_mm"] == 2.0
    (bend,) = body["bends"]
    assert bend["angle_deg"] == pytest.approx(90.0)
    assert bend["inner_radius_mm"] == pytest.approx(R)
    assert bend["outer_radius_mm"] == pytest.approx(R + T)
    assert bend["length_mm"] == pytest.approx(W)
    assert bend["bend_allowance_mm"] == pytest.approx(ba(k), abs=1e-6)
    assert bend["flanges"] == ["F-1", "F-2"]
    flat_length = 50.0 + 30.0 + ba(k)
    flat = body["flat_pattern"]
    assert flat["k_factor"] == k and flat["k_factor_source"] == "entered"
    # Measured along the bend line, then across it.
    assert flat["rectangle_length_mm"] == pytest.approx(W, abs=1e-6)
    assert flat["rectangle_width_mm"] == pytest.approx(flat_length, abs=1e-6)
    assert flat["cut_perimeter_mm"] == pytest.approx(2 * flat_length + 2 * W, abs=1e-6)
    assert flat["net_area_mm2"] == pytest.approx(flat_length * W, abs=1e-6)
    assert flat["contour_count"] == 1 and flat["pierce_count"] == 1


def test_a_u_channel_unfolds_with_two_bend_allowances():
    body = measure("u_channel", 0.4)
    assert [b["bend_id"] for b in body["bends"]] == ["B-1", "B-2"]
    assert all(b["angle_deg"] == pytest.approx(90.0) for b in body["bends"])
    flat_length = 40.0 + 60.0 + 40.0 + 2 * ba(0.4)
    flat = body["flat_pattern"]
    assert flat["rectangle_width_mm"] == pytest.approx(flat_length, abs=1e-6)
    assert flat["cut_perimeter_mm"] == pytest.approx(2 * flat_length + 2 * W, abs=1e-6)
    assert flat["net_area_mm2"] == pytest.approx(flat_length * W, abs=1e-6)
    # The middle web is the largest flange, so it is F-1 and both bends start from it.
    assert body["flanges"][0]["area_mm2"] == pytest.approx(60.0 * W)
    assert {tuple(b["flanges"]) for b in body["bends"]} == {("F-1", "F-2"), ("F-1", "F-3")}


def test_a_hole_in_a_flange_unfolds_with_its_flange():
    plain = measure("l_bracket", 0.4)["flat_pattern"]
    holed = measure("l_bracket_with_flange_hole", 0.4)["flat_pattern"]
    # The blank's outline is unchanged; the Ø8 hole is one more contour inside it.
    assert holed["outline_area_mm2"] == pytest.approx(plain["outline_area_mm2"], abs=1e-6)
    assert holed["rectangle_width_mm"] == pytest.approx(plain["rectangle_width_mm"], abs=1e-6)
    assert holed["net_area_mm2"] == pytest.approx(plain["net_area_mm2"] - 16.0 * math.pi, abs=1e-6)
    assert holed["inner_perimeter_mm"] == pytest.approx(8.0 * math.pi, abs=1e-6)
    assert holed["cut_perimeter_mm"] == pytest.approx(plain["cut_perimeter_mm"] + 8.0 * math.pi, abs=1e-6)
    assert holed["contour_count"] == 2 and holed["pierce_count"] == 2


@pytest.mark.parametrize(
    "name, flat_length",
    [
        ("z_bracket", 30.0 + 25.0 + 35.0 + 2 * ba(0.4)),
        ("bend_45", 40.0 + 40.0 + ba(0.4, 45.0)),
    ],
)
def test_opposite_bends_and_a_45_degree_bend_unfold_by_the_same_formula(name, flat_length):
    flat = measure(name, 0.4)["flat_pattern"]
    assert flat["rectangle_width_mm"] == pytest.approx(flat_length, abs=1e-6)
    assert flat["net_area_mm2"] == pytest.approx(flat_length * W, abs=1e-6)


def test_where_the_part_sits_changes_nothing():
    upright = measure("u_channel", 0.4)
    turned = measure("rotated_u_channel", 0.4)
    for key in ("rectangle_length_mm", "rectangle_width_mm", "net_area_mm2", "cut_perimeter_mm"):
        assert turned["flat_pattern"][key] == pytest.approx(upright["flat_pattern"][key], abs=1e-5)
    assert [b["bend_allowance_mm"] for b in turned["bends"]] == pytest.approx(
        [b["bend_allowance_mm"] for b in upright["bends"]]
    )


def test_an_iges_solid_measures_exactly_as_the_step_does():
    from tests.iges_fixtures import to_iges

    data = to_iges(sheet_fixtures.sheet_profile_solid(sheet_fixtures.L_BRACKET))
    response = client.post(
        "/cad/sheet-metal",
        files={"file": ("l.igs", data, "application/octet-stream")},
        data={"k_factor": "0.4"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_format"] == "iges"
    assert body["flat_pattern"]["cut_perimeter_mm"] == pytest.approx(
        measure("l_bracket", 0.4)["flat_pattern"]["cut_perimeter_mm"], abs=1e-5
    )


# --- 2. the K-factor is the engineer's -------------------------------------------


def test_without_a_k_factor_there_is_no_flat_size_and_nothing_is_assumed():
    body = measure("u_channel")
    assert body["status"] == "sheet"
    assert body["k_factor"] is None
    assert body["flat_pattern"] is None
    assert body["needs_input"] == ["k_factor"]
    assert "no default is assumed" in body["flat_pattern_note"]
    assert all(b["bend_allowance_mm"] is None for b in body["bends"])
    assert body["bends"][0]["bend_allowance_formula"] == "BA = 1.5708 × (3 + K × 2)"
    # Everything that does not depend on K is still measured.
    assert body["thickness_mm"] == 2.0 and body["bend_count"] == 2
    assert body["contour_count"] == 1 and body["pierce_count"] == 1
    assert body["flange_edge_cut_length_mm"] == pytest.approx(2 * 140.0 + 2 * W)
    assert body["bend_end_count"] == 4


def test_the_cut_perimeter_is_the_flange_edges_plus_one_allowance_per_bend_end():
    without = measure("u_channel")
    for k in (0.3, 0.45):
        flat = measure("u_channel", k)["flat_pattern"]
        assert flat["cut_perimeter_mm"] == pytest.approx(
            without["flange_edge_cut_length_mm"] + without["bend_end_count"] * ba(k), abs=1e-6
        )


@pytest.mark.parametrize("k", ["0", "-0.2", "0.51", "1.0", "nan"])
def test_a_k_factor_outside_its_physical_range_is_refused(k):
    response = client.post(
        "/cad/sheet-metal",
        files={"file": ("l.step", step("l_bracket"), "application/octet-stream")},
        data={"k_factor": k},
    )
    assert response.status_code in (400, 422)


def test_the_k_factor_validator_keeps_none_as_none():
    assert validate_k_factor(None) is None
    assert validate_k_factor(0.5) == 0.5
    with pytest.raises(ValueError, match="at most 0.5"):
        validate_k_factor(0.6)
    with pytest.raises(ValueError):
        validate_k_factor(True)


# --- 3. refusals ----------------------------------------------------------------


def test_a_block_is_not_a_sheet():
    body = measure("thick_block", 0.4)
    assert body["status"] == "not_sheet" and body["is_sheet"] is False
    assert "not sheet" in body["reasons"][0] and "20 mm thick" in body["reasons"][0]
    assert body["flat_pattern"] is None and body["bends"] == []


def test_a_plate_that_changes_thickness_is_not_a_sheet():
    body = measure("stepped_plate", 0.4)
    assert body["status"] == "not_sheet"
    assert "2 and 4 mm apart: the thickness varies" in body["reasons"][0]
    assert body["flat_pattern"] is None


def test_a_pocket_is_refused_by_the_classifiers_own_rule():
    body = measure("thin_plate_with_pocket", 0.4)
    assert body["status"] == "not_sheet"
    assert body["reasons"] == [
        "PKT-001 (pocket (blind) 40 × 20 mm depth 1 mm) cannot be produced by cutting and "
        "bending sheet — the same rule the route recommendation applies."
    ]


@pytest.mark.parametrize(
    "name, words",
    [
        ("bent_sheet_bracket", "sharp corner with no bend radius"),
        ("hemmed_strip", "a hem or curl"),
        ("closed_tube", "closed ring"),
    ],
)
def test_a_sheet_this_cannot_unfold_is_unsupported_not_approximated(name, words):
    body = measure(name, 0.4)
    assert body["status"] == "unsupported"
    assert words in " ".join(body["reasons"])
    assert body["flat_pattern"] is None


def test_a_faceted_export_and_an_iges_surface_model_are_refused_with_the_reason():
    from tests.faceted_fixtures import faceted_step_from_shape
    from tests.iges_fixtures import block_with_hole_surfaces

    faceted = faceted_step_from_shape(sheet_fixtures.sheet_profile_solid(sheet_fixtures.L_BRACKET))
    response = client.post(
        "/cad/sheet-metal", files={"file": ("l.step", faceted, "application/octet-stream")}
    )
    assert response.status_code == 400
    assert "faceted export" in response.json()["detail"]

    response = client.post(
        "/cad/sheet-metal",
        files={"file": ("b.igs", block_with_hole_surfaces(), "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "loose trimmed surfaces" in response.json()["detail"]


def test_an_stl_is_refused():
    stl = b"solid x\nendsolid x\n"
    response = client.post("/cad/sheet-metal", files={"file": ("x.stl", stl, "application/octet-stream")})
    assert response.status_code == 400


# --- 4. determinism and boundaries ---------------------------------------------


def test_the_same_file_gives_the_same_answer():
    first = client.post(
        "/cad/sheet-metal",
        files={"file": ("u.step", step("u_channel"), "application/octet-stream")},
        data={"k_factor": "0.4"},
    )
    second = client.post(
        "/cad/sheet-metal",
        files={"file": ("u.step", step("u_channel"), "application/octet-stream")},
        data={"k_factor": "0.4"},
    )
    assert first.content == second.content


def test_measuring_stores_nothing():
    before = client.get("/estimates").json()["total"]
    measure("l_bracket", 0.4)
    assert client.get("/estimates").json()["total"] == before


def test_the_measurement_can_be_called_on_a_scan_directly():
    topology = build_topology(read_step_shape(step("l_bracket")))
    result = analyse_sheet_metal(topology, k_factor=0.4)
    assert result.status == "sheet" and result.flat_pattern is not None


def test_the_thinness_rule_is_the_classifiers():
    from cad_costing_agent.process_classification import MAX_WALL_THICKNESS_RATIO

    assert MAX_THICKNESS_RATIO == MAX_WALL_THICKNESS_RATIO


def _code_without_prose(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    docstrings = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add((first.lineno, first.col_offset))
    kept = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and token.start in docstrings:
            continue
        kept.append(token.string)
    return "\n".join(kept)


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_measurement_imports_only_the_topology_scan_and_no_opencascade():
    path = PACKAGE / "features" / "sheet_metal.py"
    own = {m for m in _imports(path) if m.startswith("cad_costing_agent")}
    assert own == {"cad_costing_agent.features.topology"}
    assert not any(m.startswith("OCP") for m in _imports(path))
    code = _code_without_prose(path)
    for forbidden in ("rate_set", "RateSet", "money", "Money", "price", "currency"):
        assert forbidden not in code


def test_the_measurement_never_reaches_money():
    readers = {
        str(path.relative_to(PACKAGE))
        for path in PACKAGE.rglob("*.py")
        if "cad_costing_agent.features.sheet_metal" in _imports(path)
        or "analyse_sheet_metal_from_bytes" in _code_without_prose(path)
    }
    costing = {"costing.py", "cad_part_cost.py", "part_cost.py", "operation_costing.py", "stage_costing.py"}
    assert not readers & costing
