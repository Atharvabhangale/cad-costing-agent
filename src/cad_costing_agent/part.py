from pydantic import BaseModel, Field


class Part(BaseModel):
    part_id: str = Field(min_length=1)
    part_name: str = Field(min_length=1)
    material: str = Field(min_length=1, description="Material name, e.g. 'Steel' or 'Aluminium'.")
    mass_kg: float = Field(gt=0, description="Part mass in kilograms.")
    quantity: int = Field(gt=0, default=1, description="Number of parts.")
