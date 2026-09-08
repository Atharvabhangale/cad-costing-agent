"""Deterministic material cost calculation.

No AI/LLM involved: material_cost = mass_kg * material.cost_per_kg,
total_material_cost = material_cost * quantity.
"""

from pydantic import BaseModel

from cad_costing_agent.materials import Material, get_material
from cad_costing_agent.money import Money
from cad_costing_agent.part import Part


class MaterialCostResult(BaseModel):
    part_id: str
    part_name: str
    material: Material
    mass_kg: float
    quantity: int
    material_cost_per_kg: float
    material_cost: Money
    total_material_cost: Money
    formula: str
    is_illustrative_rate: bool


def calculate_material_cost(part: Part, material: Material | None = None) -> MaterialCostResult:
    """Cost the part's material.

    `material` may be supplied by a caller that has already resolved it — a
    rate set, for instance. When it is omitted the built-in material catalog is
    consulted, which is the original and still-default behaviour.
    """
    if material is None:
        material = get_material(part.material)

    material_cost = part.mass_kg * material.cost_per_kg
    total_material_cost = material_cost * part.quantity

    return MaterialCostResult(
        part_id=part.part_id,
        part_name=part.part_name,
        material=material,
        mass_kg=part.mass_kg,
        quantity=part.quantity,
        material_cost_per_kg=material.cost_per_kg,
        material_cost=material_cost,
        total_material_cost=total_material_cost,
        formula="total_material_cost = mass_kg * material_cost_per_kg * quantity",
        is_illustrative_rate=material.illustrative,
    )
