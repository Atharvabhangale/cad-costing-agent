"""Deterministic mass calculation from CAD geometry volume + material density.

This is the single place where CAD geometry and material data meet. It is
kept separate from both:

- `cad_geometry.py`, which stays purely geometric and knows nothing about
  materials or costing;
- `costing.py`/`part_cost.py`, which stay purely about cost and know nothing
  about CAD geometry or unit conversion.

The caller always states which length unit the geometry is in, and volume
conversion is CUBIC, not linear: converting a volume from one length unit to
meters requires cubing the linear meters-per-unit factor, e.g.
`mm3_to_m3 = 0.001 ** 3`, not `0.001`.

Formats differ in whether the unit is knowable from the file itself:

- STL carries no unit metadata, so `GeometryProperties.detected_unit` is
  `None` and the caller's `source_unit` is taken at face value.
- STEP carries unit metadata that the reader has already honoured, converting
  the geometry to millimetres, so `detected_unit` is `"mm"`. A `source_unit`
  that contradicts it is rejected rather than silently applied — passing
  `inch` for an already-millimetre geometry would otherwise scale the mass by
  25.4**3.

This module calculates mass only. It does not calculate cost.
"""

from enum import Enum

from pydantic import BaseModel

from cad_costing_agent.cad_geometry import GeometryProperties
from cad_costing_agent.materials import Material, get_material

MASS_FORMULA = "mass_kg = (source_volume * (meters_per_unit ** 3)) * material.density_kg_m3"


class LengthUnit(str, Enum):
    """Length unit the source CAD file was modeled in."""

    MM = "mm"
    CM = "cm"
    M = "m"
    INCH = "inch"


_METERS_PER_UNIT: dict[LengthUnit, float] = {
    LengthUnit.MM: 0.001,
    LengthUnit.CM: 0.01,
    LengthUnit.M: 1.0,
    LengthUnit.INCH: 0.0254,
}


def _resolve_unit(source_unit: str | LengthUnit) -> LengthUnit:
    if isinstance(source_unit, LengthUnit):
        return source_unit
    try:
        return LengthUnit(source_unit.strip().lower())
    except ValueError as exc:
        supported = ", ".join(unit.value for unit in LengthUnit)
        raise ValueError(f"Unsupported length unit '{source_unit}'. Supported units: {supported}") from exc


def cubic_conversion_factor(source_unit: str | LengthUnit) -> float:
    """Volume conversion factor to m^3: cubes the linear meters-per-unit factor.

    e.g. mm -> m3 factor is (0.001) ** 3, cm -> m3 factor is (0.01) ** 3.
    """
    unit = _resolve_unit(source_unit)
    return _METERS_PER_UNIT[unit] ** 3


class GeometryMassResult(BaseModel):
    source_volume: float
    source_unit: LengthUnit
    detected_unit: str | None = None
    cubic_conversion_factor_to_m3: float
    volume_m3: float
    material: Material
    mass_kg: float
    formula: str
    is_illustrative_material: bool


def calculate_mass_from_geometry(
    geometry: GeometryProperties,
    material_name: str,
    source_unit: str | LengthUnit,
    material: Material | None = None,
) -> GeometryMassResult:
    """Convert extracted geometry volume + a material's density into a mass.

    The returned mass is for a SINGLE part (one instance of the geometry),
    not scaled by any production quantity — quantity scaling happens later,
    in the existing material costing calculation.

    `material` lets the caller supply an already-resolved material, which is
    how a costing profile's own density reaches this calculation: the API
    layer resolves the material from the selected rate set and injects it,
    the same way it injects a resolved `RateSet` into the part-cost
    calculator. Without it, density falls back to the built-in catalog, which
    is correct only when no rate set was selected — a profile that declares
    its own density must not have that density silently ignored.

    The formula is unchanged either way; only where the density comes from
    differs.
    """
    if geometry.volume <= 0:
        raise ValueError(f"Geometry volume must be positive, got {geometry.volume}.")

    unit = _resolve_unit(source_unit)

    # Some formats (STEP) carry unit metadata that the reader has already
    # honoured, so the geometry is known to be in a specific unit. Reject a
    # conflicting caller-supplied unit rather than silently mis-scaling the
    # mass by the cube of the discrepancy.
    if geometry.detected_unit is not None and unit.value != geometry.detected_unit:
        raise ValueError(
            f"Source unit '{unit.value}' conflicts with the unit detected from the CAD file "
            f"('{geometry.detected_unit}'). {geometry.source_format.upper()} geometry is already "
            f"normalized to '{geometry.detected_unit}', so pass source_unit='{geometry.detected_unit}'."
        )

    if material is None:
        material = get_material(material_name)

    factor = cubic_conversion_factor(unit)
    volume_m3 = geometry.volume * factor
    mass_kg = volume_m3 * material.density_kg_m3

    return GeometryMassResult(
        source_volume=geometry.volume,
        source_unit=unit,
        detected_unit=geometry.detected_unit,
        cubic_conversion_factor_to_m3=factor,
        volume_m3=volume_m3,
        material=material,
        mass_kg=mass_kg,
        formula=MASS_FORMULA,
        is_illustrative_material=material.illustrative,
    )
