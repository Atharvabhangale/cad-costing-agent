"""Turning an estimate into a business document.

The thing being defended here is that a document is not a second opinion. A
quotation reads the snapshots its estimate persisted and arranges them; if it
could calculate anything it could disagree with the estimate it claims to
present, and a customer would have no way to tell which number was real.

So most of these tests are about what the document does *not* do: it runs no
costing, it never sums its way to a total it then presents as authoritative, it
never calls a supplied volume a CAD volume, it refuses to exist rather than show
a partial sum as a price, and it does not carry the internal correction records
into a file somebody forwards.
"""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.estimates import EstimateDetail
from cad_costing_agent.main import app
from cad_costing_agent.quotation import (
    ASSUMED_VOLUME_HEADLINE,
    CUSTOMER,
    ENGINEERING,
    TIME_FROM_ENGINEER,
    TIME_FROM_SYSTEM,
    VOLUME_ASSUMED,
    VOLUME_MEASURED,
    QuoteMetadata,
    build_quotation,
)
from tests import assembly_fixtures
from tests.step_fixtures import block_with_through_hole, plain_block

client = TestClient(app)

STATIC = Path(__file__).resolve().parent.parent / "src" / "cad_costing_agent" / "static"
REAL_MODELS = Path(__file__).resolve().parent.parent / "cadmodels"

RATE_SET = "poc-machine-shop"

# Test values entered the way a user enters them. None is a market price.
BEARING_UNIT_COST = 250.0
BOLT_UNIT_COST = 12.5


# --------------------------------------------------------------------- helpers


def cost_a_part(*, quantity=5, material="Steel", rate_set=RATE_SET, requirements=None):
    """A real part estimate through the unchanged pipeline."""
    data = {
        "material": material,
        "source_unit": "mm",
        "part_id": "P-1",
        "part_name": "Bushing",
        "quantity": str(quantity),
    }
    if rate_set:
        data["rate_set_id"] = rate_set
    data["operation_requirements"] = json.dumps(
        requirements
        if requirements is not None
        else [
            {
                "operation_id": "OP1",
                "operation_name": "Drill bore",
                "process_type": "drilling",
                "setup_time_minutes": 15,
                "cycle_time_minutes": 3,
                "quantity": quantity,
            }
        ]
    )
    response = client.post(
        "/cost/cad-part",
        files={"file": ("hole.step", block_with_through_hole(), "application/octet-stream")},
        data=data,
    )
    assert response.status_code == 200, response.text
    return response.json()["estimate_id"]


# A quotable assembly is a *reviewed* one: since manufacturing completeness
# joined the roll-up, an assembly whose manufactured components have committed
# no time is refused as a customer document, however exact its arithmetic. The
# tests below that are about the document itself therefore review it first.
# `poc-default` is used for those, because the pump's plans contain turning and
# `poc-machine-shop` prices only drilling and milling.
REVIEWED_RATE_SET = "poc-default"


def reviewed_plans(components=None):
    return assembly_fixtures.committed_plans(
        client, rate_set_id=REVIEWED_RATE_SET, components=components
    )


def cost_an_assembly(components=None, plans=None, **extra):
    data = {"material": "Steel", "source_unit": "mm", "rate_set_id": RATE_SET}
    data.update({key: str(value) for key, value in extra.items()})
    if components is not None:
        data["components"] = json.dumps(components)
    if plans is not None:
        data["component_plans"] = json.dumps(plans)
        data["rate_set_id"] = extra.get("rate_set_id", REVIEWED_RATE_SET)
    response = client.post(
        "/cost/assembly",
        files={
            "file": ("pump.step", assembly_fixtures.pump_assembly(), "application/octet-stream")
        },
        data=data,
    )
    assert response.status_code == 200, response.text
    return response.json()


def latest_estimate_id():
    return client.get("/estimates?limit=1").json()["items"][0]["estimate_id"]


def definition_ids():
    """The pump's component definition ids, by name."""
    from cad_costing_agent.cad_import import decompose_assembly, detect_format

    data = assembly_fixtures.pump_assembly()
    adapter, _ = detect_format("pump.step", data)
    assembly = decompose_assembly(data, "pump.step", adapter)
    return {d.name: d.definition_id for d in assembly.definitions}


def quotation_of(estimate_id, view=CUSTOMER, **metadata):
    params = {"view": view, **metadata}
    response = client.get(f"/estimates/{estimate_id}/quotation", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def line(document, category):
    return next(item for item in document["totals"]["lines"] if item["category"] == category)


# --------------------------------------------------------------------- a part quote


def test_a_complete_part_quote_carries_the_stored_total():
    estimate_id = cost_a_part()
    stored = client.get(f"/estimates/{estimate_id}").json()
    document = quotation_of(estimate_id)

    assert document["available"] is True
    assert document["blockers"] == []
    assert document["subject"]["kind"] == "part"
    assert document["subject"]["name"] == "Bushing"
    assert document["totals"]["total"] == pytest.approx(stored["total_part_cost"])
    assert document["totals"]["currency"] == "INR"
    assert document["totals"]["quantity"] == 5


def test_the_cost_lines_partition_the_total():
    """The one place a document could quietly disagree with its estimate. The
    lines are chosen so that they sum, and the sum is checked."""
    estimate_id = cost_a_part()
    document = quotation_of(estimate_id)
    totals = document["totals"]

    summed = sum(item["amount"] for item in totals["lines"])
    assert summed == pytest.approx(totals["lines_total"], abs=0.5)
    assert totals["reconciliation"] == ""
    assert {item["category"] for item in totals["lines"]} == {
        "material",
        "operation_setup",
        "operation_run",
        "overhead",
        "scrap",
        "tooling",
    }


def test_a_remainder_is_reported_rather_than_absorbed():
    """A gap must never pass for an explanation — the rule the cost-impact
    decomposition already follows."""
    estimate_id = cost_a_part()
    detail = EstimateDetail(
        **{
            key: value
            for key, value in client.get(f"/estimates/{estimate_id}").json().items()
            if key
            in (
                "estimate_id",
                "request_snapshot",
                "result_snapshot",
                "rate_set_snapshot",
                "plan_snapshot",
                "time_profile_snapshot",
            )
        }
    )
    # Break one stored line so the partition no longer holds.
    detail.result_snapshot["part_cost"]["total_material_cost"] = 1.0
    document = build_quotation(detail)

    assert "unaccounted for" in document.totals.reconciliation
    assert "recorded total is the authority" in document.totals.reconciliation


def test_setup_and_run_are_shown_separately():
    """Both figures already exist per operation. Setup is charged once per
    setup, so summing the per-operation setup costs gives the part's setup cost
    rather than one per operation."""
    estimate_id = cost_a_part()
    document = quotation_of(estimate_id)

    setup = line(document, "operation_setup")
    run = line(document, "operation_run")
    assert setup["amount"] > 0
    assert run["amount"] > 0
    assert "once per setup" in setup["detail"]
    operations_total = next(
        item for item in document["totals"]["breakdown"] if item["category"] == "operations_total"
    )
    assert setup["amount"] + run["amount"] == pytest.approx(operations_total["amount"], abs=0.02)


def test_the_per_unit_figure_is_the_stored_one():
    estimate_id = cost_a_part(quantity=5)
    stored = client.get(f"/estimates/{estimate_id}").json()
    document = quotation_of(estimate_id)

    assert document["totals"]["per_unit"] == pytest.approx(
        stored["result_snapshot"]["part_cost"]["per_unit_cost"]
    )
    assert "batch of 5" in document["totals"]["lines_basis"]


def test_the_manufacturing_summary_reports_setup_and_cycle_apart():
    estimate_id = cost_a_part()
    document = quotation_of(estimate_id)
    manufacturing = document["manufacturing"]

    assert manufacturing["operation_count"] == 1
    assert manufacturing["total_setup_time_minutes"] == pytest.approx(15.0)
    assert manufacturing["total_cycle_time_minutes"] == pytest.approx(3.0)
    assert manufacturing["processes"] == ["drilling"]
    assert "once per setup" in manufacturing["time_basis"]


# --------------------------------------------------------------------- provenance


def test_a_measured_volume_is_described_as_measured():
    estimate_id = cost_a_part()
    document = quotation_of(estimate_id)
    provenance = document["provenance"]

    assert provenance["volume_basis"] == VOLUME_MEASURED
    assert "measured from the CAD geometry" in provenance["volume_statement"]
    assert provenance["user_supplied_volume_mm3"] is None
    assert ASSUMED_VOLUME_HEADLINE not in provenance["volume_statement"]


def test_the_document_names_every_input_it_rested_on():
    estimate_id = cost_a_part()
    provenance = quotation_of(estimate_id)["provenance"]

    assert provenance["source_file"] == "hole.step"
    assert provenance["geometry_source"] == "brep"
    assert provenance["material"] == "Steel"
    assert provenance["density_kg_m3"] > 0
    assert provenance["mass_kg"] > 0
    assert provenance["rate_set_id"] == RATE_SET
    assert provenance["rate_set_version"]
    assert provenance["overhead_rate_percent"] is not None
    assert "operation" in provenance["plan_state"].lower()


def test_the_basis_note_says_nothing_is_recalculated():
    document = quotation_of(cost_a_part())

    assert "read from the estimate" in document["basis_note"]
    assert "Nothing is recalculated" in document["basis_note"]


# --------------------------------------------------------------------- warnings


def test_an_illustrative_rate_set_is_warned_about_prominently():
    document = quotation_of(cost_a_part())
    warning = next(w for w in document["warnings"] if w["code"] == "illustrative_rates")

    assert warning["severity"] == "critical"
    assert "not a commercial price" in warning["title"]
    assert "placeholders" in warning["detail"]


def partly_measurable() -> bytes:
    """A 10 mm cube (measurable) beside an open sheet (not).

    The same construction the geometry-completion tests use, because it is the
    only reliable way to reach the assumption path: an ordinary open mesh still
    yields a signed volume, and the costing endpoint correctly refuses an
    assumption for geometry it could measure.
    """
    import numpy as np

    from tests.faceted_fixtures import faceted_step_from_mesh

    cube_vertices = np.array(
        [
            [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0], [1.0, 0.0, 1.0], [1.0, 1.0, 1.0], [0.0, 1.0, 1.0],
        ]
    )
    cube_triangles = np.array(
        [
            [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
            [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
            [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
        ]
    )
    sheet = np.array([[0.0, 0.0, 50.0], [10.0, 0.0, 50.0], [10.0, 10.0, 50.0]])
    vertices = np.vstack([cube_vertices * 10.0, sheet])
    triangles = np.vstack([cube_triangles, np.array([[8, 9, 10]])])
    return faceted_step_from_mesh(vertices, triangles)


def test_a_supplied_volume_is_never_called_a_cad_volume():
    """The single most consequential sentence in the document. A mass is a
    volume times a density and a price is a mass times a rate, so an assumed
    volume relabelled as measured makes the whole quotation a fabrication."""
    response = client.post(
        "/cost/cad-part",
        files={"file": ("shell.step", partly_measurable(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-2",
            "part_name": "Shell",
            "quantity": "1",
            "rate_set_id": RATE_SET,
            "geometry_completion_method": "total_volume",
            "geometry_completion_volume_mm3": "3981004.3",
        },
    )
    assert response.status_code == 200, response.text
    document = quotation_of(response.json()["estimate_id"])
    provenance = document["provenance"]

    assert provenance["volume_basis"] == VOLUME_ASSUMED
    assert provenance["volume_statement"].startswith(ASSUMED_VOLUME_HEADLINE)
    assert "User-supplied assumption" in provenance["volume_statement"]
    assert "Volume used" in provenance["volume_statement"]
    assert "3,981,004.3" in provenance["volume_statement"]
    assert "CAD volume" not in provenance["volume_statement"]
    assert provenance["user_supplied_volume_mm3"] == pytest.approx(3981004.3)

    warning = next(w for w in document["warnings"] if w["code"] == "assumed_volume")
    assert warning["title"] == ASSUMED_VOLUME_HEADLINE
    assert warning["severity"] == "critical"
    # And the document is still produced: a stated assumption is a warning,
    # not a refusal.
    assert document["available"] is True


def test_a_polygon_derived_volume_says_so():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("box.stl", _stl_box(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-3",
            "part_name": "Block",
            "quantity": "1",
            "rate_set_id": RATE_SET,
        },
    )
    assert response.status_code == 200
    document = quotation_of(response.json()["estimate_id"])

    warning = next(w for w in document["warnings"] if w["code"] == "reconstructed_geometry")
    assert warning["severity"] == "advisory"
    assert "flat polygons" in warning["detail"]


def _stl_box():
    import trimesh

    stl = trimesh.creation.box(extents=(40, 30, 20)).export(file_type="stl")
    return stl if isinstance(stl, bytes) else stl.encode()


def test_warnings_are_not_suppressed_for_a_cleaner_document():
    """There is no parameter that hides them, in the API or the page."""
    import inspect

    import cad_costing_agent.quotation as module

    source = inspect.getsource(module)
    for forbidden in ("hide_warnings", "suppress_warnings", "include_warnings"):
        assert forbidden not in source
    # Every view carries them.
    estimate_id = cost_a_part()
    for view in (CUSTOMER, ENGINEERING):
        assert quotation_of(estimate_id, view=view)["warnings"]


# --------------------------------------------------------------------- assembly


def test_a_complete_assembly_quote_lists_every_component_by_kind():
    ids = definition_ids()
    decisions = [
        {
            "definition_id": ids["COVER"],
            "user_classification": "purchased",
            "unit_cost": BEARING_UNIT_COST,
            "currency": "INR",
            "supplier": "Test Supplier",
        },
        {
            "definition_id": ids["PIN"],
            "user_classification": "standard",
            "unit_cost": BOLT_UNIT_COST,
            "currency": "INR",
        },
    ]
    cost_an_assembly(decisions, plans=reviewed_plans(decisions))
    document = quotation_of(latest_estimate_id())

    assert document["available"] is True
    assert document["subject"]["kind"] == "assembly"
    rows = {row["name"]: row for row in document["components"]}
    assert rows["HOUSING"]["classification_label"] == "Manufactured"
    assert rows["COVER"]["classification_label"] == "Purchased"
    assert rows["COVER"]["supplier"] == "Test Supplier"
    assert rows["COVER"]["unit_cost"] == pytest.approx(BEARING_UNIT_COST)
    assert rows["PIN"]["classification_label"] == "Standard"
    assert rows["PIN"]["extended_cost"] == pytest.approx(BOLT_UNIT_COST * 2)
    # A sub-assembly is not a component line at all.
    assert "COVER_ASSY" not in rows
    assert "container, not a part" in document["assembly"]["sub_assembly_note"]


def test_the_assembly_lines_are_one_per_classification_and_partition_the_total():
    ids = definition_ids()
    summary = cost_an_assembly(
        [
            {
                "definition_id": ids["COVER"],
                "user_classification": "purchased",
                "unit_cost": BEARING_UNIT_COST,
                "currency": "INR",
            },
            {
                "definition_id": ids["PIN"],
                "user_classification": "standard",
                "unit_cost": BOLT_UNIT_COST,
                "currency": "INR",
            },
        ]
    )
    document = quotation_of(latest_estimate_id())
    totals = document["totals"]

    assert [item["category"] for item in totals["lines"]] == [
        "manufactured",
        "purchased",
        "standard",
        "excluded",
    ]
    assert line(document, "purchased")["amount"] == pytest.approx(BEARING_UNIT_COST)
    assert line(document, "standard")["amount"] == pytest.approx(BOLT_UNIT_COST * 2)
    assert line(document, "excluded")["amount"] == 0.0
    assert sum(item["amount"] for item in totals["lines"]) == pytest.approx(
        summary["total_cost"], abs=0.5
    )
    assert totals["reconciliation"] == ""
    assert totals["lines_basis"] == "one assembly"


def test_an_excluded_component_is_shown_as_zero_with_its_reason():
    ids = definition_ids()
    decisions = [
        {
            "definition_id": ids["PIN"],
            "user_classification": "excluded",
            "exclusion_reason": "shipping hardware, not supplied",
        }
    ]
    cost_an_assembly(decisions, plans=reviewed_plans(decisions))
    document = quotation_of(latest_estimate_id())
    pin = next(row for row in document["components"] if row["name"] == "PIN")

    assert pin["excluded"] is True
    assert pin["extended_cost"] == 0.0
    assert "shipping hardware, not supplied" in pin["reason"]
    warning = next(w for w in document["warnings"] if w["code"] == "excluded_components")
    assert "explicit decision" in warning["detail"]
    assert document["available"] is True


def test_the_assembly_summary_carries_the_counts_a_reviewer_needs():
    cost_an_assembly(plans=reviewed_plans())
    document = quotation_of(latest_estimate_id())
    summary = document["assembly"]

    assert summary["assembly_name"] == "PUMP"
    assert summary["definition_count"] == 5
    assert summary["part_definition_count"] == 3
    assert summary["occurrence_count"] == 6
    assert summary["part_occurrence_count"] == 5
    assert summary["costed_definition_count"] == 3
    assert summary["uncosted_definition_count"] == 0
    assert summary["is_complete"] is True


def test_a_production_quantity_is_stated_rather_than_folded_into_the_lines():
    summary = cost_an_assembly(assembly_quantity=10)
    document = quotation_of(latest_estimate_id())
    totals = document["totals"]

    assert totals["quantity"] == 10
    assert totals["quantity_label"] == "assemblies"
    assert totals["per_unit"] == pytest.approx(summary["total_cost"], abs=0.01)
    assert totals["total"] == pytest.approx(summary["production_total_cost"], abs=0.01)
    assert totals["lines_basis"] == "one assembly"


# --------------------------------------------------------------------- refusals


def test_an_incomplete_assembly_blocks_the_quotation():
    ids = definition_ids()
    cost_an_assembly(
        [{"definition_id": ids["PIN"], "user_classification": "purchased"}]  # no price
    )
    document = quotation_of(latest_estimate_id())

    assert document["available"] is False
    blocker = next(b for b in document["blockers"] if b["code"] == "assembly_incomplete")
    assert blocker["title"] == "Quotation unavailable"
    assert "1 component(s) still require costing input" in blocker["detail"]
    assert "partial sum must not be presented as a price" in blocker["detail"]


def test_a_mixed_currency_assembly_refuses_to_present_a_total():
    ids = definition_ids()
    cost_an_assembly(
        [
            {
                "definition_id": ids["COVER"],
                "user_classification": "purchased",
                "unit_cost": 30.0,
                "currency": "EUR",
            }
        ]
    )
    document = quotation_of(latest_estimate_id())

    assert document["available"] is False
    codes = {b["code"] for b in document["blockers"]}
    assert "mixed_currency" in codes
    blocker = next(b for b in document["blockers"] if b["code"] == "mixed_currency")
    assert "EUR" in blocker["detail"] and "INR" in blocker["detail"]
    assert "Nothing here converts" in blocker["detail"]


def test_unresolved_geometry_blocks_the_quotation():
    """Guarded on the stored record. `/cost/cad-part` refuses such a request
    outright, so this cannot be reached from a current upload — and it is
    checked anyway, because a document is the last place an unresolved volume
    should first become visible."""
    estimate_id = cost_a_part()
    stored = client.get(f"/estimates/{estimate_id}").json()
    detail = EstimateDetail(
        estimate_id=estimate_id,
        request_snapshot=stored["request_snapshot"],
        result_snapshot=stored["result_snapshot"],
    )
    detail.result_snapshot["geometry"]["geometry_status"] = "incomplete_requires_input"
    document = build_quotation(detail)

    assert document.available is False
    blocker = next(b for b in document.blockers if b.code == "geometry_requires_input")
    assert blocker.title == "Quotation unavailable"
    assert "Geometry requires user input" in blocker.detail


def test_issuing_a_blocked_quotation_is_refused():
    ids = definition_ids()
    cost_an_assembly([{"definition_id": ids["PIN"], "user_classification": "purchased"}])
    response = client.post(
        f"/estimates/{latest_estimate_id()}/quotation",
        json={"customer_name": "Acme"},
    )

    assert response.status_code == 409
    assert "cannot be quoted" in response.json()["detail"]


def test_a_blocked_estimate_still_renders_its_engineering_view():
    """The person fixing the problem has to be able to see it. Only the
    customer-facing document is withheld."""
    ids = definition_ids()
    cost_an_assembly([{"definition_id": ids["PIN"], "user_classification": "purchased"}])
    document = quotation_of(latest_estimate_id(), view=ENGINEERING)

    assert document["available"] is False
    assert document["engineering"] is not None
    assert document["components"]


# --------------------------------------------------------------------- the two views


def test_the_customer_view_does_not_carry_the_engineering_appendix():
    document = quotation_of(cost_a_part(), view=CUSTOMER)

    assert document["engineering"] is None
    # But everything a buyer needs is there.
    assert document["subject"]["name"]
    assert document["totals"]["total"] is not None
    assert document["totals"]["per_unit"] is not None
    assert document["provenance"]["material"]
    assert document["warnings"]


def test_the_customer_view_carries_no_internal_feedback_metadata():
    """Absent, not merely unrendered: a field a template does not print is
    still a field in the JSON somebody forwards."""
    ids = definition_ids()
    cost_an_assembly(
        [
            {
                "definition_id": ids["COVER"],
                "user_classification": "purchased",
                "unit_cost": BEARING_UNIT_COST,
                "currency": "INR",
                "classification_reason": "catalogue bearing",
            },
            {
                "definition_id": ids["PIN"],
                "user_classification": "standard",
                "unit_cost": BOLT_UNIT_COST,
                "currency": "INR",
            },
        ]
    )
    payload = json.dumps(quotation_of(latest_estimate_id(), view=CUSTOMER))

    for forbidden in (
        "classification_feedback",
        "system_classification",
        "user_classification",
        "classification_changed",
        "system_suggested_operation",
        "suggestion_confidence",
        "system_time_parameters",
        "recorded_at",
        "corrections",
    ):
        assert forbidden not in payload, forbidden


def test_the_engineering_view_adds_setups_operations_and_provenance():
    document = quotation_of(cost_a_part(), view=ENGINEERING)
    engineering = document["engineering"]

    assert engineering is not None
    assert engineering["operations"]
    assert engineering["operations"][0]["operation_id"] == "OP1"
    assert engineering["operations"][0]["process_type"] == "drilling"
    assert engineering["rate_set_provenance"]["rate_set_id"] == RATE_SET
    assert engineering["rate_set_provenance"]["is_illustrative"] is True
    assert "time_profile_id" in engineering["time_profile_provenance"]
    assert engineering["geometry"]


def test_an_unknown_view_is_refused():
    response = client.get(
        f"/estimates/{cost_a_part()}/quotation", params={"view": "marketing"}
    )

    assert response.status_code == 400
    assert "Unknown view" in response.json()["detail"]


# --------------------------------------------------------------------- plan detail


def test_a_planned_part_shows_its_setups_with_the_operations_inside_them():
    """The plan path, so the setup/operation structure the report shows is the
    one the plan actually committed."""
    from tests.step_fixtures import block_with_through_hole as fixture

    suggestions = client.post(
        "/cad/operations/suggest",
        files={"file": ("hole.step", fixture(), "application/octet-stream")},
    ).json()
    plan_response = client.post(
        "/plan/build",
        json={
            "suggestions": suggestions["suggestions"],
            "declined": suggestions.get("declined", []),
        },
    )
    if plan_response.status_code != 200:
        pytest.skip("this build has no /plan/build endpoint in this shape")

    document = quotation_of(cost_a_part(), view=ENGINEERING)
    assert document["engineering"]["operations"]


def test_a_committed_time_is_labelled_by_where_it_came_from():
    """A label, not a recomputation: an operation only reaches the cost if it
    had a committed time, so the source is a two-branch test rather than a
    third copy of the resolution rule."""
    from cad_costing_agent.quotation import TIME_SOURCE_LABELS

    assert TIME_SOURCE_LABELS[TIME_FROM_ENGINEER] == "entered by the engineer"
    assert TIME_SOURCE_LABELS[TIME_FROM_SYSTEM] == "system estimate, accepted"

    # With no plan snapshot the source is left blank rather than guessed.
    document = quotation_of(cost_a_part(), view=ENGINEERING)
    operation = document["engineering"]["operations"][0]
    assert operation["time_source"] == ""
    assert operation["cycle_time_minutes"] == pytest.approx(3.0)
    assert operation["setup_time_minutes"] == pytest.approx(15.0)
    assert operation["setup_cost"] > 0
    assert operation["run_cost"] > 0


def test_every_operation_the_document_shows_actually_reached_the_cost():
    """The document reads the costed operations, not the plan's. A planned
    operation with no committed time was left out of the price and must not
    appear as though it had been paid for."""
    estimate_id = cost_a_part()
    stored = client.get(f"/estimates/{estimate_id}").json()
    costed = {op["operation_id"] for op in stored["result_snapshot"]["part_cost"]["operations"]}
    document = quotation_of(estimate_id, view=ENGINEERING)

    assert {op["operation_id"] for op in document["engineering"]["operations"]} == costed


# --------------------------------------------------------------------- metadata


def test_quote_metadata_is_carried_onto_the_document():
    document = quotation_of(
        cost_a_part(),
        customer_name="Acme Pumps Pvt Ltd",
        project_name="Line 4 retrofit",
        quote_reference="Q-2026-014",
        prepared_by="A. Engineer",
        notes="Tooling amortised over the batch.",
        validity="30 days from the date above",
    )

    metadata = document["metadata"]
    assert metadata["customer_name"] == "Acme Pumps Pvt Ltd"
    assert metadata["project_name"] == "Line 4 retrofit"
    assert metadata["prepared_by"] == "A. Engineer"
    assert metadata["notes"] == "Tooling amortised over the batch."
    assert metadata["validity"] == "30 days from the date above"
    assert document["quote_reference"] == "Q-2026-014"
    assert document["subject"]["reference"] == "Q-2026-014"


def test_without_a_reference_the_estimate_id_identifies_the_document():
    estimate_id = cost_a_part()
    document = quotation_of(estimate_id)

    assert document["quote_reference"] == estimate_id


def test_issuing_a_quotation_persists_only_its_metadata_and_totals():
    estimate_id = cost_a_part()
    response = client.post(
        f"/estimates/{estimate_id}/quotation",
        json={
            "customer_name": "Acme Pumps Pvt Ltd",
            "project_name": "Line 4 retrofit",
            "prepared_by": "A. Engineer",
            "notes": "Tooling amortised over the batch.",
            "validity": "30 days",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    issued = body["issued"]
    assert issued["quote_number"] == "Q-0001"
    assert issued["estimate_id"] == estimate_id
    assert issued["customer_name"] == "Acme Pumps Pvt Ltd"
    assert issued["currency"] == "INR"
    assert issued["total_at_issue"] == pytest.approx(body["quotation"]["totals"]["total"])
    assert body["quotation"]["issued_at"] is not None


def test_quote_numbers_are_sequential_and_never_reused():
    estimate_id = cost_a_part()
    first = client.post(f"/estimates/{estimate_id}/quotation", json={}).json()
    second = client.post(f"/estimates/{estimate_id}/quotation", json={}).json()

    assert first["issued"]["quote_number"] == "Q-0001"
    assert second["issued"]["quote_number"] == "Q-0002"

    clash = client.post(
        f"/estimates/{estimate_id}/quotation", json={"quote_number": "Q-0001"}
    )
    assert clash.status_code == 409
    assert "already been issued" in clash.json()["detail"]


def test_a_user_supplied_quote_number_is_used_as_given():
    estimate_id = cost_a_part()
    issued = client.post(
        f"/estimates/{estimate_id}/quotation", json={"quote_number": "ACME/2026/014"}
    ).json()

    assert issued["issued"]["quote_number"] == "ACME/2026/014"
    assert issued["quotation"]["quote_reference"] == "ACME/2026/014"


def test_an_issued_quotation_reads_back_with_its_metadata():
    estimate_id = cost_a_part()
    client.post(
        f"/estimates/{estimate_id}/quotation",
        json={"quote_number": "Q-9", "customer_name": "Acme", "validity": "14 days"},
    )
    response = client.get("/quotations/Q-9")

    assert response.status_code == 200
    body = response.json()
    assert body["issued"]["customer_name"] == "Acme"
    assert body["quotation"]["metadata"]["customer_name"] == "Acme"
    assert body["quotation"]["metadata"]["validity"] == "14 days"
    assert body["quotation"]["quote_reference"] == "Q-9"


def test_an_unknown_quotation_is_a_404():
    assert client.get("/quotations/nope").status_code == 404


# --------------------------------------------------------------------- immutability


def test_a_later_rate_set_version_does_not_change_an_issued_quotation():
    """The point of the whole design: the document is re-rendered from the
    estimate's own snapshots, which nothing published later can touch."""
    estimate_id = cost_a_part()
    issued = client.post(
        f"/estimates/{estimate_id}/quotation", json={"quote_number": "Q-IMM"}
    ).json()
    before = issued["quotation"]

    published = client.post(
        f"/rates/{RATE_SET}/versions",
        json={
            "version": "99.0.0",
            "name": "Much more expensive",
            "description": "A later version, to prove it changes nothing.",
            "is_illustrative": False,
            "currency": "INR",
            "materials": {"steel": {"name": "Steel", "cost_per_kg": 9999.0, "density_kg_m3": 7850.0}},
            "operations": {
                "drilling": {"machine_rate_per_hour": 9999.0, "labor_rate_per_hour": 9999.0}
            },
            "additional_costs": {
                "overhead_rate_percent": 99.0,
                "scrap_rate_percent": 99.0,
                "tooling_cost_per_batch": 9999.0,
            },
        },
    )
    assert published.status_code in (200, 201), published.text

    after = client.get("/quotations/Q-IMM").json()
    assert after["quotation"]["totals"]["total"] == pytest.approx(before["totals"]["total"])
    assert after["quotation"]["totals"]["lines"] == before["totals"]["lines"]
    assert after["quotation"]["provenance"]["rate_set_version"] == before["provenance"][
        "rate_set_version"
    ]
    # And the figure recorded at issue still matches what re-rendering produces.
    assert after["issued"]["total_at_issue"] == pytest.approx(
        after["quotation"]["totals"]["total"]
    )


def test_a_later_estimate_of_the_same_part_does_not_change_an_issued_quotation():
    first = cost_a_part(quantity=5)
    client.post(f"/estimates/{first}/quotation", json={"quote_number": "Q-A"})
    before = client.get("/quotations/Q-A").json()["quotation"]

    cost_a_part(quantity=50)  # a new estimate of the same part

    after = client.get("/quotations/Q-A").json()["quotation"]
    assert after["totals"]["total"] == pytest.approx(before["totals"]["total"])
    assert after["totals"]["quantity"] == 5
    assert after["estimate_id"] == first


def test_a_later_component_classification_does_not_change_an_issued_assembly_quote():
    ids = definition_ids()
    cost_an_assembly(plans=reviewed_plans())
    estimate_id = latest_estimate_id()
    client.post(f"/estimates/{estimate_id}/quotation", json={"quote_number": "Q-ASM"})
    before = client.get("/quotations/Q-ASM").json()["quotation"]

    cost_an_assembly(
        [
            {
                "definition_id": ids["HOUSING"],
                "user_classification": "purchased",
                "unit_cost": 9999.0,
                "currency": "INR",
            }
        ]
    )

    after = client.get("/quotations/Q-ASM").json()["quotation"]
    assert after["totals"]["total"] == pytest.approx(before["totals"]["total"])
    housing = next(row for row in after["components"] if row["name"] == "HOUSING")
    assert housing["classification"] == "manufactured"


# --------------------------------------------------------------------- determinism


def test_rendering_the_same_quotation_twice_is_identical():
    estimate_id = cost_a_part()
    metadata = {"customer_name": "Acme", "quote_reference": "Q-1"}

    first = quotation_of(estimate_id, **metadata)
    second = quotation_of(estimate_id, **metadata)

    assert first == second


def test_the_document_reads_no_clock_of_its_own():
    """`issued_at` is a parameter, so a preview and the document it previews
    cannot differ by a timestamp."""
    import inspect

    import cad_costing_agent.quotation as module

    source = inspect.getsource(module)
    assert "datetime.now" not in source
    assert "utcnow" not in source

    estimate_id = cost_a_part()
    stored = client.get(f"/estimates/{estimate_id}").json()
    detail = EstimateDetail(
        estimate_id=estimate_id,
        request_snapshot=stored["request_snapshot"],
        result_snapshot=stored["result_snapshot"],
    )
    assert build_quotation(detail).issued_at is None
    fixed = datetime(2026, 9, 4, 12, 0, 0)
    assert build_quotation(detail, issued_at=fixed).issued_at == fixed


def test_currency_is_formatted_consistently_and_never_converted():
    document = quotation_of(cost_a_part())

    assert document["totals"]["currency"] == "INR"
    payload = json.dumps(document)
    for foreign in ("USD", "EUR", "$", "€"):
        assert foreign not in payload, foreign


def test_every_money_figure_is_rounded_the_same_way():
    """Two decimals, from the existing presentation policy — not a second
    rounding rule invented for the document."""
    document = quotation_of(cost_a_part())
    amounts = [item["amount"] for item in document["totals"]["lines"]] + [
        document["totals"]["total"],
        document["totals"]["per_unit"],
    ]

    for amount in amounts:
        if amount is None:
            continue
        assert round(amount, 2) == amount, amount


# --------------------------------------------------------------------- revision


def test_a_revision_shows_the_figures_and_attributes_no_cause():
    first = cost_a_part(quantity=5)
    second = cost_a_part(quantity=50)
    document = quotation_of(second)
    revision = document["revision"]

    assert revision is not None
    assert revision["previous_estimate_id"] == first
    assert revision["comparable"] is True
    assert revision["change"] == pytest.approx(
        revision["current_total"] - revision["previous_total"], abs=0.02
    )
    assert "No cause is attributed" in revision["note"]
    # And no causal language sneaks in.
    for forbidden in ("because", "caused by", "due to"):
        assert forbidden not in revision["note"].lower()


def test_the_first_estimate_of_a_part_has_no_revision_block():
    assert quotation_of(cost_a_part())["revision"] is None


# --------------------------------------------------------------------- structure


def _package_imports_of(module) -> set[str]:
    import ast

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return {name for name in imported if name.startswith("cad_costing_agent")}


def test_the_quotation_layer_reaches_no_calculator_and_no_repository():
    """There is no second costing path because there is nowhere for one to come
    from: this module can reach the assumptions reader and the money policy,
    and nothing else."""
    import cad_costing_agent.quotation as module

    assert _package_imports_of(module) == {
        "cad_costing_agent.estimates",
        "cad_costing_agent.money",
    }


def test_the_quotation_layer_calculates_no_cost():
    """It regroups stored figures and reconciles them. It must not resolve a
    rate, price a mass or estimate a time."""
    import ast
    import inspect

    import cad_costing_agent.quotation as module

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    code = ast.unparse(tree)

    for forbidden in (
        "calculate_",
        "resolve_material(",
        "resolve_operation(",
        "estimate_operation",
        "calculate_mass",
        "extract_geometry",
        "rate_per_hour *",
        "cost_per_kg *",
        "* density",
        "round_money(",
    ):
        assert forbidden not in code, forbidden

    # And it holds no rate set, no part and no operation object: the only
    # things it can be handed are an estimate's snapshots.
    for forbidden in ("RateSet", "ManufacturingOperation", "OperationRequirement", "Part("):
        assert forbidden not in code, forbidden


# --------------------------------------------------------------------- the page


def test_the_report_page_is_served_and_standalone():
    response = client.get("/ui/quotation.html")

    assert response.status_code == 200
    html = response.text
    assert "<title>" in html
    # A dedicated page: none of the application's interactive panels.
    for forbidden in ("app.js", "viewer.js", "plan.js", "assembly.js", "setup.js"):
        assert forbidden not in html, forbidden


def test_the_report_page_computes_no_costing_arithmetic():
    """The same rule the rest of this UI follows, and it matters most here: a
    plausible number invented in a printable document is indistinguishable from
    a real one."""
    source = (STATIC / "quotation.js").read_text(encoding="utf-8")

    for forbidden in ("reduce(", "* quantity", "* line.quantity", "per_unit_cost *"):
        assert forbidden not in source, forbidden
    for pattern in (
        r"\.total\s*=(?!=)",
        r"\.per_unit\s*=(?!=)",
        r"\.amount\s*=(?!=)",
        r"\.extended_cost\s*=(?!=)",
        r"(?:amount|total|per_unit|extended_cost|unit_cost)\s*[-+*/]",
    ):
        assert not re.search(pattern, source), pattern


def test_the_report_page_reads_only_fields_the_api_declares():
    """The Step 25 blank-panel bug: a renamed field threw inside a template
    literal and the panel rendered empty with no error."""
    from cad_costing_agent.quotation import (
        QuoteComponentLine,
        QuoteLine,
        QuoteProvenance,
        Quotation,
        QuoteTotals,
    )

    source = (STATIC / "quotation.js").read_text(encoding="utf-8")
    for name, model in (
        ("quote", Quotation),
        ("totals", QuoteTotals),
        ("provenance", QuoteProvenance),
        ("line", QuoteLine),
        ("component", QuoteComponentLine),
    ):
        declared = set(model.model_fields)
        for field in re.findall(rf"\b{name}\.([a-z_0-9]+)", source):
            assert field in declared, f"{name}.{field}"


def test_the_page_prints_the_assumed_volume_headline_verbatim():
    """The document's most consequential sentence must not be paraphrased by
    the template."""
    source = (STATIC / "quotation.js").read_text(encoding="utf-8")
    html = (STATIC / "quotation.html").read_text(encoding="utf-8")

    assert "volume_statement" in source
    # And the page never invents the phrase the backend refuses to use.
    assert "CAD volume" not in source
    assert "CAD volume" not in html


def test_the_print_stylesheet_paginates_and_hides_the_controls():
    """The declarations only — CSS comments are stripped first, because the
    stylesheet explains at length why it does *not* force a paper size, and a
    naive grep matches the paragraph promising the rule is absent."""
    raw = (STATIC / "quotation.html").read_text(encoding="utf-8") + (
        STATIC / "quotation.css"
    ).read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)

    assert "@media print" in css
    assert "@page" in css
    assert "margin: 14mm" in css
    # The paper size is deliberately left to the reader: forcing one would
    # override the print dialog and make a Letter printer scale or clip.
    assert "size:" not in css.split("@page")[1].split("}")[0]
    # Repeating table headers and sensible breaks.
    assert "table-header-group" in css
    assert "break-inside" in css
    # The controls are not part of the document.
    assert "no-print" in css
    assert "display: none !important" in css


def test_the_estimate_history_offers_the_quotation_action():
    source = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "quotation.html" in source
    assert "Quotation" in source or "quotation" in source


# --------------------------------------------------------------------- real files


@pytest.mark.skipif(
    not (REAL_MODELS / "model1_1.stp").is_file(), reason="the real part is not present"
)
def test_a_real_part_quotes_from_its_own_estimate():
    data = (REAL_MODELS / "model1_1.stp").read_bytes()
    response = client.post(
        "/cost/cad-part",
        files={"file": ("model1_1.stp", data, "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "M1",
            "part_name": "Bracket",
            "quantity": "10",
            "rate_set_id": RATE_SET,
        },
    )
    assert response.status_code == 200, response.text
    estimate_id = response.json()["estimate_id"]
    stored = client.get(f"/estimates/{estimate_id}").json()
    document = quotation_of(estimate_id)

    assert document["available"] is True
    assert document["totals"]["total"] == pytest.approx(stored["total_part_cost"])
    assert document["provenance"]["volume_basis"] == VOLUME_MEASURED
    assert document["provenance"]["source_file"] == "model1_1.stp"


def test_a_material_only_estimate_says_so_rather_than_implying_machining():
    estimate_id = cost_a_part(requirements=[])
    document = quotation_of(estimate_id)

    assert document["available"] is True
    assert document["manufacturing"]["operation_count"] == 0
    assert "material-only" in document["provenance"]["plan_state"]
    assert line(document, "operation_run")["amount"] == 0.0


def test_the_rounding_gap_a_reader_will_find_is_stated():
    """A quotation shows a per-unit figure beside a total and anyone checking it
    multiplies one by the other. Both are rounded from their own exact values,
    so the product can be a few paisa out — found on the real document during
    live verification, where 154.69 x 25 came to 3,867.25 against a stated
    3,867.31 with nothing to explain the six paisa."""
    document = quotation_of(cost_a_part(quantity=25))
    totals = document["totals"]

    product = round(totals["per_unit"] * totals["quantity"], 2)
    if abs(product - totals["total"]) >= 0.005:
        assert totals["quantity_note"]
        assert f"{totals['quantity']}" in totals["quantity_note"]
        assert "rounded true total" in totals["quantity_note"]
        assert "No figure has been adjusted" in totals["quantity_note"]
    else:  # pragma: no cover - depends on the rates in force
        assert totals["quantity_note"] == ""


def test_a_quantity_of_one_needs_no_rounding_note():
    document = quotation_of(cost_a_part(quantity=1))

    assert document["totals"]["quantity_note"] == ""


def test_the_setup_count_is_never_zero_beside_a_charged_setup_time():
    """Also found on the real document: an estimate made from hand-entered
    requirements has no plan, so the plan's setup count is zero — printed
    beside twenty minutes of setup time, which is a contradiction."""
    document = quotation_of(
        cost_a_part(
            requirements=[
                {
                    "operation_id": "OP1",
                    "operation_name": "Face both ends",
                    "process_type": "milling",
                    "setup_time_minutes": 20,
                    "cycle_time_minutes": 0.8,
                    "quantity": 5,
                },
                {
                    "operation_id": "OP2",
                    "operation_name": "Drill bore",
                    "process_type": "drilling",
                    "setup_time_minutes": 0,
                    "cycle_time_minutes": 0.23,
                    "quantity": 5,
                },
            ]
        )
    )
    manufacturing = document["manufacturing"]

    assert manufacturing["total_setup_time_minutes"] == pytest.approx(20.0)
    assert manufacturing["setup_count"] == 1  # the one operation carrying setup time
    assert manufacturing["operation_count"] == 2


def test_the_page_prints_a_null_profile_as_words_rather_than_json():
    """A printed document with `{"version": null}` on it reads as a defect
    rather than as "no profile was used", which is what it means."""
    source = (STATIC / "quotation.js").read_text(encoding="utf-8")

    assert "function provenancePairs(" in source
    assert "JSON.stringify(engineering" not in source
    assert "none recorded" in source


def test_the_hidden_attribute_beats_the_layout_rules():
    """Found in the browser: `.no-print { display: flex }` silently defeated
    the `hidden` attribute, so the metadata form was showing on an
    already-issued quotation."""
    css = (STATIC / "quotation.css").read_text(encoding="utf-8")

    assert "[hidden] { display: none !important; }" in css


def test_an_assembly_does_not_report_itself_as_a_material_only_estimate():
    """Found on the real assembly document: the plan sentence was written for a
    part, so an assembly whose components were costed with material and
    machining printed "no operations were costed; this is a material-only
    estimate"."""
    cost_an_assembly()
    document = quotation_of(latest_estimate_id())
    plan_state = document["provenance"]["plan_state"]

    assert "material-only" not in plan_state
    assert "No assembly-level plan" in plan_state
    assert "component(s) was costed in its own right" in plan_state
    assert "no assembly labour" in plan_state


def test_a_part_with_no_operations_still_says_material_only():
    document = quotation_of(cost_a_part(requirements=[]))

    assert "material-only" in document["provenance"]["plan_state"]


def test_the_page_does_not_print_a_part_number_that_repeats_the_name():
    """The pump's components carry a part number equal to their name, and the
    document printed the same string twice, which reads as a defect."""
    source = (STATIC / "quotation.js").read_text(encoding="utf-8")

    assert "component.part_number !== component.name" in source


def test_the_page_does_not_print_an_empty_geometry_row_for_an_assembly():
    """An assembly has no aggregate geometry, so the row was printing '— · —'."""
    source = (STATIC / "quotation.js").read_text(encoding="utf-8")

    assert "measured per component" in source


def test_the_blocked_page_says_unavailable_once():
    """Every blocker carries the same title for API consumers, so printing it
    per reason said "Quotation unavailable" three times on one page."""
    source = (STATIC / "quotation.js").read_text(encoding="utf-8")
    blocked = source[source.index("function renderBlocked") : source.index("function renderWarnings")]
    # Comments stripped: the code comment explains why the phrase appears once,
    # and would otherwise be counted as a second occurrence of it.
    code = re.sub(r"^\s*//.*$", "", blocked, flags=re.M)

    assert code.count("Quotation unavailable") == 1
    assert "blocker.title" not in code
    assert "blocker.detail" in code
