import trimesh
import pytest

from cad_costing_agent.cad_geometry import extract_geometry_from_bytes, extract_geometry_from_file


def _cube_stl_bytes(x: float = 10.0, y: float = 20.0, z: float = 30.0) -> bytes:
    box = trimesh.creation.box(extents=(x, y, z))
    return box.export(file_type="stl")


def test_extract_volume_surface_area_and_bounding_box_from_bytes():
    data = _cube_stl_bytes(10.0, 20.0, 30.0)

    result = extract_geometry_from_bytes(data, "stl")

    assert result.source_format == "stl"
    assert result.volume == pytest.approx(10.0 * 20.0 * 30.0)
    assert result.surface_area == pytest.approx(2 * (10 * 20 + 10 * 30 + 20 * 30))
    assert result.bounding_box.x == pytest.approx(10.0)
    assert result.bounding_box.y == pytest.approx(20.0)
    assert result.bounding_box.z == pytest.approx(30.0)


def test_extract_geometry_from_file(tmp_path):
    stl_path = tmp_path / "cube.stl"
    stl_path.write_bytes(_cube_stl_bytes(5.0, 5.0, 5.0))

    result = extract_geometry_from_file(stl_path)

    assert result.volume == pytest.approx(5.0**3)
    assert result.bounding_box.x == pytest.approx(5.0)
    assert result.bounding_box.y == pytest.approx(5.0)
    assert result.bounding_box.z == pytest.approx(5.0)


def test_unsupported_format_rejected():
    # STL, STEP and IGES are supported; a Creo part is recognized but has no
    # parser, and anything else is not a format this reads at all.
    data = _cube_stl_bytes()

    with pytest.raises(ValueError, match="Unsupported CAD file format"):
        extract_geometry_from_bytes(data, "prt")

    with pytest.raises(ValueError, match="Unsupported CAD file format"):
        extract_geometry_from_bytes(data, "sldprt")


def test_a_supported_extension_is_still_parsed_as_that_format():
    """An extension only chooses the reader; it cannot make bad content readable.

    STL bytes named `.iges` reach the IGES reader and are refused by it. The
    import layer resolves this mismatch from the content before it gets here —
    this is the lower-level guarantee that the declared format is the one used.
    """
    with pytest.raises(ValueError, match="not valid IGES data"):
        extract_geometry_from_bytes(_cube_stl_bytes(), "iges")


def test_missing_file_raises_file_not_found(tmp_path):
    missing_path = tmp_path / "does_not_exist.stl"

    with pytest.raises(FileNotFoundError):
        extract_geometry_from_file(missing_path)


def test_empty_file_rejected():
    with pytest.raises(ValueError, match="empty"):
        extract_geometry_from_bytes(b"", "stl")


def test_invalid_content_rejected():
    with pytest.raises(ValueError):
        extract_geometry_from_bytes(b"this is not a valid stl file", "stl")
