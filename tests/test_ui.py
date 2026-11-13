"""UI serving, and the exact API contracts the browser app depends on.

There is no JS runtime here, so these cover the two things that can actually
break the UI: the static assets not being served, and the request shapes the
frontend sends drifting from what the API accepts.
"""

import json
import re
from pathlib import Path

import pytest
import trimesh
from fastapi.testclient import TestClient

from cad_costing_agent.main import STATIC_PATH, app

client = TestClient(app)

RATE_SET = "poc-machine-shop"


def stl_bytes(x=10.0, y=20.0, z=30.0) -> bytes:
    return trimesh.creation.box(extents=(x, y, z)).export(file_type="stl")


def base_form(**overrides) -> dict:
    """Exactly the fields app.js always sends."""
    form = {
        "material": "Steel",
        "source_unit": "mm",
        "part_id": "P-001",
        "part_name": "Bracket",
        "quantity": "10",
        "rate_set_id": RATE_SET,
    }
    form.update(overrides)
    return form


def calculate(form: dict, filename: str = "cube.stl", data: bytes | None = None):
    return client.post(
        "/cost/cad-part",
        files={"file": (filename, data if data is not None else stl_bytes(), "application/octet-stream")},
        data=form,
    )


# --- the UI is served -------------------------------------------------------


def test_root_redirects_to_the_ui():
    response = client.get("/", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/ui/"


def test_ui_index_is_served():
    response = client.get("/ui/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "CAD Costing Agent" in response.text


@pytest.mark.parametrize("asset", ["app.js", "styles.css", "estimateflow.js"])
def test_ui_assets_are_served(asset):
    response = client.get(f"/ui/{asset}")

    assert response.status_code == 200
    assert response.content


def test_mounting_the_ui_does_not_shadow_the_api():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/rates").status_code == 200


def test_static_files_exist_on_disk():
    for name in ("index.html", "app.js", "styles.css", "estimateflow.js"):
        assert (STATIC_PATH / name).is_file()


def test_the_ui_keeps_no_list_of_cad_formats_of_its_own():
    """Step 23 replaced the hardcoded list with the backend's capability matrix.

    A list of formats in the browser is a second statement of what the
    application supports, and a second statement can disagree with the first —
    advertising a format the backend refuses, or omitting one it gained. The
    upload panel is rendered from `/cad/formats` instead, so there is only one
    statement and the interface cannot drift from it.
    """
    html = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC_PATH / "app.js").read_text(encoding="utf-8")

    # No `accept` attribute enumerating extensions: it is set from the catalog.
    assert "accept=" not in html
    assert 'id="format-support"' in html
    assert '"/cad/formats"' in app_js

    # And no format list survives in the script either.
    for hardcoded in ('["step", "stp", "stl"]', '["step","stp","stl"]', "ACCEPTED ="):
        assert hardcoded not in app_js


def test_the_ui_advertises_exactly_what_the_backend_declares():
    """The panel is data-driven, so this checks the data it will be given."""
    catalog = client.get("/cad/formats").json()

    offered = {
        extension
        for row in catalog
        if row["any_support"]
        for extension in row["extensions"]
    }
    assert offered == {"step", "stp", "stl", "iges", "igs"}

    # Recognized-but-unreadable formats are still listed, so a user with one
    # learns why it is refused rather than finding it silently ignored.
    refused = {row["source_format"] for row in catalog if not row["any_support"]}
    assert refused == {"creo_part", "creo_assembly", "creo_drawing"}


def test_the_estimate_page_is_sectioned_rather_than_one_long_page():
    """One workflow in six sections, and every card lives in exactly one.

    Before this, the estimate tab held eighteen cards, ten of them visible with
    nothing uploaded, and analysing a 39-feature bracket produced 6,621px of
    page — about twelve screens. The cards are unchanged; what changed is that
    only one section is shown at a time.
    """
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    for section in ("overview", "cad", "manufacturing", "cost", "assembly", "quote"):
        assert markup.count(f'data-pane="{section}"') == 1
        assert f'data-flow="{section}"' in markup

    # Every pane but the first ships hidden, so nothing depends on the router
    # having run before the first paint.
    assert markup.count('class="flow-pane hidden"') == 5
    assert markup.count('class="flow-pane"') == 1


def test_the_detailed_feature_table_is_behind_a_disclosure():
    """Identifiers, axes and CAD face counts are evidence, not a headline.

    They are what an engineer checks when a suggestion looks wrong, and reading
    them first is what made this page feel like a debugging tool. The table is
    still there, still complete, one click away.
    """
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    detail = markup[markup.index('id="feature-detail-toggle"') :]
    detail = detail[: detail.index("</details>")]
    assert "<summary>View detailed feature analysis" in detail
    for column in (">ID<", ">Axis<", ">CAD faces<"):
        assert column in detail, f"{column} should be inside the disclosure"

    # And a `<details>` is closed unless it says otherwise.
    opening = markup[markup.index('id="feature-detail-toggle"') - 60 :][:120]
    assert " open" not in opening


def test_the_flow_router_decides_nothing_but_where_things_are_shown():
    """`estimateflow.js` may arrange the page; it may not work anything out.

    It shows a cost figure on the overview, so it is exactly the kind of file
    that could grow a "helpful" total of its own. It reads the strings `app.js`
    already rendered instead, which is why the overview cannot disagree with
    the breakdown it summarises.
    """
    router = (STATIC_PATH / "estimateflow.js").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in router.split("\n") if not line.strip().startswith(("//", "*", "/*"))
    )

    for arithmetic in ("machine_rate_per_hour", "cost_per_kg", "* quantity", "/ 100 *"):
        assert arithmetic not in code
    # No format list of its own, for the same reason no other UI file has one.
    for fmt in ('"step"', '"stl"', '"iges"', '"stp"'):
        assert fmt not in code
    # The time-resolution rule has one implementation; this borrows it.
    assert "isCostable" in code
    assert "system_time_accepted" not in code


def test_the_flow_router_loads_after_the_globals_it_borrows():
    """A file may only call into one loaded before it."""
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    router = markup.index('src="estimateflow.js"')
    for earlier in ("app.js", "features.js", "suggestions.js", "plan.js", "assembly.js"):
        assert markup.index(f'src="{earlier}"') < router


def test_a_new_file_gets_its_own_default_part_name():
    """The name must describe the file that is loaded, not the one before it.

    `part_name` shipped as a fixed value in the markup and survived every
    upload, so loading an assembly after a part left the previous part's name
    describing it — the overview read "Assembly / Bracket". The default now
    comes from the file, and follows the rule the rest of this application
    follows for anything the system fills in: it is applied only while nobody
    has said otherwise, so a typed name is never overwritten.
    """
    app_js = (STATIC_PATH / "app.js").read_text(encoding="utf-8")

    assert "function defaultPartNameFor" in app_js
    assert "function applyDefaultPartName" in app_js
    assert "applyDefaultPartName(file.name)" in app_js

    # The suggestion is skipped when the field holds something a person typed.
    assert "state.autoPartName" in app_js
    assert "INITIAL_PART_NAME" in app_js

    # Nothing about the file's kind is baked in: the name is the file's name.
    body = app_js[app_js.index("function defaultPartNameFor") :]
    body = body[: body.index("function refreshCalculateState")]
    for invented in ('"Assembly"', "'Assembly'", '"Part"', '"Bracket"'):
        assert invented not in body


def _router_code():
    """`estimateflow.js` with its prose removed.

    Every rule this file obeys is also *described* in it at length, so a naive
    grep for a forbidden idiom matches the comment promising the idiom is
    absent. Strip the comments, then look.
    """
    router = (STATIC_PATH / "estimateflow.js").read_text(encoding="utf-8")
    return "\n".join(
        line
        for line in router.split("\n")
        if not line.strip().startswith(("//", "*", "/*"))
    )


def test_one_function_decides_which_estimate_is_current():
    """Cost, Quote and the context bar must not each pick for themselves.

    A file can hold two estimates that answer different questions, and before
    this each screen chose independently: the Quote screen read `state.result`,
    which only `/cost/cad-part` ever sets, so an assembly whose components had
    just been costed was quoted as a single lump of material.
    """
    code = _router_code()

    assert "function authoritativeEstimate" in code
    # Everything that names an estimate asks it, and nothing re-derives one.
    assert code.count("authoritativeEstimate()") >= 4
    assert "function quotableEstimate" not in code, "one authority, one name"


def test_the_cost_section_names_the_assembly_estimate():
    """The Cost section must say which estimate it is looking at.

    The result panel there belongs to `/cost/cad-part` and always has. Beside a
    roll-up it is a second opinion about a different question, and unlabelled it
    reads as the assembly's cost.
    """
    code = _router_code()

    assert "function renderCostAuthority" in code
    assert "renderCostAuthority()" in code, "the banner must be part of the refresh"
    # It prints the authoritative estimate's own id, not the result panel's.
    banner = code[code.index("function renderCostAuthority") :]
    banner = banner[: banner.index("function whatTheResultPanelIs")]
    assert "authoritativeEstimate()" in banner
    assert "escapeHtml(target.id)" in banner
    # And it says what the panel underneath actually is.
    assert "function whatTheResultPanelIs" in code
    assert "costed as one solid part" in code


def test_the_quote_screen_issues_the_authoritative_estimate():
    """Both quotation links carry the same id, whichever kind it is."""
    code = _router_code()

    quote = code[code.index("function renderQuote") :]
    assert "authoritativeEstimate()" in quote
    assert quote.count("quotation.html?estimate=${encodeURIComponent(target.id)}") == 2
    assert "state.result" not in quote, "the Quote screen must not pick an estimate itself"


def test_an_assembly_never_falls_back_to_the_part_estimate():
    """A competing `/cost/cad-part` estimate must not stand in for the roll-up.

    Costing the same file as one part is a legitimate thing to do and the
    estimate is still saved and still quotable from History. It is simply never
    what an assembly's Cost and Quote sections show, because for an assembly it
    is the wrong answer rather than an approximate one.
    """
    code = _router_code()

    chooser = code[code.index("function authoritativeEstimate") :]
    chooser = chooser[: chooser.index("function estimateGuidance")]

    # The assembly branch returns before the part branch can be reached.
    assembly_branch = chooser[chooser.index("if (isAssemblyUpload())") :]
    assembly_branch = assembly_branch[: assembly_branch.index("if (hasCurrentResult()")]
    assert "return null;" in assembly_branch, "no roll-up means no estimate, not a part one"
    assert "state.result" not in assembly_branch


def test_a_stale_roll_up_is_refused_rather_than_replaced():
    """Changing a classification invalidates the estimate reference.

    `markRollUpStale` already discarded the roll-up; absent and stale then read
    identically, and the screens quietly showed something else. Staleness is now
    a state with its own sentence.
    """
    assembly_js = (STATIC_PATH / "assembly.js").read_text(encoding="utf-8")
    stale = assembly_js[assembly_js.index("function markRollUpStale") :]
    stale = stale[: stale.index("\n}")]
    assert "state.assemblyCost = null" in stale
    assert "state.assemblyCostStale = true" in stale
    assert "refreshFlowChrome" in stale, "Cost and Quote must hear about it at once"

    # Costing the components again clears it, and so does a new upload.
    assert "state.assemblyCostStale = false" in assembly_js
    reset = assembly_js[assembly_js.index("function resetAssemblyPanel") :]
    assert "state.assemblyCostStale = false" in reset[: reset.index("\n}")]

    code = _router_code()
    assert "state.assemblyCostStale" in code
    assert "not shown here and not quoted" in code


def test_a_single_part_still_quotes_its_part_estimate():
    """Nothing about the single-part path changes."""
    code = _router_code()
    chooser = code[code.index("function authoritativeEstimate") :]
    chooser = chooser[: chooser.index("function estimateGuidance")]

    part = chooser[chooser.index("if (hasCurrentResult()") :]
    assert "state.result.part_cost.estimate_id" in part
    assert 'shown("r-unit")' in part and 'shown("r-total")' in part
    assert 'kind: "part"' in part

    # And the Cost banner is not shown at all for one.
    banner = code[code.index("function renderCostAuthority") :]
    assert "if (!isAssemblyUpload())" in banner[: banner.index("const target")]


def test_the_router_still_works_nothing_out():
    """It arranges. The moment it can compute a total, two can disagree."""
    code = _router_code()

    for arithmetic in ("* quantity", "/ 100 *", "machine_rate_per_hour", "cost_per_kg"):
        assert arithmetic not in code
    # Every figure it prints is either a formatted backend field or a string
    # another file already rendered.
    assert "money(rollUp.total_cost" in code
    assert "money(rollUp.production_total_cost" in code
    assert "rollUp.total_cost *" not in code
    assert "rollUp.total_cost +" not in code


def _assembly_code():
    """`assembly.js` with its prose removed, for the same reason as above."""
    source = (STATIC_PATH / "assembly.js").read_text(encoding="utf-8")
    return "\n".join(
        line
        for line in source.split("\n")
        if not line.strip().startswith(("//", "*", "/*"))
    )


def test_decomposing_an_assembly_asks_for_its_component_plans():
    """The dependency between two buttons became one workflow.

    Costing an assembly whose plans had never been generated produced a
    complete-looking total of material and tooling only, and nothing on the
    screen connected the two clicks.
    """
    code = _assembly_code()

    decompose = code[code.index('$("decompose").addEventListener') :]
    decompose = decompose[: decompose.index("\nfunction renderAssembly")]
    assert "generateComponentPlans({ automatic: true })" in decompose

    # It is the same endpoint the button always used — no second pipeline.
    assert code.count('api("/cad/assembly/plans"') == 1
    assert "function generateComponentPlans" in code


def test_a_reclassification_keeps_the_plans_honest_without_losing_committed_times():
    """Entitlement changes; committed work does not evaporate.

    A rebuild returns freshly estimated times with nothing accepted, so adopting
    one wholesale would silently discard every time the engineer committed.
    """
    code = _assembly_code()

    assert "function reconcileComponentPlans" in code
    assert "reconcileComponentPlans()" in code
    assert "function mergeComponentPlans" in code

    merge = code[code.index("function mergeComponentPlans") :]
    merge = merge[: merge.index("\nasync function reconcileComponentPlans")]
    assert "ours && ours.plan && entry.plan ? ours : entry" in merge


def test_only_manufactured_measurable_components_are_planned_automatically():
    """The browser does not send an unplannable assembly away to find out."""
    code = _assembly_code()

    plannable = code[code.index("function plannableComponents") :]
    plannable = plannable[: plannable.index("function componentsWithoutPlans")]
    assert 'effectiveClassification(profile) !== "manufactured"' in plannable
    assert 'definition.geometry_status === "measured"' in plannable
    assert "profile.is_assembly" in plannable


def test_automatic_generation_never_commits_a_time():
    """The status line says what a generated plan is, every time it is written."""
    code = _assembly_code()

    generate = code[code.index("async function generateComponentPlans") :]
    generate = generate[: generate.index("function mergeComponentPlans")]
    assert "nothing is committed until you accept" in generate
    # And it must not set any of the fields that commit one.
    for committing in (
        "system_time_accepted = true",
        "system_setup_time_accepted = true",
        "user_cycle_time =",
        "user_setup_time =",
    ):
        assert committing not in generate


def test_editing_a_components_plan_goes_to_the_section_that_shows_it():
    """The operations table lives in Manufacturing, so the click must go there.

    Retargeting the table while the Assembly section was showing left it correct
    and invisible: scrollIntoView on an element inside a hidden pane scrolls
    nowhere, so the button looked inert and the user had to find the tab.
    """
    code = _assembly_code()
    edit = code[
        code.index("function editComponentPlan") : code.index("function plannableComponents")
    ]

    # It still retargets the one operations table rather than building a second.
    assert "state.plan = entry.plan" in edit
    assert "state.planComponentId = definitionId" in edit
    assert "renderPlan()" in edit
    # And then goes where that table is shown.
    assert 'setFlow("manufacturing")' in edit
    # Guarded forward, because estimateflow.js is loaded after this file.
    assert 'typeof setFlow === "function"' in edit
    assert "scrollIntoView" in edit, "the pre-router behaviour is the fallback"

    # Only the explicit button navigates. Clicking a component's name opens its
    # detail beside the tree and isolates it in the viewer, which is a different
    # intention and stays where the user is.
    picker = code[code.index('document.querySelectorAll(".plan-pick")') :]
    picker = picker[: picker.index("function ")]
    assert "renderComponentPlanDetail" in picker
    assert "editComponentPlan" not in picker


def test_the_partial_assembly_state_is_named_wherever_the_total_is_shown():
    """A figure with the manufacturing missing must not read as finished."""
    assembly_code = _assembly_code()
    router = _router_code()

    # The roll-up panel renders the server's own completeness note, in the
    # warning style whenever the roll-up is not complete, rather than composing
    # a second sentence that could drift from the counts behind it.
    assert 'summary.is_complete ? "hint" : "assembly-warning"' in assembly_code
    assert "summary.completeness_note" in assembly_code
    # And the Cost section, which shows the headline, carries it too.
    banner = router[router.index("function renderCostAuthority") :]
    banner = banner[: banner.index("function whatTheResultPanelIs")]
    assert "target.incomplete" in banner
    assert "escapeHtml(target.note)" in banner, "the server's sentence, not a second one"
    assert 'class="warning-inline"' in banner

    # And that sentence is authored in exactly one place.
    roll_up = (
        Path(__file__).resolve().parents[1]
        / "src" / "cad_costing_agent" / "assembly_costing.py"
    ).read_text(encoding="utf-8")
    assert roll_up.count("Partial assembly estimate — manufacturing plans incomplete") == 1


def test_the_roll_up_total_says_which_basis_it_is_on():
    """"Assembly total" beside a production quantity invited misreading."""
    assembly_js = (STATIC_PATH / "assembly.js").read_text(encoding="utf-8")

    assert "Assembly total (one assembly)" in assembly_js
    # And the production figure is still reported separately, once.
    assert "production_total_cost" in assembly_js


def test_no_two_classic_scripts_declare_the_same_global_function():
    """Every classic script shares one global scope, so a later declaration of
    the same name silently replaces the earlier one. That is how feedback.js's
    renderOperations replaced the rate-set editor's, and opening the editor
    threw before its processes table was drawn."""
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="([a-z]+\.js)"></script>', markup)
    assert "setup.js" in scripts and "feedback.js" in scripts
    declared: dict[str, str] = {}
    duplicates = []
    for script in scripts:
        source = (STATIC_PATH / script).read_text(encoding="utf-8")
        for name in re.findall(r"^(?:async )?function ([A-Za-z_$][\w$]*)", source, re.MULTILINE):
            if name in declared:
                duplicates.append(f"{name}: {declared[name]} and {script}")
            declared[name] = script
    assert duplicates == []


def test_route_stages_are_rows_of_the_plan_but_never_operations():
    """A decided route's cutting, welding or fettling stage is shown in the
    operations table in route order, but as a stage row: it carries no
    `plan-row` class (so no handler treats it as an operation), it is built from
    the classification's stages and never pushed into `state.plan.operations`."""
    source = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")
    body = source.split("function routeStageRows()", 1)[1].split("\nfunction ", 1)[0]
    assert "route-stage-row" in body
    assert "plan-row" not in body
    assert "operations.push" not in body
    assert "classification.route.stages" in body
    render = source.split("function renderPlan()", 1)[1].split("\nfunction ", 1)[0]
    assert "stageRows.before" in render and "stageRows.after" in render


def test_the_browsers_timeable_stages_mirror_the_backends():
    """plan.js decides which stages offer time pricing; the server decides which
    it accepts. If the two lists drift, the UI offers a choice the server refuses."""
    from cad_costing_agent.stage_costing import TIMEABLE_STAGES

    source = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")
    declared = re.search(r"const TIMED_STAGE_PROCESSES = (\{[^}]*\});", source).group(1)
    mirrored = dict(re.findall(r"(\w+): \"(\w+)\"", declared))
    assert mirrored == TIMEABLE_STAGES


def test_the_browsers_stages_timed_inside_the_welds_mirror_the_backends():
    """plan.js words fit-up and weld finishing as part of each weld's time, with no
    + Add weld of their own. Its keys must be exactly the stages the server times
    only together with welding."""
    from cad_costing_agent.stage_costing import TIMED_ONLY_WITH

    source = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")
    declared = re.search(r"const TIMED_IN_WELDS = (\{[^}]*\});", source).group(1)
    assert set(re.findall(r"(\w+): \"", declared)) == set(TIMED_ONLY_WITH) == {"fit_up", "weld_finishing"}


def test_a_weld_lines_time_resolves_like_the_backends():
    """estimatedFor mirrors PlannedOperation.estimated_for: the suggested process,
    else the one a weld's time was estimated for — and nothing else."""
    source = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")
    body = source.split("function estimatedFor(operation)", 1)[1].split("\nfunction ", 1)[0]
    assert "system_suggested_operation || operation.system_time_operation_type" in body
    applies = source.split("function systemTimeApplies(operation)", 1)[1].split("\nfunction ", 1)[0]
    assert "estimatedFor(operation)" in applies
    # Describing a weld again drops the earlier acceptance, as with_weld does.
    weld = source.split("async function estimateWeld(operation)", 1)[1].split("\nfunction ", 1)[0]
    assert "operation.system_time_accepted = false" in weld
    assert 'operation.system_time_operation_type = "welding"' in weld


def test_ui_contains_no_costing_arithmetic():
    """Costing stays in the backend; the page only formats what it is given."""
    app_js = (STATIC_PATH / "app.js").read_text(encoding="utf-8")

    for formula_fragment in [
        "machine_rate_per_hour *",
        "cost_per_kg *",
        "/ 100 *",
        "* quantity",
    ]:
        assert formula_fragment not in app_js


# --- rate set loading (what the UI needs to populate its selects) -----------


def test_rate_list_gives_the_ui_families_versions_currency_and_flag():
    rows = client.get("/rates").json()

    assert rows
    for row in rows:
        assert {"rate_set_id", "name", "version", "currency", "is_illustrative"} <= row.keys()

    families = {row["rate_set_id"] for row in rows}
    assert RATE_SET in families
    assert {row["version"] for row in rows if row["rate_set_id"] == RATE_SET} >= {"1.0.0", "2.0.0"}


def test_rate_set_detail_gives_materials_and_process_types_for_the_dropdowns():
    body = client.get(f"/rates/{RATE_SET}", params={"version": "1.0.0"}).json()

    assert [m["name"] for m in body["materials"].values()]
    assert "milling" in body["operations"]
    assert body["currency"] == "INR"


def test_latest_and_pinned_version_selection_both_work():
    latest = client.get(f"/rates/{RATE_SET}").json()
    pinned = client.get(f"/rates/{RATE_SET}", params={"version": "1.0.0"}).json()

    assert latest["version"] == "2.0.0"
    assert pinned["version"] == "1.0.0"
    assert latest["materials"]["steel"]["cost_per_kg"] != pinned["materials"]["steel"]["cost_per_kg"]


# --- geometry preview on upload --------------------------------------------


def test_upload_returns_geometry_for_the_preview_panel():
    response = client.post(
        "/cad/geometry", files={"file": ("cube.stl", stl_bytes(), "application/octet-stream")}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["volume"] == pytest.approx(6000.0)
    assert {"surface_area", "bounding_box", "detected_unit"} <= body.keys()


def test_geometry_preview_rejects_an_unreadable_file_with_a_message():
    response = client.post(
        "/cad/geometry", files={"file": ("cube.stl", b"not a mesh", "application/octet-stream")}
    )

    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)


# --- calculate: the exact request the UI builds -----------------------------


def test_calculate_with_the_uis_minimal_form_succeeds():
    response = calculate(base_form())

    assert response.status_code == 200
    part_cost = response.json()["part_cost"]
    assert part_cost["currency"] == "INR"
    assert part_cost["total_part_cost"] > 0
    assert part_cost["per_unit_cost"] > 0
    assert part_cost["estimate_id"]


def test_total_and_per_unit_are_two_decimal_places_for_display():
    body = calculate(base_form()).json()["part_cost"]

    for amount in (body["total_part_cost"], body["per_unit_cost"]):
        assert round(amount, 2) == amount


def test_per_unit_is_the_total_divided_by_quantity():
    body = calculate(base_form(quantity="10")).json()["part_cost"]

    assert body["per_unit_cost"] == pytest.approx(body["total_part_cost"] / 10, abs=0.01)


def test_omitting_blank_additional_costs_lets_the_rate_set_supply_them():
    """The UI omits blank fields; sending 0 would override the rate set."""
    body = calculate(base_form()).json()["part_cost"]
    expected = client.get(f"/rates/{RATE_SET}").json()["additional_costs"]

    assert body["additional_costs"]["overhead_rate_percent"] == expected["overhead_rate_percent"]
    assert body["additional_costs"]["scrap_rate_percent"] == expected["scrap_rate_percent"]


def test_an_entered_additional_cost_overrides_the_rate_set():
    body = calculate(base_form(overhead_rate_percent="5")).json()["part_cost"]

    assert body["additional_costs"]["overhead_rate_percent"] == 5.0


def test_calculate_with_a_pinned_version_uses_that_version():
    body = calculate(base_form(rate_set_version="1.0.0")).json()["part_cost"]

    assert body["rate_set"]["version"] == "1.0.0"


def test_calculate_without_a_pinned_version_uses_the_latest():
    body = calculate(base_form()).json()["part_cost"]

    assert body["rate_set"]["version"] == "2.0.0"


def test_pinned_and_latest_versions_give_different_totals():
    pinned = calculate(base_form(rate_set_version="1.0.0")).json()["part_cost"]
    latest = calculate(base_form()).json()["part_cost"]

    assert pinned["total_part_cost"] != latest["total_part_cost"]


def test_calculate_with_operation_requirements_as_the_ui_sends_them():
    requirements = [
        {
            "operation_id": "OP1",
            "operation_name": "Operation 1",
            "process_type": "milling",
            "setup_time_minutes": 20.0,
            "cycle_time_minutes": 2.0,
            "quantity": 10,  # the UI always derives this from the part quantity
        }
    ]

    body = calculate(
        base_form(operation_requirements=json.dumps(requirements))
    ).json()["part_cost"]

    assert len(body["operations"]) == 1
    assert body["total_operation_cost"] > 0
    # Rates came from the rate set, not from the browser.
    assert body["operations"][0]["machine_rate_per_hour"] > 0


def test_the_ui_derives_operation_quantity_so_a_mismatch_cannot_happen():
    """If the UI ever sent a mismatched quantity the API would reject it."""
    requirements = [
        {
            "operation_id": "OP1",
            "operation_name": "Operation 1",
            "process_type": "milling",
            "setup_time_minutes": 20.0,
            "cycle_time_minutes": 2.0,
            "quantity": 3,
        }
    ]

    response = calculate(base_form(quantity="10", operation_requirements=json.dumps(requirements)))

    assert response.status_code == 400
    assert "must match part quantity" in response.json()["detail"]


def test_a_step_upload_works_through_the_same_form(step_box_bytes):
    response = calculate(base_form(), filename="box.step", data=step_box_bytes(10.0, 20.0, 30.0))

    assert response.status_code == 200
    assert response.json()["geometry"]["source_format"] == "step"


def test_step_with_a_non_mm_unit_is_rejected_which_is_why_the_ui_locks_it(step_box_bytes):
    response = calculate(
        base_form(source_unit="inch"), filename="box.step", data=step_box_bytes()
    )

    assert response.status_code == 400
    assert "conflicts with the unit detected" in response.json()["detail"]


# --- errors the UI has to surface -------------------------------------------


def test_unknown_material_returns_a_readable_string():
    """With a rate set selected, the message names that rate set's materials.

    It used to name the built-in catalog's instead, because CAD mass resolved
    density there regardless of the selected rate set. The catalog's contents
    are irrelevant — and misleading — when a profile is what is being costed
    against.
    """
    response = calculate(base_form(material="Unobtainium"))

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "Unobtainium" in detail
    assert RATE_SET in detail
    assert "Steel" in detail  # what this rate set does price


def test_invalid_quantity_returns_a_readable_message():
    """The multipart endpoint surfaces validation as a 400 string, not a 422 list."""
    response = calculate(base_form(quantity="0"))

    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)


def test_json_endpoints_return_the_list_shaped_errors_the_ui_also_formats():
    """`readableError` in app.js handles both shapes; this is the list one."""
    response = client.post("/rates", json={"rate_set_id": "x", "name": "X", "version": "bad"})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert {"loc", "msg"} <= detail[0].keys()


# --- history and comparison -------------------------------------------------


def test_a_calculated_estimate_appears_in_history_with_what_the_ui_shows():
    estimate_id = calculate(base_form()).json()["estimate_id"]

    page = client.get("/estimates", params={"limit": 50}).json()
    row = next(r for r in page["items"] if r["estimate_id"] == estimate_id)

    assert row["part_id"] == "P-001"
    assert row["quantity"] == 10  # the history table's Qty column
    assert row["currency"] == "INR"
    assert row["total_part_cost"] > 0
    assert row["rate_set_id"] == RATE_SET
    assert row["rate_set_version"]


def per_unit_from_snapshot(snapshot: dict):
    """Mirror of the lookup app.js does — the snapshot shape depends on source."""
    if "per_unit_cost" in snapshot:
        return snapshot["per_unit_cost"]
    return snapshot.get("part_cost", {}).get("per_unit_cost")


def test_history_detail_supplies_per_unit_for_a_cad_estimate():
    """/cost/cad-part nests the part cost, so per-unit is one level down."""
    estimate_id = calculate(base_form()).json()["estimate_id"]

    stored = client.get(f"/estimates/{estimate_id}").json()

    assert stored["currency"] == "INR"
    assert stored["result_snapshot"]["part_cost"]["per_unit_cost"] > 0
    assert per_unit_from_snapshot(stored["result_snapshot"]) > 0


def test_history_detail_supplies_per_unit_for_a_json_part_estimate():
    """/cost/part stores the part cost directly, so per-unit is top level."""
    estimate_id = client.post(
        "/cost/part",
        json={
            "part": {
                "part_id": "P-JSON",
                "part_name": "Bracket",
                "material": "Steel",
                "mass_kg": 2.0,
                "quantity": 4,
            },
            "rate_set_id": RATE_SET,
        },
    ).json()["estimate_id"]

    stored = client.get(f"/estimates/{estimate_id}").json()

    assert stored["result_snapshot"]["per_unit_cost"] > 0
    assert per_unit_from_snapshot(stored["result_snapshot"]) > 0


def test_two_estimates_can_be_compared_from_history():
    first = calculate(base_form(rate_set_version="1.0.0")).json()["estimate_id"]
    second = calculate(base_form(rate_set_version="2.0.0")).json()["estimate_id"]

    body = client.get(
        "/estimates/compare", params={"baseline": first, "comparison": second}
    ).json()

    assert body["baseline"]["currency"] == body["comparison"]["currency"] == "INR"
    assert body["total_part_cost"]["delta"] != 0
    assert body["newer_estimate_id"] == second


def test_history_is_empty_on_a_fresh_database():
    assert client.get("/estimates").json()["total"] == 0


def test_the_browser_must_revalidate_every_ui_asset():
    """A page must never run a version of the UI the server has replaced.

    There is no build step, so no asset filename carries a content hash: every
    release is served as `app.js` and `estimateflow.js` at the same URLs. With
    only an etag and a last-modified date, a browser is free to reuse its stored
    copy without asking — and Chrome did, running a 13,403-byte `estimateflow.js`
    against a server serving 16,178 bytes. The page quoted the wrong estimate,
    and every symptom of it looked like an application bug.

    `no-cache` requires revalidation, not re-download: the etag still turns an
    unchanged file into an empty 304.
    """
    for asset in ("estimateflow.js", "app.js", "assembly.js", "styles.css", "index.html"):
        response = client.get(f"/ui/{asset}")
        assert response.status_code == 200, asset
        assert response.headers.get("cache-control") == "no-cache", asset
        assert response.headers.get("etag"), f"{asset} must still revalidate cheaply"

    # And the conditional request that etag enables still short-circuits.
    etag = client.get("/ui/estimateflow.js").headers["etag"]
    conditional = client.get("/ui/estimateflow.js", headers={"if-none-match": etag})
    assert conditional.status_code == 304
    assert conditional.headers.get("cache-control") == "no-cache"
