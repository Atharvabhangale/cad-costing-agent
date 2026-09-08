"""CAD-driven part cost estimate: geometry -> mass -> existing part costing.

This module duplicates no costing formulas. It composes three things that
already exist and are independently tested:

    cad_geometry.extract_geometry_from_bytes(...)   (done by the caller)
    cad_mass.calculate_mass_from_geometry(...)       -> mass_kg for one part
    part_cost.calculate_part_cost(...)               -> existing material +
                                                         operation costing

Quantity semantics: the CAD-derived mass is for a SINGLE part. It is passed
into `Part.mass_kg` unscaled; `Part.quantity` is applied only once, inside
the existing `calculate_material_cost`, exactly as it is for a manually
supplied mass via `/cost/part`. This module never multiplies mass by
quantity itself — doing so would double-count it.
"""

from pydantic import BaseModel

from cad_costing_agent.additional_costs import AdditionalCostInputs
from cad_costing_agent.cad_geometry import GeometryProperties
from cad_costing_agent.cad_mass import GeometryMassResult, calculate_mass_from_geometry
from cad_costing_agent.operation import ManufacturingOperation
from cad_costing_agent.part import Part
from cad_costing_agent.rate_sets import OperationRequirement, RateSet, resolve_material
from cad_costing_agent.part_cost import (
    PartCostEstimateRequest,
    PartCostEstimateResult,
    calculate_part_cost,
)


class CadPartCostEstimateResult(BaseModel):
    estimate_id: str | None = None
    geometry: GeometryProperties
    mass: GeometryMassResult
    part_cost: PartCostEstimateResult


def calculate_cad_part_cost(
    geometry: GeometryProperties,
    material_name: str,
    source_unit: str,
    part_id: str,
    part_name: str,
    quantity: int,
    operations: list[ManufacturingOperation] | None = None,
    additional_costs: AdditionalCostInputs | None = None,
    operation_requirements: list[OperationRequirement] | None = None,
    rate_set_id: str | None = None,
    rate_set_version: str | None = None,
    rate_set: RateSet | None = None,
) -> CadPartCostEstimateResult:
    # When a rate set is in play it is the source of material data, density
    # included. Resolving here means a profile's own materials can actually be
    # costed, and that a profile which redefines a material's density is not
    # quietly overruled by the built-in catalog.
    material = resolve_material(rate_set, material_name) if rate_set is not None else None
    mass_result = calculate_mass_from_geometry(
        geometry, material_name, source_unit, material=material
    )

    part = Part(
        part_id=part_id,
        part_name=part_name,
        material=material_name,
        mass_kg=mass_result.mass_kg,
        quantity=quantity,
    )

    part_cost = calculate_part_cost(
        PartCostEstimateRequest(
            part=part,
            operations=operations or [],
            operation_requirements=operation_requirements or [],
            additional_costs=additional_costs,
            rate_set_id=rate_set_id,
            rate_set_version=rate_set_version,
        ),
        rate_set=rate_set,
    )

    return CadPartCostEstimateResult(geometry=geometry, mass=mass_result, part_cost=part_cost)
