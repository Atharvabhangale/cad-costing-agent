"""The feature layer reaches the STEP and IGES readers through one function.

`features.shape_loading.load_exact_shape` turns upload bytes into the exact
B-Rep shape, or a typed reason there is none. Recognition, the wall and
pull-direction measurements, the setup proposal and the sheet-metal
measurement all start from it and word the reason themselves. The structural
tests here are what stop a second copy of the gating from growing back.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import cad_costing_agent
from cad_costing_agent.features import recognize_features_with_shape
from cad_costing_agent.features.shape_loading import NotExact, load_exact_shape
from tests import faceted_fixtures, iges_fixtures, step_fixtures
from tests.assembly_fixtures import pump_assembly

PACKAGE = Path(cad_costing_agent.__file__).parent
LOADER = "features/shape_loading.py"


def _code_without_prose(path: Path) -> str:
    """Source with docstrings and comments stripped, so prose cannot match a grep."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    body.pop(0)
                    if not body:
                        body.append(ast.Pass())
    return ast.unparse(tree)


def _files_naming(token: str) -> set[str]:
    return {
        path.relative_to(PACKAGE).as_posix()
        for path in sorted(PACKAGE.rglob("*.py"))
        if token in _code_without_prose(path)
    }


# ------------------------------------------------------------------ structure


def test_the_step_reader_is_reached_only_by_geometry_the_viewer_and_the_loader():
    assert _files_naming("read_step_shape") == {"cad_geometry.py", "cad_mesh.py", LOADER}


def test_the_iges_reader_is_reached_only_by_geometry_the_viewer_and_the_loader():
    assert _files_naming("read_iges_shape") == {
        "cad_geometry.py",
        "cad_iges.py",
        "cad_mesh.py",
        LOADER,
    }


def test_no_feature_module_but_the_loader_gates_or_reads_a_file():
    """The gating cannot grow back beside the loader, and `main.py` holds none."""
    consumers = sorted((PACKAGE / "features").rglob("*.py")) + [
        PACKAGE / "main.py",
        PACKAGE / "setup_proposal.py",
        PACKAGE / "sheet_operations.py",
        PACKAGE / "dfm.py",
    ]
    assert len(consumers) > 10

    for path in consumers:
        name = path.relative_to(PACKAGE).as_posix()
        if name == LOADER:
            continue
        code = _code_without_prose(path)
        assert "read_step_shape" not in code, name
        assert "read_iges_shape" not in code, name
        assert "ensure_readable" not in code, name
        assert "carries_a_solid" not in code, name
        assert "analyse_iges_shape" not in code, name
        if name != "features/step_text.py":  # where the classifier is defined
            assert "classify_step_bytes" not in code, name


def test_the_loader_itself_calls_each_reader_exactly_once():
    code = _code_without_prose(PACKAGE / LOADER)

    assert code.count("read_step_shape(") == 1
    assert code.count("read_iges_shape(") == 1
    assert code.count("read_step_assembly(") == 1
    assert code.count("ensure_readable(") == 1


def test_every_feature_layer_consumer_goes_through_the_loader():
    assert _files_naming("load_exact_shape(") == {
        LOADER,
        "features/__init__.py",
        "features/approach.py",
    }
    package_init = _code_without_prose(PACKAGE / "features" / "__init__.py")
    # Recognition and sheet metal: one call each, so neither reads a file twice.
    assert package_init.count("load_exact_shape(") == 2


def test_the_loader_measures_and_recognizes_nothing():
    tree = ast.parse((PACKAGE / LOADER).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert {m for m in imported if m.startswith("cad_costing_agent")} == {
        "cad_costing_agent.cad_assembly",
        "cad_costing_agent.cad_geometry",
        "cad_costing_agent.cad_iges",
        "cad_costing_agent.features.step_text",
    }
    code = _code_without_prose(PACKAGE / LOADER)
    for forbidden in ("BRepGProp", "trimesh", "recognize_features", "build_topology", "suffix"):
        assert forbidden not in code, forbidden


# ------------------------------------------------------------------ behaviour


def test_an_analytic_step_file_loads_exact():
    loaded = load_exact_shape(step_fixtures.block_with_through_hole(), "step")

    assert loaded.reason is None
    assert loaded.is_exact
    assert loaded.step_representation is not None


def test_a_faceted_step_file_is_decided_without_a_parse(monkeypatch):
    import cad_costing_agent.features.shape_loading as module

    def refuse(data):  # pragma: no cover - reaching it is the failure
        raise AssertionError("a faceted export must not be handed to the reader")

    monkeypatch.setattr(module, "read_step_shape", refuse)
    loaded = load_exact_shape(faceted_fixtures.faceted_block_with_blind_hole(), "step")

    assert loaded.reason is NotExact.FACETED_STEP
    assert loaded.shape is None
    assert not loaded.is_exact


def test_a_wireframe_step_file_is_decided_without_a_shape():
    wireframe = step_fixtures.plain_block().replace(b"ADVANCED_FACE", b"NOT_A_FACE")

    loaded = load_exact_shape(wireframe, "step")

    assert loaded.reason is NotExact.WIREFRAME_STEP
    assert loaded.shape is None


def test_an_iges_solid_loads_exact_and_a_surface_model_keeps_its_sewn_shape():
    solid = load_exact_shape(iges_fixtures.block_with_hole_solid(), "iges")
    surfaces = load_exact_shape(iges_fixtures.block_with_hole_surfaces(), "iges")

    assert solid.reason is None and solid.is_exact
    assert surfaces.reason is NotExact.IGES_SEWN
    assert surfaces.shape is not None
    assert not surfaces.is_exact


def test_a_caller_with_no_use_for_a_surface_model_is_answered_before_the_parse(monkeypatch):
    import cad_costing_agent.cad_iges as iges_module

    def refuse(data):  # pragma: no cover - reaching it is the failure
        raise AssertionError("a surface model must not be parsed for this caller")

    monkeypatch.setattr(iges_module, "read_iges_shape", refuse)
    loaded = load_exact_shape(
        iges_fixtures.block_with_hole_surfaces(), "iges", analytic_solid_only=True
    )

    assert loaded.reason is NotExact.IGES_SURFACE_MODEL
    assert loaded.shape is None


def test_an_iges_file_with_no_unit_is_still_refused_by_the_one_guard():
    with pytest.raises(ValueError):
        load_exact_shape(
            iges_fixtures.without_unit_declaration(iges_fixtures.box_solid()), "iges"
        )


def test_a_mesh_format_is_read_by_nothing():
    loaded = load_exact_shape(b"solid x\nendsolid x\n", "stl")

    assert loaded.reason is NotExact.NOT_A_BREP_FORMAT
    assert loaded.shape is None


def test_a_component_is_loaded_from_the_product_structure_or_named_unknown():
    data = pump_assembly()
    missing = load_exact_shape(data, "step", component_id="NO_SUCH_PART")

    assert missing.reason is NotExact.UNKNOWN_COMPONENT

    from cad_costing_agent.cad_assembly import read_step_assembly

    definitions = read_step_assembly(data).definitions
    component_id = next(key for key, d in definitions.items() if d.has_geometry)
    found = load_exact_shape(data, "step", component_id=component_id)

    assert found.is_exact
    assert found.component is not None


@pytest.mark.parametrize("consumer", ["recognition", "sheet_metal", "setup_proposal"])
def test_each_consumer_parses_an_analytic_step_file_exactly_once(monkeypatch, consumer):
    import cad_costing_agent.features.shape_loading as module
    from cad_costing_agent.features import analyse_sheet_metal_from_bytes
    from cad_costing_agent.features.approach import shape_and_features

    calls = []
    original = module.read_step_shape

    def counting(data):
        calls.append(len(data))
        return original(data)

    monkeypatch.setattr(module, "read_step_shape", counting)
    data = step_fixtures.block_with_through_hole()

    if consumer == "recognition":
        recognize_features_with_shape(data, "block.step")
    elif consumer == "sheet_metal":
        analyse_sheet_metal_from_bytes(data, "block.step")
    else:
        shape_and_features(data, "step")

    assert len(calls) == 1
