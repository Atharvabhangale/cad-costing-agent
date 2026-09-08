"""Assembly decomposition: structure, quantities, placements and roll-up.

The thing being defended here is that an assembly stops being one lump of
geometry without becoming a set of invented facts. So most of these tests are
about what is *not* claimed: that a multi-body part is not an assembly, that a
component with no geometry is not given the assembly's, that a sub-assembly is
not costed on top of its own children, and that a component nobody could cost
is not counted as zero.

The arithmetic is exact and stated in `assembly_fixtures.py` rather than
observed, so a wrong volume or a lost occurrence fails against a number that
was computed by hand.
"""

from functools import lru_cache
from pathlib import Path

import pytest
import trimesh
from fastapi.testclient import TestClient

from cad_costing_agent import limits
from cad_costing_agent.assembly_costing import (
    ASSEMBLY_SCOPE_NOTE,
    CLASSIFICATION_SCOPE_NOTE,
    CURRENCY_MISMATCH,
    NO_GEOMETRY,
    NOT_ESTIMATED,
    SUB_ASSEMBLY,
    AssemblyCostSummary,
    ComponentEstimate,
    roll_up,
)
from cad_costing_agent.cad_assembly import read_step_assembly
from cad_costing_agent.cad_import import decompose_assembly, detect_format, import_cad_document
from cad_costing_agent.cad_import.model import (
    AGGREGATE_ONLY,
    ASSEMBLY,
    COSTING,
    GEOMETRY,
    PARTIALLY_SUPPORTED,
    STRUCTURE_AVAILABLE,
    SUPPORTED,
    UNSUPPORTED,
)
from cad_costing_agent.cad_mesh import build_assembly_mesh
from cad_costing_agent.features import recognize_component_features
from cad_costing_agent.main import app
from tests import assembly_fixtures
from tests.step_fixtures import block_with_through_hole, plain_block

client = TestClient(app)

REAL_MODELS = Path(__file__).resolve().parent.parent / "cadmodels"


@lru_cache(maxsize=None)
def fixture(name: str) -> bytes:
    return getattr(assembly_fixtures, name)()


@lru_cache(maxsize=None)
def structure(name: str):
    return read_step_assembly(fixture(name))


def post(path: str, filename: str, data: bytes, **params):
    return client.post(
        path, files={"file": (filename, data, "application/octet-stream")}, params=params
    )


def assembly_of(name: str):
    adapter, _ = detect_format(f"{name}.step", fixture(name))
    return decompose_assembly(fixture(name), f"{name}.step", adapter)


# --- part versus assembly ---------------------------------------------------


def test_a_single_part_is_not_an_assembly():
    assembly = assembly_of("pump_assembly")
    part = decompose_assembly(
        plain_block(), "block.step", detect_format("block.step", plain_block())[0]
    )

    assert assembly.structure_state == STRUCTURE_AVAILABLE
    assert part.structure_state == AGGREGATE_ONLY
    assert part.occurrence_count == 0


def test_the_document_says_at_upload_which_it_is():
    """Required in its own right: a 198 MB assembly must not be discovered to
    have been treated as a part only after it has been costed."""
    assembly = import_cad_document(fixture("pump_assembly"), "pump.step", with_geometry=False)
    part = import_cad_document(plain_block(), "block.step", with_geometry=False)

    assert assembly.structure_state == STRUCTURE_AVAILABLE
    assert assembly.is_assembly is True
    assert "Decompose it" in assembly.assembly_note
    assert part.structure_state == AGGREGATE_ONLY
    assert part.is_assembly is False


def test_a_part_reports_no_assembly_capability():
    document = import_cad_document(plain_block(), "block.step", with_geometry=False)

    assert document.capability_matrix[ASSEMBLY] == UNSUPPORTED
    assert "several solid bodies is still a part" in document.capability(ASSEMBLY).detail


@pytest.mark.skipif(
    not (REAL_MODELS / "model2.stp").is_file(), reason="the real multi-body part is not present"
)
def test_a_real_multi_body_part_is_not_reported_as_an_assembly():
    """`model2.stp` is a Siemens NX export with two solid bodies under one
    product and no assembly entities. Two bodies are a property of a part."""
    data = (REAL_MODELS / "model2.stp").read_bytes()
    assert data.count(b"MANIFOLD_SOLID_BREP") == 2
    assert data.count(b"NEXT_ASSEMBLY_USAGE_OCCURRENCE") == 0

    result = read_step_assembly(data)

    assert result.is_assembly is False
    assert len(result.occurrences) == 0
    assert len(result.part_definitions) == 1
    assert result.part_definitions[0].solid_count == 2


# --- hierarchy --------------------------------------------------------------


def test_the_hierarchy_is_preserved_rather_than_flattened():
    assembly = assembly_of("pump_assembly")

    roots = assembly.children_of(None)
    assert {occurrence.instance_name for occurrence in roots} == {"HOUSING", "COVER_ASSY"}

    cover_assembly = next(o for o in roots if o.instance_name == "COVER_ASSY")
    children = assembly.children_of(cover_assembly.occurrence_id)
    assert sorted(child.instance_name for child in children) == ["COVER", "PIN", "PIN"]


def test_every_child_points_at_a_parent_that_exists():
    assembly = assembly_of("pump_assembly")
    known = {occurrence.occurrence_id for occurrence in assembly.occurrences}

    for occurrence in assembly.occurrences:
        if occurrence.parent_occurrence_id is not None:
            assert occurrence.parent_occurrence_id in known


def test_depth_survives_four_levels():
    assembly = assembly_of("deep_assembly")

    assert assembly.max_depth == 4
    assert [o.depth for o in sorted(assembly.occurrences, key=lambda x: x.depth)] == [1, 2, 3, 4]
    assert [o.instance_name for o in sorted(assembly.occurrences, key=lambda x: x.depth)] == [
        "L1_SUB",
        "L2_SUB",
        "L3_SUB",
        "BLOCK",
    ]


def test_a_flat_assembly_is_reported_as_flat():
    assembly = assembly_of("flat_assembly")

    assert assembly.max_depth == 1
    assert assembly.occurrence_count == 3
    assert all(o.parent_occurrence_id is None for o in assembly.occurrences)


def test_component_ids_are_unique():
    """Two placements of the same part must not share an occurrence id, or a
    selection in the viewer would highlight both."""
    assembly = assembly_of("pump_assembly")

    ids = [occurrence.occurrence_id for occurrence in assembly.occurrences]
    assert len(ids) == len(set(ids))

    definition_ids = [definition.definition_id for definition in assembly.definitions]
    assert len(definition_ids) == len(set(definition_ids))


# --- definitions versus occurrences ----------------------------------------


def test_a_repeated_part_is_one_definition_and_many_occurrences():
    """The distinction the whole model rests on. Storing the housing twice
    would duplicate its geometry; reporting it once would lose a housing."""
    assembly = assembly_of("pump_assembly")

    housings = [o for o in assembly.occurrences if o.instance_name == "HOUSING"]
    assert len(housings) == assembly_fixtures.HOUSING_QUANTITY
    assert len({o.definition_id for o in housings}) == 1

    definition = assembly.definition(housings[0].definition_id)
    assert definition.quantity == assembly_fixtures.HOUSING_QUANTITY


def test_geometry_is_held_once_per_definition_not_once_per_occurrence():
    assembly = assembly_of("pump_assembly")

    housing = next(d for d in assembly.definitions if d.name == "HOUSING")
    assert housing.quantity == 2
    assert housing.geometry is not None
    # There is exactly one geometry object for the two placements.
    assert sum(1 for d in assembly.definitions if d.name == "HOUSING") == 1


def test_quantity_is_an_occurrence_count_and_says_so():
    assembly = assembly_of("pump_assembly")
    pin = next(d for d in assembly.definitions if d.name == "PIN")

    assert pin.quantity == assembly_fixtures.PIN_QUANTITY
    field = type(pin).model_fields["quantity"]
    assert "not a production quantity" in field.description


def test_quantities_match_the_fixture_exactly():
    assembly = assembly_of("pump_assembly")
    quantities = {d.name: d.quantity for d in assembly.definitions if not d.is_assembly}

    assert quantities == {
        "HOUSING": assembly_fixtures.HOUSING_QUANTITY,
        "COVER": assembly_fixtures.COVER_QUANTITY,
        "PIN": assembly_fixtures.PIN_QUANTITY,
    }


# --- transforms -------------------------------------------------------------


def test_a_placement_is_preserved_relative_to_its_parent_and_to_the_assembly():
    assembly = assembly_of("pump_assembly")

    pin = next(
        o
        for o in assembly.occurrences
        if o.instance_name == "PIN"
        and o.transform.local_translation == assembly_fixtures.PIN_OFFSETS[0]
    )

    assert pin.transform.local_translation == assembly_fixtures.PIN_OFFSETS[0]
    # Local (10, 20, 5) inside a sub-assembly placed at (0, 0, 20).
    assert pin.transform.world_translation == (10.0, 20.0, 25.0)


def test_transforms_compose_down_four_levels():
    assembly = assembly_of("deep_assembly")
    block = next(o for o in assembly.occurrences if o.instance_name == "BLOCK")

    assert block.transform.world_translation == assembly_fixtures.DEEP_BLOCK_WORLD


def test_an_unplaced_component_reports_an_identity_transform():
    assembly = assembly_of("pump_assembly")
    housing = next(
        o for o in assembly.occurrences if o.instance_name == "HOUSING" and o.depth == 1
    )

    assert housing.transform.is_identity is True


def test_a_transform_crosses_the_boundary_as_plain_numbers():
    """Nothing outside the reader may hold an OpenCASCADE handle."""
    assembly = assembly_of("pump_assembly")
    payload = assembly.model_dump(mode="json")

    text = repr(payload)
    for leak in ("gp_Trsf", "TopLoc", "TopoDS", "OCP."):
        assert leak not in text
    assert all(isinstance(value, float) for value in payload["occurrences"][0]["transform"]["world"])


# --- component geometry -----------------------------------------------------


def test_each_component_is_measured_from_its_own_shape():
    assembly = assembly_of("pump_assembly")
    volumes = {
        d.name: d.geometry.volume for d in assembly.definitions if d.geometry is not None
    }

    assert volumes["HOUSING"] == pytest.approx(assembly_fixtures.HOUSING_VOLUME)
    assert volumes["COVER"] == pytest.approx(assembly_fixtures.COVER_VOLUME)
    assert volumes["PIN"] == pytest.approx(assembly_fixtures.PIN_VOLUME, rel=1e-3)


def test_no_component_is_given_the_assemblys_measurements():
    """The failure this replaced: 43 components each reported as weighing what
    the whole assembly weighs."""
    assembly = assembly_of("pump_assembly")
    measured = [d for d in assembly.definitions if d.geometry is not None]

    assert len({round(d.geometry.volume, 6) for d in measured}) == len(measured)
    assert all(
        d.geometry.volume < assembly_fixtures.PUMP_TOTAL_VOLUME for d in measured
    )


def test_a_sub_assembly_is_not_measured_in_its_own_right():
    """Measuring it as well as its children would count the same material twice."""
    assembly = assembly_of("pump_assembly")
    cover_assembly = next(d for d in assembly.definitions if d.name == "COVER_ASSY")

    assert cover_assembly.is_assembly is True
    assert cover_assembly.geometry is None
    assert cover_assembly.geometry_status == "not_measured"
    assert "twice" in cover_assembly.geometry_note


def test_the_components_add_up_to_the_assembly():
    """A cross-check on the decomposition itself, not on any cost."""
    assembly = assembly_of("pump_assembly")
    total = sum(
        d.geometry.volume * d.quantity
        for d in assembly.definitions
        if d.geometry is not None
    )

    assert total == pytest.approx(assembly_fixtures.PUMP_TOTAL_VOLUME, rel=1e-3)


def test_measurement_can_be_skipped_for_structure_alone():
    adapter, _ = detect_format("pump.step", fixture("pump_assembly"))
    assembly = decompose_assembly(
        fixture("pump_assembly"), "pump.step", adapter, measure_components=False
    )

    assert assembly.occurrence_count == 6
    assert all(d.geometry is None for d in assembly.definitions)
    assert {d.geometry_status for d in assembly.definitions} == {"not_measured"}


# --- component capabilities -------------------------------------------------


def test_a_measured_component_reports_what_it_can_do():
    assembly = assembly_of("pump_assembly")
    housing = next(d for d in assembly.definitions if d.name == "HOUSING")

    matrix = {report.capability: report.support for report in housing.capabilities}
    assert matrix[GEOMETRY] == SUPPORTED
    assert matrix[COSTING] == SUPPORTED


def test_a_sub_assembly_reports_that_it_is_not_a_part():
    assembly = assembly_of("pump_assembly")
    cover_assembly = next(d for d in assembly.definitions if d.name == "COVER_ASSY")

    matrix = {report.capability: report.support for report in cover_assembly.capabilities}
    assert matrix[COSTING] == UNSUPPORTED
    assert "not a part" in cover_assembly.capabilities[0].detail


def test_the_assembly_capability_states_what_is_not_implemented():
    assembly = assembly_of("pump_assembly")

    detail = assembly.capabilities[0].detail
    assert assembly.capabilities[0].support == PARTIALLY_SUPPORTED
    for missing in ("assembly labour", "fastening", "purchased-part", "make/buy"):
        assert missing in detail


def test_material_is_left_unresolved_rather_than_inferred():
    """Nothing in a STEP file says what a component is made of, and nothing
    here guesses from a name, a density or a shape."""
    assembly = assembly_of("pump_assembly")

    assert all(definition.material is None for definition in assembly.definitions)


# --- component features -----------------------------------------------------


def test_features_can_be_recognized_on_one_component():
    from cad_costing_agent.cad_geometry import read_step_shape

    shape = read_step_shape(block_with_through_hole())
    result = recognize_component_features(shape, component_id="BRACKET", component_name="BRACKET")

    assert result.component_id == "BRACKET"
    assert result.feature_counts["hole"] == 1


def test_component_feature_ids_are_unique_across_the_assembly():
    """Two components each having a first hole must not both call it H-001."""
    from cad_costing_agent.cad_geometry import read_step_shape

    shape = read_step_shape(block_with_through_hole())
    first = recognize_component_features(shape, component_id="PART_A")
    second = recognize_component_features(shape, component_id="PART_B")

    ids = [f.feature_id for f in first.features] + [f.feature_id for f in second.features]
    assert len(ids) == len(set(ids))
    assert all(identifier.startswith("PART_A/") for identifier in
               [f.feature_id for f in first.features])
    assert all(feature.component_id == "PART_B" for feature in second.features)


def test_a_feature_carries_its_component_so_an_operation_can_be_traced_back():
    from cad_costing_agent.cad_geometry import read_step_shape
    from cad_costing_agent.operation_suggestions import suggest_operations

    shape = read_step_shape(block_with_through_hole())
    recognition = recognize_component_features(shape, component_id="HOUSING")
    suggestions = suggest_operations(recognition)

    assert suggestions.suggestions
    for suggestion in suggestions.suggestions:
        assert suggestion.component_id == "HOUSING"
        assert suggestion.feature_id.startswith("HOUSING/")
        # component -> feature -> operation, unbroken.
        assert suggestion.feature_id in {f.feature_id for f in recognition.features}


def test_the_component_feature_endpoint_answers_for_one_component():
    assembly = assembly_of("pump_assembly")
    pin = next(d for d in assembly.definitions if d.name == "PIN")

    response = client.post(
        "/cad/assembly/features",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
        data={"component_id": pin.definition_id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["component_name"] == "PIN"
    assert all(f["component_id"] == "PIN" for f in body["features"])


def test_asking_for_a_component_that_does_not_exist_says_so():
    response = client.post(
        "/cad/assembly/features",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
        data={"component_id": "0:9:9:9"},
    )

    assert response.status_code == 404
    assert "Known components" in response.json()["detail"]


# --- the viewer contract ----------------------------------------------------


def test_the_assembly_mesh_is_definitions_plus_placements():
    """Step 25 replaced the baked single buffer with shared geometry and a
    transform per occurrence. A baked buffer welds each component's world
    position into its vertices, so moving one means rewriting geometry — which
    is exactly what an exploded view has to do, and what it must not do to CAD
    data."""
    mesh = build_assembly_mesh(structure("pump_assembly"))

    assert len(mesh.definitions) == 3  # HOUSING, COVER, PIN
    assert len(mesh.instances) == 5  # leaf placements only
    assert {definition.definition_id for definition in mesh.definitions} == {
        instance.definition_id for instance in mesh.instances
    }


def test_every_instance_carries_its_own_cad_placement():
    mesh = build_assembly_mesh(structure("pump_assembly"))

    for instance in mesh.instances:
        assert len(instance.transform) == 12
    translations = sorted(tuple(instance.transform[9:]) for instance in mesh.instances)
    assert (100.0, 0.0, 0.0) in translations  # the second housing
    assert (10.0, 20.0, 25.0) in translations  # a pin, in world space


def test_a_sub_assembly_is_not_drawn_on_top_of_its_children():
    mesh = build_assembly_mesh(structure("pump_assembly"))

    assert "COVER_ASSY" not in {instance.name for instance in mesh.instances}
    assert "COVER_ASSY" not in {definition.name for definition in mesh.definitions}


def test_components_are_drawn_where_the_assembly_puts_them():
    """A lost transform would stack every component at the origin."""
    mesh = build_assembly_mesh(structure("pump_assembly"))

    # Two housings 60 wide, the second at x = 100.
    assert mesh.bounds.max[0] == pytest.approx(160.0)
    # The cover sub-assembly sits at z = 20 and its pins are 30 tall.
    assert mesh.bounds.max[2] == pytest.approx(55.0)


def test_a_definition_used_twice_is_meshed_once_and_placed_twice():
    """Now literally true rather than merely true of the tessellation: the
    housing's vertices are transmitted once and drawn from twice."""
    mesh = build_assembly_mesh(structure("pump_assembly"))

    housings = [instance for instance in mesh.instances if instance.name == "HOUSING"]
    assert len(housings) == 2
    assert len({instance.definition_id for instance in housings}) == 1

    definitions = [d for d in mesh.definitions if d.name == "HOUSING"]
    assert len(definitions) == 1
    # Drawn twice, sent once.
    assert mesh.drawn_triangle_count > mesh.unique_triangle_count
    assert mesh.unique_triangle_count == sum(d.triangle_count for d in mesh.definitions)


def test_the_payload_does_not_grow_with_repetition():
    """The property that makes a large assembly viewable at all."""
    mesh = build_assembly_mesh(structure("pump_assembly"))
    sent = sum(len(definition.positions) for definition in mesh.definitions)
    drawn = sum(
        len(next(d for d in mesh.definitions if d.definition_id == instance.definition_id).positions)
        for instance in mesh.instances
    )

    assert sent < drawn


def test_decimation_no_longer_costs_component_identity(monkeypatch):
    """Step 24 had to withdraw selection when the display mesh was decimated,
    because renumbering one shared index buffer made every component range
    point at the wrong triangles. With a geometry per definition, decimation
    reduces each definition in place and every component stays selectable."""
    import cad_costing_agent.cad_mesh as mesh_module

    monkeypatch.setattr(mesh_module, "MAX_TRIANGLES", 40)
    mesh = build_assembly_mesh(structure("pump_assembly"))

    assert mesh.display_mesh_simplified is True
    assert len(mesh.instances) == 5
    assert len(mesh.definitions) == 3
    assert mesh.simplification_method
    assert "individually selectable" in mesh.tessellation.note


def test_no_definition_is_decimated_out_of_existence(monkeypatch):
    """A component reduced to nothing would vanish from the assembly, which
    reads as a missing part rather than a coarse one."""
    import cad_costing_agent.cad_mesh as mesh_module

    monkeypatch.setattr(mesh_module, "MAX_TRIANGLES", 1)
    mesh = build_assembly_mesh(structure("pump_assembly"))

    assert len(mesh.definitions) == 3
    for definition in mesh.definitions:
        assert definition.triangle_count > 0


def test_a_single_part_is_still_drawn_the_way_it_always_was():
    """`/cad/mesh` is unchanged: a part is one mesh, with no assembly
    apparatus bolted onto it."""
    response = post("/cad/mesh", "block.step", plain_block())

    assert response.status_code == 200
    mesh = response.json()["mesh"]
    assert mesh["triangle_count"] > 0
    assert "instances" not in mesh
    assert "definitions" not in mesh


# --- the endpoint -----------------------------------------------------------


def test_the_assembly_endpoint_returns_structure_and_a_mesh():
    response = post("/cad/assembly", "pump.step", fixture("pump_assembly"), with_mesh="true")

    assert response.status_code == 200
    body = response.json()
    assembly = body["document"]["assembly"]
    assert assembly["name"] == "PUMP"
    assert len(assembly["definitions"]) == 5
    assert len(assembly["occurrences"]) == 6
    assert assembly["max_depth"] == 2
    assert len(body["mesh"]["instances"]) == 5
    assert len(body["mesh"]["definitions"]) == 3


def test_the_assembly_endpoint_reports_a_part_as_a_part():
    response = post("/cad/assembly", "block.step", plain_block())

    assert response.status_code == 200
    document = response.json()["document"]
    assert document["structure_state"] == AGGREGATE_ONLY
    assert document["assembly"]["occurrences"] == []


def test_a_format_with_no_structure_says_so_rather_than_failing():
    mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0)).export(file_type="stl")

    response = post("/cad/assembly", "part.stl", mesh, with_mesh="true")

    assert response.status_code == 200
    body = response.json()
    assert body["document"]["assembly"]["structure_state"] == AGGREGATE_ONLY
    assert "no component structure" in body["document"]["assembly"]["warnings"][0]["message"]
    assert "nothing to draw" in body["mesh_error"]


def test_decomposition_is_deterministic():
    excluded = {"read_ms"}
    first = assembly_of("pump_assembly").model_dump(exclude=excluded)
    second = decompose_assembly(
        fixture("pump_assembly"),
        "pump_assembly.step",
        detect_format("pump_assembly.step", fixture("pump_assembly"))[0],
    ).model_dump(exclude=excluded)

    assert first == second


def test_the_component_budget_truncates_and_says_so(monkeypatch):
    monkeypatch.setattr(limits, "MAX_ASSEMBLY_COMPONENTS", 2)
    import cad_costing_agent.cad_assembly as assembly_module

    monkeypatch.setattr(assembly_module.limits, "MAX_ASSEMBLY_COMPONENTS", 2)
    result = read_step_assembly(fixture("pump_assembly"))

    assert result.truncated is True
    assert len(result.occurrences) <= 2
    assert any("budget" in note for note in result.notes)


# --- rolling costs up -------------------------------------------------------


def _estimate(definition_id, per_unit, currency="INR"):
    return ComponentEstimate(
        definition_id=definition_id,
        estimate_id=f"est-{definition_id}",
        currency=currency,
        per_unit_cost=per_unit,
        material_per_unit=per_unit,
    )


def test_a_component_cost_is_multiplied_by_its_quantity():
    assembly = assembly_of("pump_assembly")
    housing = next(d for d in assembly.definitions if d.name == "HOUSING")

    summary = roll_up(assembly, {housing.definition_id: _estimate(housing.definition_id, 500.0)})
    line = next(line for line in summary.lines if line.name == "HOUSING")

    assert line.quantity == 2
    assert line.per_unit_cost == 500.0
    assert line.extended_cost == 1000.0


def test_the_total_is_the_sum_of_the_extended_costs():
    assembly = assembly_of("pump_assembly")
    by_name = {d.name: d.definition_id for d in assembly.definitions}
    summary = roll_up(
        assembly,
        {
            by_name["HOUSING"]: _estimate(by_name["HOUSING"], 500.0),
            by_name["COVER"]: _estimate(by_name["COVER"], 200.0),
            by_name["PIN"]: _estimate(by_name["PIN"], 50.0),
        },
    )

    # 500x2 + 200x1 + 50x2
    assert summary.total_cost == pytest.approx(1300.0)
    assert summary.is_complete is True
    assert summary.costed_occurrence_count == 5
    # Five of the six occurrences are parts; the sixth places a sub-assembly,
    # which is not a thing to be costed and is not in the denominator.
    assert summary.occurrence_count == 6
    assert summary.part_occurrence_count == 5
    assert "all 5 part occurrences" in summary.completeness_note


def test_an_uncosted_component_is_never_counted_as_zero():
    assembly = assembly_of("pump_assembly")
    by_name = {d.name: d.definition_id for d in assembly.definitions}
    summary = roll_up(assembly, {by_name["HOUSING"]: _estimate(by_name["HOUSING"], 500.0)})

    assert summary.total_cost == 1000.0  # the housings only
    assert summary.is_complete is False
    uncosted = [line for line in summary.lines if not line.costed and not line.is_assembly]
    assert {line.name for line in uncosted} == {"COVER", "PIN"}
    for line in uncosted:
        assert line.extended_cost is None
        assert line.reason == NOT_ESTIMATED
        assert line.reason_detail


def test_an_incomplete_total_says_it_is_not_a_quotation():
    assembly = assembly_of("pump_assembly")
    by_name = {d.name: d.definition_id for d in assembly.definitions}
    summary = roll_up(assembly, {by_name["HOUSING"]: _estimate(by_name["HOUSING"], 500.0)})

    assert "partial sum" in summary.completeness_note
    assert "not a quotation" in summary.completeness_note


def test_with_nothing_costed_there_is_no_total_at_all():
    summary = roll_up(assembly_of("pump_assembly"), {})

    assert summary.total_cost is None
    assert summary.is_complete is False
    assert "Nothing is reported as zero" in summary.completeness_note


def test_a_sub_assembly_is_not_costed_on_top_of_its_children():
    assembly = assembly_of("pump_assembly")
    summary = roll_up(assembly, {})
    sub = next(line for line in summary.lines if line.name == "COVER_ASSY")

    assert sub.reason == SUB_ASSEMBLY
    assert "double it" in sub.reason_detail
    # And it is not counted among the parts that are missing a cost.
    assert summary.uncosted_definition_count == 3


def test_a_component_with_no_geometry_says_that_is_why():
    assembly = assembly_of("pump_assembly")
    stripped = assembly.model_copy(
        update={
            "definitions": [
                definition.model_copy(update={"geometry": None, "geometry_status": "no_geometry_in_file"})
                if definition.name == "PIN"
                else definition
                for definition in assembly.definitions
            ]
        }
    )

    summary = roll_up(stripped, {})
    pin = next(line for line in summary.lines if line.name == "PIN")

    assert pin.reason == NO_GEOMETRY
    assert "nothing to cost" in pin.reason_detail


def test_a_differently_denominated_component_is_left_out_rather_than_converted():
    assembly = assembly_of("pump_assembly")
    by_name = {d.name: d.definition_id for d in assembly.definitions}
    summary = roll_up(
        assembly,
        {
            by_name["HOUSING"]: _estimate(by_name["HOUSING"], 500.0, "INR"),
            by_name["COVER"]: _estimate(by_name["COVER"], 200.0, "USD"),
        },
    )
    cover = next(line for line in summary.lines if line.name == "COVER")

    assert summary.currency == "INR"
    assert summary.total_cost == 1000.0
    assert cover.costed is False
    assert cover.reason == CURRENCY_MISMATCH


def test_every_summary_states_what_the_total_does_not_include():
    """Step 26 moved purchased parts and make/buy from the absent list into the
    model, so the note names what is *still* missing rather than repeating a
    limitation that no longer applies."""
    summary = roll_up(assembly_of("pump_assembly"), {})

    assert ASSEMBLY_SCOPE_NOTE in summary.notes
    assert CLASSIFICATION_SCOPE_NOTE in summary.notes
    for missing in ("assembly labour", "fastening"):
        assert missing in ASSEMBLY_SCOPE_NOTE
    # And the classification note is honest about what the new model does not do.
    for missing in ("no price is looked up", "no cost is derived from a component's weight"):
        assert missing in CLASSIFICATION_SCOPE_NOTE


def test_the_assembly_cost_endpoint_rolls_the_real_pipeline_up():
    response = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
        data={"material": "Steel", "source_unit": "mm", "rate_set_id": "poc-machine-shop"},
    )

    assert response.status_code == 200
    summary = response.json()
    assert summary["currency"] == "INR"
    assert summary["part_definition_count"] == 3
    assert summary["occurrence_count"] == 6
    # Every definition was costed — coverage is complete — but no manufacturing
    # time has been committed for any of them, so the total is a partial one and
    # now says so. The arithmetic below is unaffected and is the point of this
    # test; see test_component_plans.py for the committed case.
    assert summary["is_complete"] is False
    assert summary["unplanned_definition_count"] == 3
    assert summary["uncosted_definition_count"] == 0

    costed = {line["name"]: line for line in summary["lines"] if line["costed"]}
    assert set(costed) == {"HOUSING", "COVER", "PIN"}
    # The total is exactly the sum of the extended lines, not a second calculation.
    assert summary["total_cost"] == pytest.approx(
        sum(line["extended_cost"] for line in costed.values())
    )
    # And each extended line is its per-unit cost times its quantity.
    for line in costed.values():
        assert line["extended_cost"] == pytest.approx(line["per_unit_cost"] * line["quantity"])


def test_the_assembly_estimate_is_persisted_with_its_structure():
    """Requirement of its own: a historical estimate keeps the hierarchy, the
    quantities and the component costs, and a later CAD change cannot rewrite
    it."""
    before = client.get("/estimates").json()["total"]
    client.post(
        "/cost/assembly",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
        data={"material": "Steel", "source_unit": "mm", "rate_set_id": "poc-machine-shop"},
    )
    listing = client.get("/estimates").json()
    assert listing["total"] == before + 1

    estimate_id = listing["items"][0]["estimate_id"]
    stored = client.get(f"/estimates/{estimate_id}").json()

    snapshot = stored["request_snapshot"]["assembly"]
    assert snapshot["definition_count"] == 5
    assert snapshot["occurrence_count"] == 6
    assert snapshot["max_depth"] == 2
    quantities = {d["name"]: d["quantity"] for d in snapshot["definitions"]}
    assert quantities["HOUSING"] == 2
    # Placements travel with it, so a future exploded view can be rebuilt.
    assert any(o["transform"]["world"][11] == 20.0 for o in snapshot["occurrences"])
    # And the component costs are in the result snapshot.
    assert stored["result_snapshot"]["lines"]


def _package_imports_of(module) -> set[str]:
    import ast
    from pathlib import Path as _Path

    tree = ast.parse(_Path(module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return {name for name in imported if name.startswith("cad_costing_agent")}


def test_the_assembly_roll_up_imports_no_calculator_and_no_repository():
    """Same boundary the comparison layer keeps: this module multiplies and
    adds figures it is given, and cannot reach the engine that made them.

    Step 26 added exactly one package import — the classification vocabulary —
    and the next test proves that vocabulary is a leaf, so the boundary is
    unchanged rather than merely one hop longer.
    """
    import cad_costing_agent.assembly_costing as module

    assert _package_imports_of(module) == {"cad_costing_agent.component_classification"}


def test_the_classification_vocabulary_is_a_leaf():
    import cad_costing_agent.component_classification as module

    assert _package_imports_of(module) == set()


# --- the browser adds nothing up -------------------------------------------


STATIC = Path(__file__).resolve().parent.parent / "src" / "cad_costing_agent" / "static"


def test_the_assembly_panel_is_served():
    for asset in ("assembly.js",):
        response = client.get(f"/ui/{asset}")
        assert response.status_code == 200
        assert response.content
    assert '<script src="assembly.js"></script>' in (STATIC / "index.html").read_text(
        encoding="utf-8"
    )


def test_the_browser_computes_no_assembly_arithmetic():
    """The same rule the rest of this UI follows. A quantity multiplied in
    JavaScript beside one multiplied on the server is indistinguishable from
    it, and only one of them is the authority.
    """
    import re

    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    # No multiplication by a quantity and no summation: those are the two
    # operations that would produce a second, competing total.
    for forbidden in ("per_unit_cost *", "* line.quantity", "* definition.quantity", "reduce("):
        assert forbidden not in source, forbidden

    # An assignment to a cost field on an object, as opposed to a comparison
    # against one or a local alias for a value the server sent.
    #
    # This assertion used to carry a literal backspace character where a
    # word-boundary escape was meant, so its pattern could never match and the
    # check had been silently passing on anything. Written out rather than
    # built by interpolation, so a stray escape cannot make it vacuous again.
    for pattern in (
        r"\.extended_cost\s*=(?!=)",
        r"\.total_cost\s*=(?!=)",
        r"\.per_unit_cost\s*=(?!=)",
        r"\.quantity\s*=(?!=)",
        r"\.production_extended_cost\s*=(?!=)",
    ):
        assert not re.search(pattern, source), pattern

    # And no arithmetic anywhere near a money field, however it is spelled.
    for pattern in (
        r"(?:extended_cost|per_unit_cost|unit_cost|total_cost)\s*[-+*/]",
        r"[-+*/]\s*(?:extended_cost|per_unit_cost|unit_cost|total_cost)\b",
    ):
        assert not re.search(pattern, source), pattern


def test_the_panel_reads_the_structure_state_rather_than_guessing():
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    assert "structure_state" in source
    # It must not decide what an assembly is from a file extension or a count.
    assert ".stp" not in source
    assert ".step" not in source


def test_the_viewer_exposes_the_selection_contract():
    """`assembly.js` calls these; a rename that broke them would leave the
    component list clickable and inert."""
    source = (STATIC / "viewer.js").read_text(encoding="utf-8")

    for name in (
        "showAssembly",
        "highlightComponent",
        "isolateComponent",
        "restoreComponents",
        "setExplode",
        "resetExplode",
        "componentPlacement",
    ):
        assert f"{name}," in source, name
        assert f"function {name}(" in source, name
