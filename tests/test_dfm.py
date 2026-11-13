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
    # Every rule but the one the profile sets (the minimum-wall rule joined RuleId in 1.1.0).
    assert len(skipped) == len(RuleId) - 1 and all(r.skipped_reason for r in skipped)


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


def test_the_summary_says_exceeded_and_never_calls_an_unmeasured_part_clean():
    flagged = _dfm(_deep_hole_plate()).json()
    assert flagged["summary"].startswith("1 flag(s)")

    clean = _dfm(step_fixtures.block_with_through_hole()).json()
    assert clean["measurements_checked"] > 0
    assert "none exceeded its threshold" in clean["summary"]
    assert "passed" not in clean["summary"]

    # Pinned to 1.0.0, which has no wall rule: under 1.1.0 the plain block's walls are
    # measured, so the block is no longer a part with nothing measurable.
    nothing = _dfm(step_fixtures.plain_block(), dfm_profile_version="1.0.0").json()
    assert nothing["measurements_checked"] == 0
    assert "not a clean bill of health" in nothing["summary"]
    assert {rule["label"] for rule in nothing["rules"]} == {
        "Deep holes",
        "Deep, narrow pockets or slots",
        "Small internal radii",
        "Many different hole sizes",
        "Thin walls",
        "Low draft (cast, moulded or forged parts)",
        "Undercuts (cast, moulded or forged parts)",
    }


def test_the_dfm_panel_adds_nothing_up():
    source = (SRC / "static" / "dfm.js").read_text(encoding="utf-8")
    code = "\n".join(line.split("//")[0] for line in source.splitlines())
    for fragment in ("reduce(", "sum +", " * ", "Math.", "toFixed"):
        assert fragment not in code, fragment


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
    assert [(p["dfm_profile_id"], p["version"]) for p in listing] == [
        (DEFAULT_DFM_PROFILE_ID, "1.0.0"),
        (DEFAULT_DFM_PROFILE_ID, "1.1.0"),
        (DEFAULT_DFM_PROFILE_ID, "1.2.0"),
    ]
    detail = client.get(f"/dfm-profiles/{DEFAULT_DFM_PROFILE_ID}").json()
    assert all(r["source_url"] for r in detail["rules"] if r["basis"] == BASIS_PUBLISHED)
    assert client.get("/dfm-profiles/unknown").status_code == 404


def test_a_new_version_changes_the_flags_and_the_old_version_is_untouched():
    data = _deep_hole_plate()
    assert _dfm(data).json()["flags"]  # ratio 5 > 4 under 1.0.0

    # 1.1.0 (minimum wall) and 1.2.0 (draft, undercuts) are seeded, so the shop's version is 1.3.0.
    created = client.post(f"/dfm-profiles/{DEFAULT_DFM_PROFILE_ID}/versions", json=_new_version("1.3.0", 6.0))
    assert created.status_code == 201

    latest = _dfm(data).json()  # no version -> latest
    assert latest["dfm_profile_version"] == "1.3.0"
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
    # 1.2.0 is seeded, so the next version a shop can publish is 1.3.0.
    assert client.post(
        f"/dfm-profiles/{DEFAULT_DFM_PROFILE_ID}/versions", json=_new_version("1.3.0", 1.0)
    ).status_code == 201
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
        assert c.execute(text("select version_num from alembic_version")).scalar_one() == "0011"
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


# --- flags pointed at on the 3D model -----------------------------------------------------------


def _three_rule_block() -> bytes:
    """A deep hole, a deep narrow slot and seven hole diameters in one block."""
    shape = _cut(_box(160.0, 100.0, 40.0), _box(60.0, 5.0, 40.0, at=(10.0, 20.0, 10.0)))
    shape = _cut(shape, _cylinder(2.5, 40.0, (20.0, 70.0, 15.0)))
    for i, d in enumerate((10.0, 11.0, 12.0, 13.0, 14.0, 15.0)):
        shape = _cut(shape, _cylinder(d / 2, 60.0, (50.0 + 20.0 * i, 70.0, -10.0)))
    return to_step(shape)


def _js_code(name: str) -> str:
    from cad_costing_agent.main import STATIC_PATH

    source = (STATIC_PATH / name).read_text(encoding="utf-8")
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL))


def test_the_report_hands_back_each_flagged_feature_once_unchanged_and_in_recognition_order():
    features = [_hole("H-001", 5, 30), _hole("H-002", 9, 9)] + [_hole(f"H-1{i}", 3.0 + i, 5.0) for i in range(6)]
    report = evaluate_dfm(features, SEED)
    assert {f.rule_id for f in report.flags} == {RuleId.DEEP_HOLE, RuleId.MANY_HOLE_DIAMETERS}
    # H-001 is named by two flags and still appears once.
    assert [f.feature_id for f in report.flagged_features] == [f.feature_id for f in features]
    assert all(returned is given for returned, given in zip(report.flagged_features, features))
    # A feature no flag names is not handed back.
    quiet = evaluate_dfm([_hole("H-001", 5, 30), _hole("H-002", 9, 9)], SEED)
    assert [f.feature_id for f in quiet.flagged_features] == ["H-001"]
    assert evaluate_dfm([_hole("H-002", 9, 9)], SEED).flagged_features == []


def test_every_flag_names_features_the_same_request_recognized():
    data = _three_rule_block()
    body = _dfm(data).json()
    assert {f["rule_id"] for f in body["flags"]} == {
        "dfm_deep_hole",
        "dfm_deep_narrow_pocket_or_slot",
        "dfm_many_distinct_hole_diameters",
    }
    returned = {f["feature_id"]: f for f in body["flagged_features"]}
    assert len(returned) == len(body["flagged_features"])
    named = set()
    for flag in body["flags"]:
        assert flag["feature_ids"], flag["rule_id"]
        for feature_id in flag["feature_ids"]:
            assert feature_id in returned, (flag["rule_id"], feature_id)
            named.add(feature_id)
    assert named == set(returned)

    # They are recognition's own features: the same objects `/cad/features` reports for
    # this file, location and axis included, so a marker is drawn where the table says.
    recognized = client.post(
        "/cad/features", files={"file": ("part.step", data, "application/octet-stream")}
    ).json()["features"]
    by_id = {f["feature_id"]: f for f in recognized}
    for feature_id, feature in returned.items():
        assert feature == by_id[feature_id]
        assert feature["location"] is not None
    assert [f["feature_id"] for f in body["flagged_features"]] == [
        f["feature_id"] for f in recognized if f["feature_id"] in returned
    ]


def test_the_endpoint_report_is_deterministic_including_the_flagged_features():
    data = _three_rule_block()
    assert _dfm(data).json() == _dfm(data).json()


def test_a_part_with_no_flags_hands_back_no_features():
    assert _dfm(step_fixtures.block_with_through_hole()).json()["flagged_features"] == []


def test_a_flag_is_marked_through_the_existing_selection_from_the_servers_own_features():
    code = _js_code("dfm.js")
    assert "selectFeatures(" in code and "flagged: true" in code
    assert "markFlaggedFeatures" in code and "flagged_features" in code
    assert "clearMarker" in code
    # The features come from the DFM response, so marking does not wait for the
    # features panel: the panel's lookup and its "analyse first" refusal are gone.
    assert "featureById" not in code
    assert "Analyse features first" not in code
    # What was drawn is the viewer's answer, and the panel says when nothing was.
    assert "Nothing could be marked" in code
    assert "No location was recorded" in code
    # The browser picks no feature by a threshold or a measured value of its own.
    assert "threshold >" not in code and "threshold <" not in code
    assert "measured_value >" not in code and "measured_value <" not in code
    assert "dimensions" not in code


def test_flag_rows_are_keyboard_reachable_and_state_what_is_marked_in_words():
    from cad_costing_agent.main import STATIC_PATH

    code = _js_code("dfm.js")
    assert '<button type="button" class="secondary tiny dfm-show" aria-pressed="false">' in code
    assert '"Marked on model"' in code and '"Show on model"' in code
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    markup = re.sub(r"<!--.*?-->", "", markup, flags=re.DOTALL)
    for element_id in ('id="dfm-show-all"', 'id="dfm-clear-marks"', 'id="dfm-marked"'):
        assert markup.count(element_id) == 1, element_id
    assert 'id="dfm-marked" class="hint" role="status"' in markup


def test_there_is_one_flag_colour_and_no_claim_of_face_highlighting():
    from cad_costing_agent.main import STATIC_PATH

    viewer = _js_code("viewer.js")
    assert viewer.count("FLAG_MARKER_COLOR = 0x") == 1
    assert len(re.findall(r"const [A-Z_]*MARKER_COLOR = 0x", viewer)) == 2  # ordinary + flag
    assert "OctahedronGeometry" in viewer  # a different shape, not colour alone
    assert "markFlaggedFeatures," in viewer
    for name in ("dfm.js", "viewer.js"):
        code = _js_code(name).lower()
        for scale in ("severity", "amber", "critical", "0xff0000", "highlights the face", "faces highlighted"):
            assert scale not in code, (name, scale)
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    legend = markup[markup.index('class="dfm-legend"') :]
    legend = legend[: legend.index("</p>")]
    assert "Magenta diamond" in legend and "Orange dot" in legend
    assert "does not outline the feature's faces" in legend
    styles = (STATIC_PATH / "styles.css").read_text(encoding="utf-8")
    styles = re.sub(r"/\*.*?\*/", "", styles, flags=re.DOTALL)
    dfm_colours = set(
        re.findall(
            r"\.dfm-(?:flag\.is-marked|swatch-flag) \{[^}]*?(?:border-color|background): (#[0-9a-f]{6})",
            styles,
        )
    )
    assert dfm_colours == {"#c2185b"}, dfm_colours


def test_the_viewer_decides_nothing_about_which_features_are_flagged():
    viewer = _js_code("viewer.js")
    body = viewer[viewer.index("function markFlaggedFeatures") :]
    body = body[: body.index(chr(10) + "}")]
    for forbidden in ("threshold", "rule_id", "dimensions", "fetch(", "/cad/"):
        assert forbidden not in body, forbidden
