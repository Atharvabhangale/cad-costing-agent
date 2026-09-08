import pytest

from cad_costing_agent.cad_geometry import (
    STEP_NORMALIZED_UNIT,
    extract_geometry_from_bytes,
    extract_geometry_from_file,
)

# A 10 x 20 x 30 box: volume 6000, surface area 2*(200 + 300 + 600) = 2200.
BOX_X, BOX_Y, BOX_Z = 10.0, 20.0, 30.0
EXPECTED_VOLUME = BOX_X * BOX_Y * BOX_Z
EXPECTED_SURFACE_AREA = 2 * (BOX_X * BOX_Y + BOX_X * BOX_Z + BOX_Y * BOX_Z)


def test_fixture_is_genuine_step_text(step_box_bytes):
    """Guard that the fixture really is an ISO 10303-21 file being parsed."""
    data = step_box_bytes()

    assert data.startswith(b"ISO-10303-21;")
    assert b"FILE_SCHEMA" in data
    assert data.rstrip().endswith(b"END-ISO-10303-21;")


def test_step_volume_surface_area_and_bounding_box(step_box_bytes):
    data = step_box_bytes(BOX_X, BOX_Y, BOX_Z)

    result = extract_geometry_from_bytes(data, "step")

    assert result.source_format == "step"
    assert result.volume == pytest.approx(EXPECTED_VOLUME)
    assert result.surface_area == pytest.approx(EXPECTED_SURFACE_AREA)
    assert result.bounding_box.x == pytest.approx(BOX_X)
    assert result.bounding_box.y == pytest.approx(BOX_Y)
    assert result.bounding_box.z == pytest.approx(BOX_Z)


def test_step_reports_detected_unit_as_mm(step_box_bytes):
    result = extract_geometry_from_bytes(step_box_bytes(), "step")

    assert result.detected_unit == STEP_NORMALIZED_UNIT == "mm"
    assert "millimetres" in result.units_note


def test_stp_extension_is_accepted_and_canonicalized(step_box_bytes):
    data = step_box_bytes(BOX_X, BOX_Y, BOX_Z)

    result = extract_geometry_from_bytes(data, ".stp")

    assert result.source_format == "step"
    assert result.volume == pytest.approx(EXPECTED_VOLUME)


def test_step_extraction_from_file(step_box_bytes, tmp_path):
    step_path = tmp_path / "box.step"
    step_path.write_bytes(step_box_bytes(BOX_X, BOX_Y, BOX_Z))

    result = extract_geometry_from_file(step_path)

    assert result.source_format == "step"
    assert result.volume == pytest.approx(EXPECTED_VOLUME)
    assert result.detected_unit == "mm"


def test_step_extraction_is_deterministic(step_box_bytes):
    data = step_box_bytes(BOX_X, BOX_Y, BOX_Z)

    first = extract_geometry_from_bytes(data, "step")
    second = extract_geometry_from_bytes(data, "step")

    assert first.model_dump() == second.model_dump()


def test_inch_authored_step_is_converted_to_millimetres(step_box_bytes):
    """A STEP file authored in inches must come back in mm, not raw inches.

    The fixture writes a 25.4 mm cube with the file unit set to INCH, i.e. a
    genuine 1-inch cube. If the reader ignored the file's declared unit the
    bounding box would come back as 1.0 rather than 25.4.
    """
    data = step_box_bytes(25.4, 25.4, 25.4, unit="INCH")

    result = extract_geometry_from_bytes(data, "step")

    assert result.bounding_box.x == pytest.approx(25.4)
    assert result.bounding_box.y == pytest.approx(25.4)
    assert result.bounding_box.z == pytest.approx(25.4)
    assert result.volume == pytest.approx(25.4**3)
    assert result.detected_unit == "mm"


def test_empty_step_file_rejected():
    with pytest.raises(ValueError, match="empty"):
        extract_geometry_from_bytes(b"", "step")


def test_invalid_step_content_rejected():
    with pytest.raises(ValueError, match="not valid STEP"):
        extract_geometry_from_bytes(b"this is definitely not a step file", "step")


def test_structurally_valid_but_empty_step_rejected():
    """Parses as STEP but carries no geometry, so there is nothing to cost."""
    empty_step = (
        b"ISO-10303-21;\n"
        b"HEADER;\n"
        b"FILE_DESCRIPTION((''),'');\n"
        b"FILE_NAME('','',(''),(''),'','','');\n"
        b"FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\n"
        b"ENDSEC;\n"
        b"DATA;\n"
        b"ENDSEC;\n"
        b"END-ISO-10303-21;\n"
    )

    with pytest.raises(ValueError, match="does not contain any transferable geometry"):
        extract_geometry_from_bytes(empty_step, "step")


def test_unsupported_format_still_rejected(step_box_bytes):
    with pytest.raises(ValueError, match="Unsupported CAD file format"):
        extract_geometry_from_bytes(step_box_bytes(), "prt")


def test_step_bytes_are_not_parseable_as_iges(step_box_bytes):
    """IGES gained a reader in Step 23, but it is a *different* reader.

    STEP content handed to it is refused, rather than being read because both
    formats happen to describe surfaces.
    """
    with pytest.raises(ValueError, match="not valid IGES data"):
        extract_geometry_from_bytes(step_box_bytes(), "iges")


def test_step_bytes_are_not_parseable_as_stl(step_box_bytes):
    """Format is driven by the declared extension, not sniffed from content."""
    with pytest.raises(ValueError):
        extract_geometry_from_bytes(step_box_bytes(), "stl")
