import pytest

from cad_costing_agent.cad_geometry import BoundingBoxDimensions, GeometryProperties
from cad_costing_agent.cad_mass import (
    LengthUnit,
    calculate_mass_from_geometry,
    cubic_conversion_factor,
)
from cad_costing_agent.materials import MATERIALS

STEEL_DENSITY_KG_M3 = MATERIALS["steel"].density_kg_m3


def _geometry(volume: float, x: float = 10.0, y: float = 20.0, z: float = 30.0) -> GeometryProperties:
    return GeometryProperties(
        source_format="stl",
        volume=volume,
        surface_area=2 * (x * y + x * z + y * z),
        bounding_box=BoundingBoxDimensions(x=x, y=y, z=z),
    )


def test_mm3_to_m3_conversion_factor():
    assert cubic_conversion_factor(LengthUnit.MM) == pytest.approx(0.001**3)


def test_cm3_to_m3_conversion_factor():
    assert cubic_conversion_factor(LengthUnit.CM) == pytest.approx(0.01**3)


def test_m3_to_m3_conversion_factor_is_identity():
    assert cubic_conversion_factor(LengthUnit.M) == pytest.approx(1.0)


def test_inch3_to_m3_conversion_factor():
    assert cubic_conversion_factor(LengthUnit.INCH) == pytest.approx(0.0254**3)


def test_unsupported_unit_rejected():
    geometry = _geometry(6000.0)

    with pytest.raises(ValueError, match="Unsupported length unit"):
        calculate_mass_from_geometry(geometry, "Steel", "furlong")


def test_zero_volume_rejected():
    geometry = _geometry(0.0)

    with pytest.raises(ValueError, match="volume must be positive"):
        calculate_mass_from_geometry(geometry, "Steel", "mm")


def test_negative_volume_rejected():
    geometry = _geometry(-1.0)

    with pytest.raises(ValueError, match="volume must be positive"):
        calculate_mass_from_geometry(geometry, "Steel", "mm")


def test_unknown_material_rejected():
    geometry = _geometry(6000.0)

    with pytest.raises(ValueError, match="Unknown material"):
        calculate_mass_from_geometry(geometry, "Unobtainium", "mm")


def test_mass_for_known_cube_and_steel_density():
    # 10 x 20 x 30 mm cube -> 6000 mm^3 -> 0.000006 m^3
    geometry = _geometry(6000.0, 10.0, 20.0, 30.0)

    result = calculate_mass_from_geometry(geometry, "Steel", "mm")

    expected_volume_m3 = 6000.0 * (0.001**3)
    expected_mass_kg = expected_volume_m3 * STEEL_DENSITY_KG_M3

    assert result.volume_m3 == pytest.approx(expected_volume_m3)
    assert result.mass_kg == pytest.approx(expected_mass_kg)
    assert result.mass_kg == pytest.approx(0.0471, abs=1e-4)
    assert result.material.name == "Steel"
    assert result.is_illustrative_material is True
    assert result.source_unit == LengthUnit.MM


def test_mass_scales_correctly_across_units_for_same_physical_size():
    # Same physical cube expressed in mm vs cm vs m must give the same mass.
    mm_geometry = _geometry(6000.0)  # 10x20x30 mm -> mm^3
    cm_geometry = _geometry(6.0)  # 1x2x3 cm -> cm^3
    m_geometry = _geometry(0.000006)  # 0.01x0.02x0.03 m -> m^3

    mm_result = calculate_mass_from_geometry(mm_geometry, "Steel", "mm")
    cm_result = calculate_mass_from_geometry(cm_geometry, "Steel", "cm")
    m_result = calculate_mass_from_geometry(m_geometry, "Steel", "m")

    assert mm_result.mass_kg == pytest.approx(cm_result.mass_kg)
    assert mm_result.mass_kg == pytest.approx(m_result.mass_kg)
