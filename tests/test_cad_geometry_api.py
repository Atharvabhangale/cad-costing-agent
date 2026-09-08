import trimesh
import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.main import app

client = TestClient(app)


def _cube_stl_bytes(x: float = 10.0, y: float = 20.0, z: float = 30.0) -> bytes:
    box = trimesh.creation.box(extents=(x, y, z))
    return box.export(file_type="stl")


def test_cad_geometry_endpoint_returns_properties():
    data = _cube_stl_bytes(10.0, 20.0, 30.0)

    response = client.post(
        "/cad/geometry",
        files={"file": ("cube.stl", data, "application/octet-stream")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source_format"] == "stl"
    assert body["volume"] == pytest.approx(6000.0)
    assert body["surface_area"] == pytest.approx(2200.0)
    assert body["bounding_box"] == pytest.approx({"x": 10.0, "y": 20.0, "z": 30.0})


def test_cad_geometry_endpoint_unsupported_format_rejected():
    """An extension this application does not read, holding content it cannot identify."""
    response = client.post(
        "/cad/geometry",
        files={"file": ("cube.sldprt", b"\x00\x01not any CAD format", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert "Unsupported CAD file format" in response.json()["detail"]


def test_an_unknown_extension_holding_known_content_is_read_from_the_content():
    """The bytes decide. An unrecognized extension is not a reason to refuse
    a file whose content is unambiguously a format this reads."""
    response = client.post(
        "/cad/geometry",
        files={"file": ("cube.sldprt", _cube_stl_bytes(), "application/octet-stream")},
    )

    assert response.status_code == 200
    assert response.json()["source_format"] == "stl"


def test_a_recognized_but_unparseable_format_answers_415_with_its_reason():
    """A Creo part is not a malformed file, and is not reported as one."""
    response = client.post(
        "/cad/geometry",
        files={"file": ("bracket.prt", b"\x00\x01native creo bytes", "application/octet-stream")},
    )

    assert response.status_code == 415
    detail = response.json()["detail"]
    assert detail["source_format"] == "creo_part"
    assert "licensed" in detail["message"]


def test_a_mislabelled_file_is_read_as_what_it_actually_is():
    """STL content named `.iges` is read as STL, and the mismatch is reported."""
    response = client.post(
        "/cad/geometry",
        files={"file": ("cube.iges", _cube_stl_bytes(), "application/octet-stream")},
    )

    assert response.status_code == 200
    assert response.json()["source_format"] == "stl"


def test_cad_geometry_endpoint_empty_file_rejected():
    response = client.post(
        "/cad/geometry",
        files={"file": ("cube.stl", b"", "application/octet-stream")},
    )

    assert response.status_code == 400
