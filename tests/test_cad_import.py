"""The unified CAD import layer: detection, capabilities, and IGES.

Two things are being tested here, and the second matters more than the first.

The first is that the layer works: a file is identified, normalized into a
`CADDocument`, and routed to the right reader, with IGES joining STEP and STL
as a format that genuinely parses.

The second is that it is **honest**. Accepting an upload is not supporting it,
and most of these tests exist to hold that line: that a capability which is not
fully supported always says why, that a volume is never reported for surfaces
that do not enclose one, that a unit is never assumed, and that a format with
no parser is refused with an explanation rather than accepted and quietly
ignored.
"""

from functools import lru_cache

import pytest
import trimesh
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cad_costing_agent import limits
from cad_costing_agent.cad_geometry import (
    COMPLETE,
    INCOMPLETE,
    extract_geometry_from_bytes,
    read_step_shape,
)
from cad_costing_agent.cad_iges import (
    analyse_iges_shape,
    detect_length_unit,
    looks_like_iges,
    read_iges_shape,
)
from cad_costing_agent.cad_import import (
    ADAPTERS,
    UnknownFormatError,
    UnsupportedFormatError,
    adapter_for,
    detect_format,
    format_catalog,
    import_cad_document,
    require,
    supported_extensions,
)
from cad_costing_agent.cad_import.model import (
    ASSEMBLY,
    CAPABILITIES,
    COSTING,
    DRAWINGS,
    FEATURES,
    GEOMETRY,
    MASS,
    PARTIALLY_SUPPORTED,
    REQUIRES_USER_INPUT,
    SUPPORTED,
    SUPPORT_LEVELS,
    SURFACE_AREA,
    UNIT_FILE_DECLARED,
    UNIT_NOT_CARRIED_BY_FORMAT,
    UNIT_UNDECLARED,
    UNSUPPORTED,
    VIEWER,
    CapabilityReport,
    weaker,
)
from cad_costing_agent.features import recognize_features, recognize_features_from_bytes
from cad_costing_agent.main import app
from tests import iges_fixtures
from tests.step_fixtures import block_with_through_hole, plain_block, to_step

client = TestClient(app)


@lru_cache(maxsize=None)
def iges(name: str) -> bytes:
    return getattr(iges_fixtures, name)()


@lru_cache(maxsize=None)
def stl_bytes() -> bytes:
    return trimesh.creation.box(extents=(40.0, 30.0, 20.0)).export(file_type="stl")


def post(path: str, filename: str, data: bytes, **params):
    return client.post(
        path, files={"file": (filename, data, "application/octet-stream")}, params=params
    )


# --- format detection -------------------------------------------------------


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("part.step", "step"),
        ("part.STEP", "step"),
        ("part.stp", "step"),
        ("part.stl", "stl"),
        ("part.iges", "iges"),
        ("part.igs", "iges"),
        ("part.IGS", "iges"),
        ("part.prt", "creo_part"),
        ("part.asm", "creo_assembly"),
        ("part.drw", "creo_drawing"),
    ],
)
def test_an_extension_selects_its_adapter(filename, expected):
    adapter, warnings = detect_format(filename)

    assert adapter.source_format == expected
    assert warnings == []


def test_every_recognized_extension_has_exactly_one_adapter():
    """Two adapters claiming one extension would make detection order-dependent."""
    for extension in supported_extensions():
        claimants = [a.source_format for a in ADAPTERS if extension in a.extensions]
        assert len(claimants) == 1, f".{extension} is claimed by {claimants}"


def test_an_unknown_extension_with_unidentifiable_content_is_refused():
    with pytest.raises(UnknownFormatError, match="Unsupported CAD file format"):
        detect_format("drawing.dwg", b"\x00\x01not a format this reads")


def test_the_refusal_names_what_is_actually_readable():
    with pytest.raises(UnknownFormatError) as failure:
        detect_format("x.dwg", b"nothing")

    message = str(failure.value)
    for extension in (".step", ".stp", ".stl", ".iges", ".igs"):
        assert extension in message


@pytest.mark.parametrize(
    "data, expected",
    [
        (plain_block(), "step"),
        (stl_bytes(), "stl"),
        (iges_fixtures.box_solid(), "iges"),
    ],
)
def test_content_alone_identifies_a_format_when_the_extension_does_not(data, expected):
    adapter, warnings = detect_format("noextension", data)

    assert adapter.source_format == expected
    assert [w.code for w in warnings] == ["extension_unrecognized"]


def test_a_wrongly_named_file_is_read_as_what_it_actually_is():
    """The extension chooses; the bytes decide.

    Parsing STEP content as STL would fail with a message about triangles that
    explains nothing, and refusing it outright would be unhelpful when the
    content is unambiguous.
    """
    document = import_cad_document(plain_block(), "block.stl")

    assert document.source_format == "step"
    assert document.extension == "stl"
    assert [w.code for w in document.warnings] == ["extension_content_mismatch"]
    assert "the extension is wrong, not the file" in document.warnings[0].message


def test_a_mislabelled_file_still_measures_correctly():
    correct = import_cad_document(plain_block(), "block.step")
    mislabelled = import_cad_document(plain_block(), "block.stl")

    assert mislabelled.geometry.volume == correct.geometry.volume


def test_stp_and_step_are_one_format():
    assert adapter_for("stp") is adapter_for("step")
    assert adapter_for("igs") is adapter_for("iges")


def test_binary_stl_is_identified_by_its_exact_size():
    """A binary STL has no magic number, only an 84-byte header whose triangle
    count must account for the rest of the file exactly."""
    data = stl_bytes()

    adapter, _ = detect_format("noextension", data)
    assert adapter.source_format == "stl"

    # One byte short: the size no longer agrees, so it is not claimed.
    adapter_for_truncated, _ = detect_format("part.stl", data[:-1])
    assert adapter_for_truncated.source_format == "stl"  # by extension only


# --- the capability model ---------------------------------------------------


def test_a_degraded_capability_must_state_a_reason():
    """The central rule of this layer, enforced rather than trusted.

    A capability reported as anything less than supported, with no explanation,
    reads to a user exactly like a working one.
    """
    for level in (PARTIALLY_SUPPORTED, REQUIRES_USER_INPUT, UNSUPPORTED):
        with pytest.raises(ValidationError, match="must say why"):
            CapabilityReport(capability=GEOMETRY, support=level)


def test_full_support_needs_no_excuse():
    assert CapabilityReport(capability=GEOMETRY, support=SUPPORTED).detail == ""


def test_an_unknown_capability_or_level_is_refused():
    with pytest.raises(ValidationError, match="Unknown capability"):
        CapabilityReport(capability="teleportation", support=SUPPORTED)
    with pytest.raises(ValidationError, match="Unknown support level"):
        CapabilityReport(capability=GEOMETRY, support="probably")


def test_weaker_never_promises_more():
    assert weaker(SUPPORTED, UNSUPPORTED) == UNSUPPORTED
    assert weaker(UNSUPPORTED, SUPPORTED) == UNSUPPORTED
    assert weaker(PARTIALLY_SUPPORTED, REQUIRES_USER_INPUT) == REQUIRES_USER_INPUT
    assert weaker(SUPPORTED, SUPPORTED) == SUPPORTED


def test_every_format_reports_every_capability():
    """A missing capability is not the same as an unsupported one, and would
    leave a consumer guessing."""
    for description in format_catalog():
        reported = [report.capability for report in description.capabilities]
        assert reported == list(CAPABILITIES), description.source_format


def test_every_declared_capability_uses_a_known_level_and_explains_itself():
    for description in format_catalog():
        for report in description.capabilities:
            assert report.support in SUPPORT_LEVELS
            if report.support != SUPPORTED:
                assert len(report.detail) > 40, (description.source_format, report.capability)


# --- the capability matrix, as it actually stands ---------------------------


@pytest.mark.parametrize(
    "source_format, expected",
    [
        (
            "step",
            {
                GEOMETRY: SUPPORTED,
                MASS: SUPPORTED,
                SURFACE_AREA: SUPPORTED,
                FEATURES: SUPPORTED,
                VIEWER: SUPPORTED,
                ASSEMBLY: PARTIALLY_SUPPORTED,
                DRAWINGS: UNSUPPORTED,
                COSTING: SUPPORTED,
            },
        ),
        (
            "stl",
            {
                GEOMETRY: SUPPORTED,
                MASS: PARTIALLY_SUPPORTED,
                SURFACE_AREA: SUPPORTED,
                FEATURES: UNSUPPORTED,
                VIEWER: SUPPORTED,
                ASSEMBLY: UNSUPPORTED,
                DRAWINGS: UNSUPPORTED,
                COSTING: PARTIALLY_SUPPORTED,
            },
        ),
        (
            "iges",
            {
                GEOMETRY: SUPPORTED,
                MASS: PARTIALLY_SUPPORTED,
                SURFACE_AREA: SUPPORTED,
                FEATURES: PARTIALLY_SUPPORTED,
                VIEWER: SUPPORTED,
                ASSEMBLY: UNSUPPORTED,
                DRAWINGS: UNSUPPORTED,
                COSTING: PARTIALLY_SUPPORTED,
            },
        ),
    ],
)
def test_the_declared_capability_matrix(source_format, expected):
    description = next(d for d in format_catalog() if d.source_format == source_format)

    assert description.capability_matrix == expected


@pytest.mark.parametrize("source_format", ["creo_part", "creo_assembly", "creo_drawing"])
def test_no_native_creo_capability_is_claimed(source_format):
    description = next(d for d in format_catalog() if d.source_format == source_format)

    assert set(description.capability_matrix.values()) == {UNSUPPORTED}
    assert description.any_support is False
    assert description.native_integration_required is True
    for report in description.capabilities:
        assert "licensed" in report.detail


def test_the_creo_refusal_says_what_the_file_is_and_what_to_do():
    part = next(d for d in format_catalog() if d.source_format == "creo_part")
    assembly = next(d for d in format_catalog() if d.source_format == "creo_assembly")
    drawing = next(d for d in format_catalog() if d.source_format == "creo_drawing")

    # Not a generic "unsupported": each says what that file actually is.
    assert "Siemens NX" in part.capabilities[0].detail  # .prt is not unique to Creo
    assert "references its member parts" in assembly.capabilities[0].detail
    assert "not a 3D model" in drawing.capabilities[0].detail
    assert "Export the part as STEP or IGES" in part.capabilities[0].detail


def test_the_formats_endpoint_serves_the_same_matrix():
    response = client.get("/cad/formats")

    assert response.status_code == 200
    served = {row["source_format"]: row for row in response.json()}
    assert set(served) == {a.source_format for a in ADAPTERS}
    for description in format_catalog():
        matrix = {
            report["capability"]: report["support"]
            for report in served[description.source_format]["capabilities"]
        }
        assert matrix == description.capability_matrix


# --- the normalized document ------------------------------------------------


def test_a_step_part_imports_as_one_component_holding_its_geometry():
    document = import_cad_document(plain_block(), "block.step")

    assert document.source_format == "step"
    assert document.format_label == "STEP / STP"
    assert document.representation == "brep"
    assert len(document.components) == 1
    component = document.components[0]
    assert component.component_id == "root"
    assert component.name == "block"
    assert component.kind == "part"
    assert component.geometry is not None
    assert component.geometry.volume == document.geometry.volume


def test_the_document_exposes_no_library_types():
    """Nothing downstream should be able to reach an OpenCASCADE handle.

    The whole point of normalizing is that a future adapter can use a different
    library without anything else noticing.
    """
    payload = import_cad_document(plain_block(), "block.step").model_dump(mode="json")
    text = repr(payload)

    for leak in ("TopoDS", "OCP.", "Bnd_Box", "IGESControl", "trimesh", "Trimesh"):
        assert leak not in text


def test_the_document_serializes_over_http():
    response = post("/cad/import", "block.step", plain_block())

    assert response.status_code == 200
    body = response.json()
    assert body["source_format"] == "step"
    assert body["units"] == "mm"
    assert body["unit_source"] == UNIT_FILE_DECLARED
    assert body["geometry"]["volume"] > 0
    assert {report["capability"] for report in body["capabilities"]} == set(CAPABILITIES)


def test_capabilities_can_be_reported_without_parsing_anything():
    """Identification must not require a parse, or describing a 200 MB export
    would cost what measuring it costs."""
    document = import_cad_document(plain_block(), "block.step", with_geometry=False)

    assert document.geometry is None
    assert document.parse_ms == 0.0
    assert document.capability_matrix[GEOMETRY] == SUPPORTED
    assert document.metadata.schema_name is not None


def test_import_timings_are_reported_separately():
    """Requirement of its own: the import layer's cost must be visible rather
    than folded into the geometry time it precedes."""
    document = import_cad_document(block_with_through_hole(), "block.step")

    assert document.detect_ms >= 0.0
    assert document.parse_ms > 0.0
    # Detection is a byte scan and a header read; parsing runs OpenCASCADE.
    assert document.detect_ms < document.parse_ms


def test_header_metadata_is_read_from_a_step_file():
    document = import_cad_document(plain_block(), "block.step", with_geometry=False)

    assert "10303" in (document.metadata.schema_name or "")
    assert document.metadata.byte_size == len(plain_block())
    assert document.metadata.entity_counts["advanced_face"] > 0


# --- assemblies -------------------------------------------------------------


def test_a_single_part_step_is_not_reported_as_an_assembly():
    document = import_cad_document(plain_block(), "block.step", with_geometry=False)

    assert document.is_assembly is False
    assert document.metadata.assembly_occurrence_count == 0
    assert document.capability_matrix[ASSEMBLY] == UNSUPPORTED
    assert "single part" in document.capability(ASSEMBLY).detail


def test_an_assembly_step_is_detected_and_says_it_is_costed_as_one_lump():
    """Detected from the STEP entity that records component placements, which
    is a byte count rather than a parse.

    Component structure is deliberately not extracted in this step — but a file
    that *is* an assembly must not be measured as a part in silence, because
    the volume is then the whole assembly's.
    """
    step = plain_block().replace(
        b"ENDSEC;", b"#9001=NEXT_ASSEMBLY_USAGE_OCCURRENCE('','','',#1,#2,$);\nENDSEC;", 1
    )

    document = import_cad_document(step, "assembly.step", with_geometry=False)

    assert document.is_assembly is True
    assert document.metadata.assembly_occurrence_count == 1
    assert document.capability_matrix[ASSEMBLY] == PARTIALLY_SUPPORTED
    assert "single lump of material" in document.assembly_note
    assert "Decompose it" in document.assembly_note
    assert "assembly_not_a_part" in [w.code for w in document.warnings]
    assert document.structure_state == "structure_available"


def test_the_component_tree_can_nest_without_changing_anything_downstream():
    """The model has to be able to hold an assembly before an adapter can fill
    one in. Nothing here builds a tree; this asserts the shape exists."""
    from cad_costing_agent.cad_import.model import CADComponent

    tree = CADComponent(
        component_id="asm",
        name="gearbox",
        kind="assembly",
        children=[
            CADComponent(component_id="c1", name="housing"),
            CADComponent(component_id="c2", name="cover"),
        ],
    )

    assert [child.name for child in tree.children] == ["housing", "cover"]
    assert tree.model_dump()["children"][0]["component_id"] == "c1"


# --- IGES: geometry ---------------------------------------------------------


def test_an_iges_solid_is_measured_exactly():
    geometry = extract_geometry_from_bytes(iges("box_solid"), "iges")

    assert geometry.source_format == "iges"
    assert geometry.volume == pytest.approx(24000.0)
    assert geometry.surface_area == pytest.approx(5200.0)
    assert geometry.bounding_box.x == pytest.approx(40.0)
    assert geometry.bounding_box.y == pytest.approx(30.0)
    assert geometry.bounding_box.z == pytest.approx(20.0)
    assert geometry.geometry_source == "brep"
    assert geometry.volume_method == "brep_mass_properties"
    assert geometry.volume_confidence == "high"
    assert geometry.repair_applied is False


def test_an_iges_part_with_a_hole_matches_the_arithmetic():
    geometry = extract_geometry_from_bytes(iges("block_with_hole_solid"), "iges")

    assert geometry.volume == pytest.approx(iges_fixtures.BLOCK_WITH_HOLE_VOLUME)


def test_iges_surfaces_that_close_are_measured_and_say_they_were_sewn():
    """The common IGES export has no solid. Sewing it is a reconstruction, and
    is reported as one rather than passed off as a measurement."""
    geometry = extract_geometry_from_bytes(iges("box_surfaces"), "iges")

    assert geometry.volume == pytest.approx(24000.0)
    assert geometry.repair_applied is True
    assert geometry.repair_method == "sewn_free_surfaces_into_closed_shell"
    assert geometry.repair_tolerance_mm is not None and geometry.repair_tolerance_mm > 0
    assert geometry.volume_confidence == "repaired"


def test_surfaces_that_do_not_enclose_a_solid_yield_no_volume():
    """The reason IGES needed its own closure test.

    `BRepGProp.VolumeProperties` returns a number for an open surface set just
    as readily as for a solid — on these three box faces it returns 10666.67 —
    and nothing about the number reveals that it is the volume of nothing.
    """
    geometry = extract_geometry_from_bytes(iges("open_surfaces"), "iges")

    assert geometry.geometry_status == INCOMPLETE
    assert geometry.volume == 0.0
    assert geometry.volume_method == "unavailable"
    assert geometry.volume_confidence == "unavailable"
    # The surface area is real and is still reported.
    assert geometry.surface_area == pytest.approx(2000.0)


def test_the_open_surface_refusal_explains_itself_in_the_file_s_own_terms():
    geometry = extract_geometry_from_bytes(iges("open_surfaces"), "iges")

    assert geometry.incomplete is not None
    assert geometry.incomplete.reason == "surfaces_do_not_enclose_a_solid"
    unresolved = geometry.incomplete.unresolved[0]
    assert unresolved.face_count == 3
    assert unresolved.free_edge_count > 0
    # Face counts are not reported in fields that mean polygons.
    assert unresolved.polygon_count == 0
    assert "do not enclose a volume" in geometry.incomplete.explanation
    assert "re-export with solid geometry" in geometry.incomplete.explanation.lower()


def test_the_open_surface_case_offers_the_same_resolutions_as_a_faceted_one():
    """It reuses the existing machinery rather than inventing a second one."""
    geometry = extract_geometry_from_bytes(iges("open_surfaces"), "iges")

    assert "total_volume" in geometry.incomplete.resolution_methods
    assert "missing_volume" in geometry.incomplete.resolution_methods


def test_an_unmeasurable_iges_weakens_the_capabilities_that_depend_on_it():
    document = import_cad_document(iges("open_surfaces"), "surfaces.iges")

    matrix = document.capability_matrix
    assert matrix[GEOMETRY] == REQUIRES_USER_INPUT
    assert matrix[MASS] == REQUIRES_USER_INPUT
    assert matrix[COSTING] == REQUIRES_USER_INPUT
    # Surface area and the preview are unaffected: both are real without a volume.
    assert matrix[SURFACE_AREA] == SUPPORTED
    assert matrix[VIEWER] == SUPPORTED


def test_the_solidity_analysis_reports_what_it_found():
    solid = analyse_iges_shape(read_iges_shape(iges("box_solid")))
    surfaces = analyse_iges_shape(read_iges_shape(iges("box_surfaces")))
    open_set = analyse_iges_shape(read_iges_shape(iges("open_surfaces")))

    assert solid.solid_count == 1 and solid.sewing_applied is False
    assert surfaces.solid_count == 1 and surfaces.sewing_applied is True
    assert surfaces.free_edge_count == 0
    assert open_set.is_solid is False and open_set.free_edge_count == 8


# --- IGES: units ------------------------------------------------------------


def test_an_iges_file_declares_its_unit_and_that_is_read():
    unit = detect_length_unit(iges("box_solid"))

    assert unit is not None
    assert unit.name == "millimetre"
    assert unit.flag == 2
    assert unit.millimetres_per_unit == 1.0


def test_a_declared_inch_file_is_converted_to_millimetres():
    """Not a round trip: the coordinates are unchanged and only the declaration
    differs, so a correct reader must scale them by 25.4."""
    data = iges_fixtures.declared_as_inches(iges("box_solid"))

    assert detect_length_unit(data).name == "inch"

    geometry = extract_geometry_from_bytes(data, "iges")
    assert geometry.bounding_box.x == pytest.approx(40.0 * 25.4)
    assert geometry.volume == pytest.approx(24000.0 * 25.4**3)
    assert geometry.detected_unit == "mm"


def test_an_undeclared_unit_is_refused_rather_than_assumed():
    """Measured behaviour of the reader, and the reason this check exists:
    handed a well-formed IGES file with no unit declaration, OpenCASCADE reads
    it as inches without complaint, turning a 40 mm part into a 1016 mm one.
    """
    data = iges_fixtures.without_unit_declaration(iges("box_solid"))

    assert detect_length_unit(data) is None

    with pytest.raises(ValueError, match="25.4 times too large"):
        extract_geometry_from_bytes(data, "iges")


@pytest.mark.parametrize("endpoint", ["/cad/geometry", "/cad/mesh"])
def test_an_undeclared_unit_refuses_the_endpoint_with_the_reason_not_a_number(endpoint):
    """A format that is readable in general, and a file that is not readable in
    particular: a 400 with the file's own explanation, and never a measurement
    that is 25.4 times too large."""
    data = iges_fixtures.without_unit_declaration(iges("box_solid"))

    response = post(endpoint, "nounit.iges", data)

    assert response.status_code == 400
    assert "25.4 times too large" in response.json()["detail"]


def test_an_undeclared_unit_is_reported_as_requiring_input_not_as_a_failure():
    data = iges_fixtures.without_unit_declaration(iges("box_solid"))

    document = import_cad_document(data, "nounit.iges", with_geometry=False)

    assert document.unit_source == UNIT_UNDECLARED
    assert document.units is None
    assert document.capability_matrix[GEOMETRY] == REQUIRES_USER_INPUT
    assert "iges_unit_undeclared" in [w.code for w in document.warnings]


def test_the_import_endpoint_describes_a_file_it_would_refuse_to_measure():
    """Describing is not measuring. `/cad/import` still answers 200 for a file
    that `/cad/geometry` refuses, because saying what is wrong with a file is
    the whole point of it."""
    data = iges_fixtures.without_unit_declaration(iges("box_solid"))

    response = post("/cad/import", "nounit.iges", data)

    assert response.status_code == 200
    body = response.json()
    assert body["geometry"] is None
    assert body["unit_source"] == UNIT_UNDECLARED
    matrix = {r["capability"]: r["support"] for r in body["capabilities"]}
    assert matrix[GEOMETRY] == REQUIRES_USER_INPUT


def test_the_unit_check_reads_a_file_that_names_its_unit_without_a_flag():
    """Flag 3 means "see the units name", and some writers omit the flag but
    still name the unit. A name we recognize is a real declaration."""
    data = iges_fixtures._rewrite_global_section(iges("box_solid"), ",2,2HMM,", ",3,2HMM,")

    unit = detect_length_unit(data)
    assert unit is not None and unit.name == "millimetre"


def test_a_unit_the_specification_does_not_define_is_not_invented():
    data = iges_fixtures._rewrite_global_section(iges("box_solid"), ",2,2HMM,", ",99,3HZZZ,")

    assert detect_length_unit(data) is None


@pytest.mark.parametrize(
    "source_format, expected_source",
    [("step", UNIT_FILE_DECLARED), ("iges", UNIT_FILE_DECLARED), ("stl", UNIT_NOT_CARRIED_BY_FORMAT)],
)
def test_every_format_says_where_its_unit_came_from(source_format, expected_source):
    """Requirement in its own right: no format may leave this unstated, and
    "the format has no unit field" is a different answer from "the file did not
    say"."""
    data = {
        "step": (plain_block(), "part.step"),
        "iges": (iges("box_solid"), "part.iges"),
        "stl": (stl_bytes(), "part.stl"),
    }[source_format]

    document = import_cad_document(data[0], data[1], with_geometry=False)

    assert document.unit_source == expected_source
    assert document.unit_note != ""


def test_stl_is_explicit_that_its_numbers_are_unverifiable():
    document = import_cad_document(stl_bytes(), "part.stl")

    assert document.units is None
    assert document.capability_matrix[MASS] == PARTIALLY_SUPPORTED
    assert "records no length unit" in document.capability(MASS).detail
    assert "the cost are wrong by that factor" in document.capability(COSTING).detail


# --- IGES: features ---------------------------------------------------------


def test_an_iges_solid_recognizes_the_same_features_as_the_step_of_the_same_part():
    """The strongest statement available about IGES: for a solid-mode export,
    recognition is not merely similar to STEP's, it is identical."""
    step_result = recognize_features_from_bytes(block_with_through_hole(), "part.step")
    iges_result = recognize_features_from_bytes(iges("block_with_hole_solid"), "part.iges")

    assert iges_result.feature_counts == step_result.feature_counts
    assert iges_result.solid_count == step_result.solid_count
    assert iges_result.face_count == step_result.face_count


def test_a_surface_only_iges_reports_its_missing_holes_rather_than_hiding_them():
    """Measured, and the reason the features capability stays partial for IGES.

    A surface-mode export rewrites cylinders as surfaces of revolution, so the
    hole recognizer cannot match them. Sewing restores the topology but cannot
    restore a cylinder's identity. The result would otherwise look exactly like
    a part with no holes in it.
    """
    result = recognize_features_from_bytes(iges("block_with_hole_surfaces"), "part.iges")

    # The part has a through hole. Recognition cannot see it, because the
    # export rewrote the cylinder as a surface of revolution — sewing the
    # surfaces back into a solid restores the topology but not the identity.
    assert result.feature_counts.get("hole", 0) == 0
    assert result.representation == "surface_model"
    assert "MISSING FROM THIS RESULT" in result.warnings[0]
    assert "surface of revolution" in result.warnings[0]


def test_a_surface_only_iges_that_will_not_even_close_says_that_as_well():
    result = recognize_features_from_bytes(iges("open_surfaces"), "part.iges")

    assert result.representation == "surface_model"
    assert any("do not close into a solid either" in warning for warning in result.warnings)


def test_the_two_iges_flavours_are_told_apart_without_parsing_anything():
    """From the Directory section, which lists every entity's type number.

    It has to work without a parse, because the upload panel reports what a
    file can do before any heavy work runs — and because the answer must not
    depend on whether geometry happened to be asked for.
    """
    from cad_costing_agent.cad_iges import SOLID_ENTITY, carries_a_solid, scan_entity_types

    assert carries_a_solid(iges("block_with_hole_solid")) is True
    assert carries_a_solid(iges("block_with_hole_surfaces")) is False

    solid_entities = scan_entity_types(iges("block_with_hole_solid"))
    surface_entities = scan_entity_types(iges("block_with_hole_surfaces"))
    assert SOLID_ENTITY in solid_entities
    assert SOLID_ENTITY not in surface_entities
    # 144 is a trimmed parametric surface: what a surface export writes instead.
    assert 144 in surface_entities


def test_the_features_claim_is_settled_before_any_geometry_is_parsed():
    solid = import_cad_document(iges("block_with_hole_solid"), "p.iges", with_geometry=False)
    surfaces = import_cad_document(iges("block_with_hole_surfaces"), "p.iges", with_geometry=False)

    assert solid.geometry is None and surfaces.geometry is None
    assert solid.capability_matrix[FEATURES] == SUPPORTED
    assert surfaces.capability_matrix[FEATURES] == PARTIALLY_SUPPORTED
    assert solid.representation == "brep"
    assert surfaces.representation == "surface_model"
    assert "iges_surface_export" in [w.code for w in surfaces.warnings]


def test_parsing_the_geometry_does_not_change_the_features_claim():
    """The same answer either way, which is what makes the upload panel honest."""
    for name in ("block_with_hole_solid", "block_with_hole_surfaces"):
        without = import_cad_document(iges(name), "p.iges", with_geometry=False)
        with_ = import_cad_document(iges(name), "p.iges")
        assert without.capability_matrix[FEATURES] == with_.capability_matrix[FEATURES]
        assert without.representation == with_.representation


def test_the_declared_entity_types_are_recorded_as_provenance():
    document = import_cad_document(iges("block_with_hole_solid"), "p.iges", with_geometry=False)

    assert document.metadata.entity_counts["entity_186"] > 0


def test_the_features_capability_distinguishes_the_two_iges_flavours():
    solid = import_cad_document(iges("block_with_hole_solid"), "part.iges")
    surfaces = import_cad_document(iges("block_with_hole_surfaces"), "part.iges")

    assert solid.capability_matrix[FEATURES] == SUPPORTED
    assert surfaces.capability_matrix[FEATURES] == PARTIALLY_SUPPORTED
    assert "spline surfaces" in surfaces.capability(FEATURES).detail
    # Both are measurable, so both are costable.
    assert solid.capability_matrix[COSTING] == SUPPORTED
    assert surfaces.capability_matrix[COSTING] == SUPPORTED


def test_iges_features_are_treated_as_exact_evidence_not_fitted_evidence():
    """An IGES face is a real surface, unlike a faceted STEP's polygons, so its
    features must not be capped the way fitted ones are."""
    result = recognize_features_from_bytes(iges("block_with_hole_solid"), "part.iges")

    assert {feature.source_type for feature in result.features} == {"brep"}


def test_the_features_endpoint_accepts_iges():
    response = post("/cad/features", "part.iges", iges("block_with_hole_solid"))

    assert response.status_code == 200
    assert response.json()["feature_counts"]["hole"] == 1


# --- IGES: viewer and costing ----------------------------------------------


def test_iges_produces_a_viewer_mesh():
    response = post("/cad/mesh", "part.iges", iges("block_with_hole_solid"))

    assert response.status_code == 200
    body = response.json()
    assert body["mesh"]["source_format"] == "iges"
    assert body["mesh"]["triangle_count"] > 0
    assert body["mesh"]["tessellation"]["method"] == "opencascade-brepmesh"
    assert body["geometry"]["volume"] == pytest.approx(iges_fixtures.BLOCK_WITH_HOLE_VOLUME)


def test_an_iges_surface_model_can_be_previewed_even_when_it_cannot_be_measured():
    """The existing separation, extended to a new format: a model that cannot
    be costed can still be looked at, and vice versa."""
    response = post("/cad/mesh", "surfaces.iges", iges("open_surfaces"))

    assert response.status_code == 200
    body = response.json()
    assert body["geometry"]["geometry_status"] == INCOMPLETE
    assert body["mesh"]["triangle_count"] > 0


def test_iges_geometry_matches_the_viewer_s_geometry_exactly():
    """One extractor, so the numbers beside the model are the costing numbers."""
    direct = post("/cad/geometry", "part.iges", iges("box_solid")).json()
    beside_the_mesh = post("/cad/mesh", "part.iges", iges("box_solid")).json()["geometry"]

    assert beside_the_mesh == direct


def test_an_iges_part_can_be_costed():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("part.iges", iges("box_solid"), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-IGES",
            "part_name": "IGES box",
            "quantity": "5",
            "rate_set_id": "poc-machine-shop",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["part_cost"]["currency"] == "INR"
    assert body["part_cost"]["total_part_cost"] > 0
    assert body["geometry"]["source_format"] == "iges"


def test_an_iges_part_and_the_step_of_the_same_shape_cost_the_same():
    """The clearest possible statement that the new format joined the existing
    pipeline rather than growing a parallel one."""
    form = {
        "material": "Steel",
        "source_unit": "mm",
        "part_id": "P-1",
        "part_name": "Block",
        "quantity": "3",
        "rate_set_id": "poc-machine-shop",
    }
    shape_step = to_step(iges_fixtures._block_with_through_hole())

    from_step = client.post(
        "/cost/cad-part",
        files={"file": ("p.step", shape_step, "application/octet-stream")},
        data=form,
    ).json()
    from_iges = client.post(
        "/cost/cad-part",
        files={"file": ("p.iges", iges("block_with_hole_solid"), "application/octet-stream")},
        data=form,
    ).json()

    assert from_iges["part_cost"]["total_part_cost"] == pytest.approx(
        from_step["part_cost"]["total_part_cost"]
    )
    assert from_iges["geometry"]["volume"] == pytest.approx(from_step["geometry"]["volume"])


def test_an_unmeasurable_iges_asks_for_the_volume_instead_of_costing_it():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("s.iges", iges("open_surfaces"), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Surfaces",
            "quantity": "1",
            "rate_set_id": "poc-machine-shop",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "needs_geometry_input"


def test_a_user_supplied_volume_completes_an_iges_surface_model():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("s.iges", iges("open_surfaces"), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Surfaces",
            "quantity": "1",
            "rate_set_id": "poc-machine-shop",
            "geometry_completion_method": "total_volume",
            "geometry_completion_volume_mm3": "24000",
        },
    )

    assert response.status_code == 200
    geometry = response.json()["geometry"]
    assert geometry["volume"] == pytest.approx(24000.0)
    assert geometry["volume_confidence"] == "user_supplied"
    assert geometry["volume_method"] == "user_supplied_total_volume"


# --- refusing what cannot be done ------------------------------------------


@pytest.mark.parametrize(
    "filename, source_format",
    [("part.prt", "creo_part"), ("part.asm", "creo_assembly"), ("part.drw", "creo_drawing")],
)
def test_a_native_creo_upload_is_identified_and_refused_with_a_reason(filename, source_format):
    document = import_cad_document(b"\x00\x01native bytes", filename)

    assert document.source_format == source_format
    assert document.geometry is None
    assert document.any_support is False
    assert document.representation == "not_parsed"
    assert document.unit_source == UNIT_UNDECLARED

    with pytest.raises(UnsupportedFormatError):
        require(document, GEOMETRY)


@pytest.mark.parametrize("filename", ["part.prt", "part.asm", "part.drw"])
def test_a_native_creo_upload_answers_415_and_not_a_pretend_success(filename):
    response = post("/cad/geometry", filename, b"\x00\x01native bytes")

    assert response.status_code == 415
    detail = response.json()["detail"]
    assert detail["error"] == "unsupported_cad_format"
    assert "licensed" in detail["message"]


@pytest.mark.parametrize("filename", ["part.prt", "part.asm", "part.drw"])
def test_the_import_endpoint_still_describes_a_format_it_cannot_read(filename):
    """Being told what a `.prt` file is, and why it is refused, is the point."""
    response = post("/cad/import", filename, b"\x00\x01native bytes")

    assert response.status_code == 200
    body = response.json()
    assert body["representation"] == "not_parsed"
    assert {r["support"] for r in body["capabilities"]} == {UNSUPPORTED}
    assert body["warnings"][0]["code"] == "native_creo_not_supported"


def test_a_drawing_is_never_treated_as_a_model():
    document = import_cad_document(b"\x00\x01drawing", "part.drw")

    assert document.capability_matrix[GEOMETRY] == UNSUPPORTED
    assert document.capability_matrix[DRAWINGS] == UNSUPPORTED
    assert "no solid geometry" in document.capability(DRAWINGS).detail


def test_an_stl_asked_for_features_is_refused_with_the_status_it_always_had():
    """A readable format that cannot do one thing is a 400, not a 415: the file
    is fine, and a triangle mesh simply carries no features."""
    response = post("/cad/features", "part.stl", stl_bytes())

    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)
    assert "triangle mesh" in response.json()["detail"]


def test_a_wireframe_step_reports_nothing_measurable():
    step = plain_block()
    # Strip every face entity, leaving points behind.
    wireframe = step.replace(b"ADVANCED_FACE", b"NOT_A_FACE")

    document = import_cad_document(wireframe, "wire.step", with_geometry=False)

    assert document.representation == "wireframe"
    assert document.capability_matrix[GEOMETRY] == UNSUPPORTED
    assert document.capability_matrix[COSTING] == UNSUPPORTED
    assert "no surfaces" in document.capability(GEOMETRY).detail


@pytest.mark.parametrize(
    "filename, data, expected",
    [
        ("part.iges", b"whatever", "not valid IGES data"),
        ("part.iges", b"", "empty"),
        ("part.step", b"this is not STEP", "not valid"),
        ("part.stl", b"", "empty"),
    ],
)
def test_bad_input_fails_with_a_readable_message(filename, data, expected):
    with pytest.raises(ValueError, match=expected):
        import_cad_document(data, filename)


def test_an_iges_named_file_that_is_not_iges_says_so_rather_than_blaming_units():
    """Two different problems, told apart before the parse: "this is not an
    IGES file" is not "this IGES file does not say what unit it is in"."""
    assert looks_like_iges(b"whatever") is False

    document = import_cad_document(b"whatever", "x.iges", with_geometry=False)
    assert document.warnings[0].code == "iges_content_not_recognized"

    response = post("/cad/geometry", "x.iges", b"whatever")
    assert response.status_code == 400
    assert "not valid IGES data" in response.json()["detail"]


# --- existing formats are unchanged ----------------------------------------


@pytest.mark.parametrize(
    "filename, data",
    [
        ("part.step", plain_block()),
        ("part.stp", block_with_through_hole()),
        ("part.stl", stl_bytes()),
    ],
)
def test_routing_through_the_import_layer_changes_no_measurement(filename, data):
    """The regression that matters most: the new layer must be a router, not a
    second opinion."""
    directly = extract_geometry_from_bytes(data, filename.rsplit(".", 1)[1])
    imported = import_cad_document(data, filename).geometry

    assert imported.model_dump() == directly.model_dump()


def test_the_step_endpoints_answer_exactly_as_before():
    geometry = post("/cad/geometry", "part.step", block_with_through_hole())
    features = post("/cad/features", "part.step", block_with_through_hole())

    assert geometry.status_code == 200
    assert geometry.json()["source_format"] == "step"
    assert geometry.json()["geometry_status"] == COMPLETE
    assert features.status_code == 200
    assert features.json()["feature_counts"]["hole"] == 1


def test_the_viewer_measures_the_file_once_per_request():
    """The import layer hands its geometry to the viewer rather than letting it
    parse the same bytes again — the layer must not cost an extra pass."""
    import cad_costing_agent.cad_mesh as mesh_module

    calls = []
    original = mesh_module.extract_geometry_from_bytes

    def counted(data, file_format):
        calls.append(file_format)
        return original(data, file_format)

    mesh_module.extract_geometry_from_bytes = counted
    try:
        response = post("/cad/mesh", "part.step", block_with_through_hole())
    finally:
        mesh_module.extract_geometry_from_bytes = original

    assert response.status_code == 200
    assert calls == []  # the geometry came from the import, not a second parse


# --- limits -----------------------------------------------------------------


def test_a_new_format_obeys_the_upload_limit(monkeypatch):
    import cad_costing_agent.main as main_module

    monkeypatch.setattr(main_module, "MAX_UPLOAD_BYTES", 100)

    response = post("/cad/import", "part.iges", iges("box_solid"))

    assert response.status_code == 400
    assert "above the" in response.json()["detail"]


def test_an_iges_model_over_the_feature_budget_is_declined_without_blocking_costing(
    monkeypatch,
):
    """The ordering the budgets exist to protect: a model too complex to
    analyse is still costable."""
    monkeypatch.setattr("cad_costing_agent.features.MAX_FACES", 2)

    features = post("/cad/features", "part.iges", iges("block_with_hole_solid"))
    geometry = post("/cad/geometry", "part.iges", iges("block_with_hole_solid"))

    assert features.status_code == 400
    assert "feature recognition will analyse" in features.json()["detail"]
    assert geometry.status_code == 200


def test_the_viewer_budget_degrades_only_the_preview(monkeypatch):
    monkeypatch.setattr(limits, "MAX_VIEWER_TRIANGLES", 8)
    import cad_costing_agent.cad_mesh as mesh_module

    monkeypatch.setattr(mesh_module, "MAX_TRIANGLES", 8)

    response = post("/cad/mesh", "part.iges", iges("block_with_hole_solid"))

    assert response.status_code == 200
    body = response.json()
    assert body["geometry"]["volume"] == pytest.approx(iges_fixtures.BLOCK_WITH_HOLE_VOLUME)
    assert body["mesh"]["display_mesh_simplified"] is True


# --- determinism ------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, name",
    [
        ("part.iges", "box_solid"),
        ("part.iges", "box_surfaces"),
        ("part.iges", "open_surfaces"),
        ("part.iges", "block_with_hole_solid"),
    ],
)
def test_importing_the_same_iges_twice_gives_the_same_answer(filename, name):
    """Timings are the only fields that may vary, and they are excluded."""
    excluded = {"detect_ms", "parse_ms"}

    first = import_cad_document(iges(name), filename).model_dump(exclude=excluded)
    second = import_cad_document(iges(name), filename).model_dump(exclude=excluded)

    assert first == second


def test_iges_feature_recognition_is_deterministic():
    data = iges("block_with_hole_solid")

    first = recognize_features_from_bytes(data, "p.iges").model_dump(exclude={"elapsed_ms"})
    second = recognize_features_from_bytes(data, "p.iges").model_dump(exclude={"elapsed_ms"})

    assert first == second


def test_the_format_catalog_is_stable():
    assert [d.model_dump() for d in format_catalog()] == [
        d.model_dump() for d in format_catalog()
    ]

# --- structure: the boundaries this layer exists to create ------------------


def code_without_prose(module) -> str:
    """A module's code with its docstrings and comments stripped.

    The prose in this codebase explains at length what it deliberately does
    *not* do, so a naive grep for a forbidden name matches the paragraph
    promising the name is absent.
    """
    import ast
    import io
    import tokenize
    from pathlib import Path as _Path

    source = _Path(module.__file__).read_text(encoding="utf-8")

    docstrings = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add((first.lineno, first.col_offset))

    kept = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and token.start in docstrings:
            continue
        kept.append(token.string)
    return "\n".join(kept)


def test_no_endpoint_parses_a_file_extension():
    """The single reason this layer exists.

    Before it, three modules and the upload UI each decided for themselves what
    an extension meant. Detection now happens in exactly one place, so an
    endpoint that read a suffix would be a second opinion able to disagree.
    """
    import cad_costing_agent.main as main_module

    code = code_without_prose(main_module)

    assert "suffix" not in code
    for extension in ('"step"', '"stp"', '"stl"', '"iges"', '"igs"'):
        assert extension not in code, f"main.py names {extension} instead of asking the registry"


def test_the_adapters_implement_no_geometry_of_their_own():
    """An adapter routes and declares. It must not become a second measurer,
    or two formats could drift into disagreeing about the same solid."""
    from cad_costing_agent.cad_import import adapters as adapters_module

    code = code_without_prose(adapters_module)

    for forbidden in ("BRepGProp", "GProp_GProps", "BRepBndLib", "trimesh", "TopExp_Explorer"):
        assert forbidden not in code


def test_the_import_layer_touches_no_costing_arithmetic():
    """The branch separation the whole codebase is built around: import feeds
    costing, and knows nothing about it."""
    from cad_costing_agent.cad_import import adapters as adapters_module
    from cad_costing_agent.cad_import import model as model_module
    import cad_costing_agent.cad_import as registry_module

    for module in (registry_module, adapters_module, model_module):
        code = code_without_prose(module)
        # Module-qualified, because the package itself is called
        # cad_costing_agent and the capability constant is called COSTING.
        for forbidden in (
            "cad_costing_agent.costing",
            "cad_costing_agent.cad_part_cost",
            "cad_costing_agent.operation_costing",
            "cad_costing_agent.part_cost",
            "cad_costing_agent.rate_sets",
            "cad_costing_agent.money",
            "cad_costing_agent.estimates",
            "cad_costing_agent.additional_costs",
            "RateSet",
        ):
            assert forbidden not in code, f"{module.__name__} reaches into {forbidden}"


def test_costing_does_not_import_the_import_layer_either():
    """Kept one-directional, like every other branch in this codebase."""
    import cad_costing_agent.cad_part_cost as cad_part_cost
    import cad_costing_agent.costing as costing
    import cad_costing_agent.operation_costing as operation_costing

    for module in (costing, operation_costing, cad_part_cost):
        assert "cad_import" not in code_without_prose(module)


def test_the_iges_reader_is_the_only_place_iges_is_parsed():
    """The same rule `read_step_shape` follows, for the same reason: one
    definition of a readable IGES file and one set of failure messages."""
    import cad_costing_agent.cad_geometry as geometry_module
    import cad_costing_agent.cad_mesh as mesh_module
    import cad_costing_agent.features as features_module

    for module in (geometry_module, mesh_module, features_module):
        code = code_without_prose(module)
        assert "IGESControl_Reader" not in code, f"{module.__name__} opens IGES itself"


def test_the_iges_unit_guard_cannot_be_bypassed():
    """Every entry point that reads an IGES file has to go through the same two
    checks, in the same order — the undeclared-unit case is a 25.4x error, and
    one path that skipped the check would be enough to ship it."""
    import cad_costing_agent.cad_geometry as geometry_module
    import cad_costing_agent.features.shape_loading as features_module

    # The feature layer reads IGES in one module, `features.shape_loading`;
    # `test_shape_loading` holds that no other feature module reads a file.
    for module in (geometry_module, features_module):
        code = code_without_prose(module)
        assert "ensure_readable" in code
