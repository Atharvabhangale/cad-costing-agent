"""Advisory DFM flags: rules, cited versioned thresholds, and the boundary to costing.

The flags are advice. So the tests are about three things: a rule fires only on a
measurement that really exceeds its profile's threshold; every threshold is
cited, versioned and immutable like a time profile; and nothing here can reach a
cost.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from cad_costing_agent import db as db_module
from cad_costing_agent.dfm import (
    BASIS_NONE_FOUND,
    BASIS_PUBLISHED,
    DEFAULT_DFM_PROFILE_ID,
    SEED_DFM_PROFILES,
    DfmProfile,
    DfmRuleSetting,
    RuleId,
    evaluate_dfm,
)
from cad_costing_agent.features.model import Confidence, Feature, FeatureType
from cad_costing_agent.main import app
from tests import step_fixtures
from tests.step_fixtures import _box, _cut, _cylinder, to_step

client = TestClient(app)
SRC = Path(__file__).resolve().parents[1] / "src" / "cad_costing_agent"
SEED = SEED_DFM_PROFILES[0]


def _feature(fid, ftype, dims, *, subtype=None, source="brep") -> Feature:
    return Feature(
        feature_id=fid,
        feature_type=ftype,
        subtype=subtype,
        solid_index=0,
        source_type=source,
        dimensions=dims,
        confidence=Confidence.HIGH,
        detection_method="test",
    )


def _hole(fid, d, depth, source="brep"):
    return _feature(fid, FeatureType.HOLE, {"diameter_mm": d, "depth_mm": depth}, subtype="blind", source=source)


def _dfm(data: bytes, name="part.step", **form):
    return client.post("/cad/dfm", files={"file": (name, data, "application/octet-stream")}, data=form)


# --- the seed profile ---------------------------------------------------------


def test_every_published_threshold_cites_a_title_and_url_and_the_rest_say_so():
    for profile in SEED_DFM_PROFILES:
        assert profile.is_illustrative
        for rule in profile.rules:
            if rule.basis == BASIS_PUBLISHED:
                assert rule.source_title and rule.source_url.startswith("https://")
            else:
                assert rule.basis == BASIS_NONE_FOUND
                assert "no count" in (rule.source_quote or "") or "illustrative" in rule.source_quote


def test_a_threshold_cannot_claim_a_published_basis_without_a_source():
    with pytest.raises(ValueError):
        DfmRuleSetting(rule_id=RuleId.DEEP_HOLE, threshold=4, unit="ratio", basis=BASIS_PUBLISHED)


# --- rules on hand-built features (no CAD needed) ---------------------------------


def test_deep_hole_fires_only_strictly_above_the_threshold():
    report = evaluate_dfm([_hole("H-001", 5, 20), _hole("H-002", 5, 25), _hole("H-003", 5, 15)], SEED)
    flagged = [f for f in report.flags if f.rule_id is RuleId.DEEP_HOLE]
    assert [f.feature_ids for f in flagged] == [["H-002"]]
    flag = flagged[0]
    assert flag.measured_value == 5.0 and flag.threshold == 4.0
    assert flag.dfm_profile_id == DEFAULT_DFM_PROFILE_ID and flag.dfm_profile_version == "1.0.0"
    assert flag.source_url and flag.evidence_type == "brep" and flag.evidence_note is None


def test_faceted_evidence_is_labelled_weaker():
    report = evaluate_dfm([_hole("H-001", 5, 30, source="faceted")], SEED)
    (flag,) = report.flags
    assert flag.evidence_type == "faceted"
    assert "polygons" in flag.evidence_note


def test_deep_narrow_recess_uses_depth_over_width_for_pockets_and_slots_only():
    slot = _feature("S-001", FeatureType.SLOT, {"width_mm": 5.0, "depth_mm": 30.0, "length_mm": 60.0})
    ok = _feature("P-001", FeatureType.POCKET, {"width_mm": 30.0, "depth_mm": 8.0})
    plane = _feature("PL-001", FeatureType.PLANE, {"width_mm": 1.0, "depth_mm": 99.0})
    report = evaluate_dfm([slot, ok, plane], SEED)
    flagged = [f for f in report.flags if f.rule_id is RuleId.DEEP_NARROW_RECESS]
    assert [f.feature_ids for f in flagged] == [["S-001"]]
    assert flagged[0].measured_value == 6.0


def test_small_internal_radius_looks_only_at_concave_fillets():
    concave = _feature("F-001", FeatureType.FILLET, {"radius_mm": 0.3}, subtype="concave")
    convex = _feature("F-002", FeatureType.FILLET, {"radius_mm": 0.3}, subtype="convex")
    big = _feature("F-003", FeatureType.FILLET, {"radius_mm": 2.0}, subtype="concave")
    report = evaluate_dfm([concave, convex, big], SEED)
    flagged = [f for f in report.flags if f.rule_id is RuleId.SMALL_INTERNAL_RADIUS]
    assert [f.feature_ids for f in flagged] == [["F-001"]]
    assert flagged[0].comparison == "less_than"


def test_many_distinct_hole_diameters_counts_diameters_not_holes():
    same = [_hole(f"H-{i}", 5.0, 5.0) for i in range(10)]
    assert not evaluate_dfm(same, SEED).flags
    varied = [_hole(f"H-{i}", 3.0 + i, 5.0) for i in range(7)]
    (flag,) = evaluate_dfm(varied, SEED).flags
    assert flag.rule_id is RuleId.MANY_HOLE_DIAMETERS
    assert flag.measured_value == 7.0 and len(flag.feature_ids) == 7
    assert flag.basis == BASIS_NONE_FOUND and flag.source_url is None


def test_a_feature_missing_a_dimension_is_not_checked_rather_than_guessed():
    report = evaluate_dfm([_feature("H-001", FeatureType.HOLE, {"diameter_mm": 5.0})], SEED)
    deep = next(r for r in report.rules if r.rule_id is RuleId.DEEP_HOLE)
    assert deep.features_checked == 0 and not report.flags


def test_a_rule_absent_from_a_profile_is_reported_as_not_evaluated():
    profile = DfmProfile(
        dfm_profile_id="only-holes", name="x", version="1.0.0", rules=[SEED.setting(RuleId.DEEP_HOLE)]
    )
    report = evaluate_dfm([_hole("H-001", 5, 30)], profile)
    skipped = [r for r in report.rules if not r.evaluated]
    assert len(skipped) == 3 and all(r.skipped_reason for r in skipped)


def test_evaluation_is_deterministic():
    features = [_hole("H-001", 5, 30), _hole("H-002", 3, 20)]
    assert evaluate_dfm(features, SEED).model_dump() == evaluate_dfm(features, SEED).model_dump()


# --- real recognition through the endpoint ------------------------------------------


def _deep_hole_plate() -> bytes:
    return to_step(_cut(_box(40.0, 40.0, 30.0), _cylinder(2.5, 40.0, (20.0, 20.0, 5.0))))  # 25 deep, d 5


def _deep_slot_block() -> bytes:
    return to_step(_cut(_box(80.0, 50.0, 40.0), _box(60.0, 5.0, 40.0, at=(10.0, 20.0, 10.0))))  # 30 deep, 5 wide


def test_endpoint_flags_a_deep_hole_from_a_real_step_file():
    response = _dfm(_deep_hole_plate())
    assert response.status_code == 200
    body = response.json()
    flags = [f for f in body["flags"] if f["rule_id"] == "dfm_deep_hole"]
    assert len(flags) == 1
    assert flags[0]["measured_value"] == pytest.approx(5.0, abs=0.01)
    assert flags[0]["evidence_type"] == "brep"
    assert "Advisory only" in body["disclaimer"]


def test_endpoint_flags_a_deep_narrow_slot():
    flags = [f for f in _dfm(_deep_slot_block()).json()["flags"] if f["rule_id"] == "dfm_deep_narrow_pocket_or_slot"]
    assert len(flags) == 1
    assert flags[0]["measured_value"] == pytest.approx(6.0, abs=0.01)


def test_a_plain_block_and_a_shallow_hole_raise_no_flags():
    assert _dfm(step_fixtures.plain_block()).json()["flags"] == []
    assert _dfm(step_fixtures.block_with_through_hole()).json()["flags"] == []


def test_stl_is_refused_like_the_feature_endpoint():
    assert _dfm(b"solid x\nendsolid x\n", name="x.stl").status_code == 400


def test_unknown_profile_is_404():
    assert _dfm(step_fixtures.plain_block(), dfm_profile_id="nope").status_code == 404


# --- versioned, immutable, editable by publishing -------------------------------------


def _new_version(version: str, deep_hole: float, rule_overrides=None) -> dict:
    rules = [r.model_dump(mode="json") for r in SEED.rules]
    for rule in rules:
        if rule["rule_id"] == "dfm_deep_hole":
            rule["threshold"] = deep_hole
    return {"name": "Shop limits", "version": version, "is_illustrative": True, "rules": rules}


def test_profiles_are_listed_and_fetchable_with_sources():
    listing = client.get("/dfm-profiles").json()
    assert [(p["dfm_profile_id"], p["version"]) for p in listing] == [(DEFAULT_DFM_PROFILE_ID, "1.0.0")]
    detail = client.get(f"/dfm-profiles/{DEFAULT_DFM_PROFILE_ID}").json()
    assert all(r["source_url"] for r in detail["rules"] if r["basis"] == BASIS_PUBLISHED)
    assert client.get("/dfm-profiles/unknown").status_code == 404


def test_a_new_version_changes_the_flags_and_the_old_version_is_untouched():
    data = _deep_hole_plate()
    assert _dfm(data).json()["flags"]  # ratio 5 > 4 under 1.0.0

    created = client.post(f"/dfm-profiles/{DEFAULT_DFM_PROFILE_ID}/versions", json=_new_version("1.1.0", 6.0))
    assert created.status_code == 201

    latest = _dfm(data).json()  # no version -> latest
    assert latest["dfm_profile_version"] == "1.1.0"
    assert not [f for f in latest["flags"] if f["rule_id"] == "dfm_deep_hole"]

    pinned = _dfm(data, dfm_profile_version="1.0.0").json()
    assert [f for f in pinned["flags"] if f["rule_id"] == "dfm_deep_hole"]
    old = client.get(f"/dfm-profiles/{DEFAULT_DFM_PROFILE_ID}", params={"version": "1.0.0"}).json()
    assert next(r for r in old["rules"] if r["rule_id"] == "dfm_deep_hole")["threshold"] == 4.0


def test_republishing_a_version_is_a_conflict():
    assert client.post(f"/dfm-profiles/{DEFAULT_DFM_PROFILE_ID}/versions", json=_new_version("1.0.0", 9.0)).status_code == 409


def test_publishing_an_uncited_published_threshold_is_refused():
    body = _new_version("2.0.0", 5.0)
    body["rules"][0].update(source_url=None)
    assert client.post(f"/dfm-profiles/{DEFAULT_DFM_PROFILE_ID}/versions", json=body).status_code == 422


# --- flags change no cost --------------------------------------------------------------


def _cost(data: bytes) -> dict:
    response = client.post(
        "/cost/cad-part",
        files={"file": ("p.step", data, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P1",
            "part_name": "Plate",
            "quantity": "5",
            "operations": "[]",
            "rate_set_id": "poc-default",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    for volatile in ("elapsed_ms", "estimate_id"):
        body.pop(volatile, None)
        body.get("part_cost", {}).pop(volatile, None)
    return body


def test_running_dfm_and_publishing_a_profile_change_no_cost_and_store_no_estimate():
    data = _deep_hole_plate()
    before = _cost(data)
    total_before = client.get("/estimates").json()["total"]

    assert _dfm(data).json()["flags"]
    client.post(f"/dfm-profiles/{DEFAULT_DFM_PROFILE_ID}/versions", json=_new_version("1.2.0", 1.0))
    assert _dfm(data).json()["flags"]

    after = _cost(data)
    assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)
    # Only the second costing call stored an estimate; the DFM calls stored none.
    assert client.get("/estimates").json()["total"] == total_before + 1


# --- migration 0007 ------------------------------------------------------------------------


def _engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def test_migration_0007_creates_dfm_profiles_on_a_fresh_database_and_init_seeds_it():
    engine = _engine()
    db_module.init_db(engine)
    assert "dfm_profiles" in inspect(engine).get_table_names()
    with engine.connect() as c:
        assert c.execute(text("select count(*) from dfm_profiles")).scalar_one() == len(SEED_DFM_PROFILES)


def test_migration_0007_upgrades_an_existing_0006_database_without_touching_data():
    from alembic import command

    engine = _engine()
    with engine.begin() as connection:
        command.upgrade(db_module._alembic_config(connection), "0006")
        assert "dfm_profiles" not in inspect(connection).get_table_names()
        connection.execute(
            text(
                "insert into time_profiles (time_profile_id, version, name, description, "
                "is_illustrative, assumptions, created_at) values "
                "('keep', '1.0.0', 'Keep', '', 1, '{}', '2026-01-01 00:00:00')"
            )
        )
    with engine.connect() as c:
        before = c.execute(text("select * from time_profiles")).fetchall()

    assert db_module.run_migrations(engine) == "upgraded"

    with engine.connect() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar_one() == "0007"
        assert c.execute(text("select * from time_profiles")).fetchall() == before
        assert "dfm_profiles" in inspect(c).get_table_names()


# --- structural boundaries -----------------------------------------------------------------


def _code_without_prose(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("cad_costing_agent"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("cad_costing_agent"))
    return found


def test_dfm_imports_only_the_feature_model_from_this_package():
    assert _imports(SRC / "dfm.py") == {"cad_costing_agent.features.model"}


def test_only_main_repository_and_db_import_dfm():
    importers = set()
    for path in SRC.rglob("*.py"):
        if path.name == "dfm.py":
            continue
        if any(m == "cad_costing_agent.dfm" for m in _imports(path)) or re.search(
            r"from cad_costing_agent import[^\n]*\bdfm\b", path.read_text(encoding="utf-8")
        ):
            importers.add(path.name)
    assert importers <= {"main.py", "repository.py", "db.py"}, importers


def test_dfm_has_no_reach_into_money_time_or_persistence():
    code = _code_without_prose(SRC / "dfm.py")
    for forbidden in ("session", "Repository", "rate_set", "RateSet", "calculate_", "commit(", "TimeAssumptions", "ManufacturingPlan"):
        assert forbidden not in code, forbidden


def test_dfm_never_computes_a_confidence_or_severity():
    code = _code_without_prose(SRC / "dfm.py")
    for forbidden in ("severity", "confidence", "score"):
        assert forbidden not in code.lower().replace("evidence_note", ""), forbidden


def test_no_costing_or_planning_module_imports_dfm():
    for name in ("costing", "part_cost", "operation_costing", "stage_costing", "cad_part_cost",
                 "manufacturing_plan", "operation_suggestions", "operation_time", "quotation",
                 "process_classification", "estimates"):
        assert "cad_costing_agent.dfm" not in _imports(SRC / f"{name}.py"), name


# --- UI wiring -----------------------------------------------------------------------------------


def test_dfm_panel_is_wired_between_actuals_and_estimateflow_and_does_no_arithmetic():
    from cad_costing_agent.main import STATIC_PATH

    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    assert markup.index('src="actuals.js"') < markup.index('src="dfm.js"') < markup.index('src="estimateflow.js"')
    assert 'id="dfm-card"' in markup
    source = (STATIC_PATH / "dfm.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", source)
    assert "/cad/dfm" in code and "selectFeature" in code
    assert not re.search(r"[A-Za-z_\)\]]\s*[*/]\s*[A-Za-z_\(\d]", re.sub(r"`[^`]*`|\"[^\"\n]*\"", "", code)), "no arithmetic in dfm.js"
