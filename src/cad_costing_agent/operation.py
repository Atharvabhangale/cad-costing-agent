from pydantic import BaseModel, Field


class ManufacturingOperation(BaseModel):
    """A single manufacturing operation applied to a part.

    Extensible for later V-next additions (tooling cost, inspection cost,
    scrap allowance, etc.) — those are intentionally not included yet.
    """

    operation_id: str = Field(min_length=1)
    operation_name: str = Field(min_length=1)
    process_type: str = Field(min_length=1, description="e.g. 'CNC Drilling', 'Milling', 'Turning'.")
    machine_rate_per_hour: float = Field(gt=0, description="ILLUSTRATIVE POC VALUE unless overridden.")
    labor_rate_per_hour: float = Field(gt=0, description="ILLUSTRATIVE POC VALUE unless overridden.")
    setup_time_minutes: float = Field(ge=0)
    cycle_time_minutes: float = Field(gt=0, description="Cycle time per part.")
    quantity: int = Field(gt=0, default=1)
    is_illustrative_rate: bool = True
