"""Illustrative material reference data.

All cost figures in this module are ILLUSTRATIVE POC VALUES used to exercise
the costing calculation. They are not real market or customer pricing and
must not be used for any actual quotation.
"""

from pydantic import BaseModel, Field


class Material(BaseModel):
    name: str
    density_kg_m3: float = Field(gt=0, description="Density, for future volume-based calculations.")
    cost_per_kg: float = Field(gt=0, description="ILLUSTRATIVE POC VALUE, not real market pricing.")
    illustrative: bool = True


MATERIALS: dict[str, Material] = {
    "steel": Material(name="Steel", density_kg_m3=7850, cost_per_kg=0.80, illustrative=True),
    "aluminium": Material(name="Aluminium", density_kg_m3=2700, cost_per_kg=2.20, illustrative=True),
}


def get_material(name: str) -> Material:
    material = MATERIALS.get(name.strip().lower())
    if material is None:
        known = ", ".join(m.name for m in MATERIALS.values())
        raise ValueError(f"Unknown material '{name}'. Known materials: {known}")
    return material
