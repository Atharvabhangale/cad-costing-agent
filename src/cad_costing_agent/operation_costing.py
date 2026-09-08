"""Deterministic manufacturing operation cost calculation.

No AI/LLM involved:

    machine_cost = machine_rate_per_hour * (cycle_time_minutes * quantity / 60)
    setup_cost   = machine_rate_per_hour * (setup_time_minutes / 60)
    labor_cost   = labor_rate_per_hour * (cycle_time_minutes * quantity / 60)
    total_operation_cost = machine_cost + setup_cost + labor_cost
"""

from pydantic import BaseModel

from cad_costing_agent.operation import ManufacturingOperation
from cad_costing_agent.money import Money

MACHINE_COST_FORMULA = "machine_cost = machine_rate_per_hour * (cycle_time_minutes * quantity / 60)"
SETUP_COST_FORMULA = "setup_cost = machine_rate_per_hour * (setup_time_minutes / 60)"
LABOR_COST_FORMULA = "labor_cost = labor_rate_per_hour * (cycle_time_minutes * quantity / 60)"
TOTAL_COST_FORMULA = "total_operation_cost = machine_cost + setup_cost + labor_cost"


class OperationCostResult(BaseModel):
    operation_id: str
    operation_name: str
    process_type: str
    quantity: int
    machine_rate_per_hour: float
    labor_rate_per_hour: float
    setup_time_minutes: float
    cycle_time_minutes: float
    machine_cost: Money
    setup_cost: Money
    labor_cost: Money
    total_operation_cost: Money
    formulas: list[str]
    is_illustrative_rate: bool


def calculate_operation_cost(operation: ManufacturingOperation) -> OperationCostResult:
    run_time_hours = operation.cycle_time_minutes * operation.quantity / 60
    setup_time_hours = operation.setup_time_minutes / 60

    machine_cost = operation.machine_rate_per_hour * run_time_hours
    setup_cost = operation.machine_rate_per_hour * setup_time_hours
    labor_cost = operation.labor_rate_per_hour * run_time_hours
    total_operation_cost = machine_cost + setup_cost + labor_cost

    return OperationCostResult(
        operation_id=operation.operation_id,
        operation_name=operation.operation_name,
        process_type=operation.process_type,
        quantity=operation.quantity,
        machine_rate_per_hour=operation.machine_rate_per_hour,
        labor_rate_per_hour=operation.labor_rate_per_hour,
        setup_time_minutes=operation.setup_time_minutes,
        cycle_time_minutes=operation.cycle_time_minutes,
        machine_cost=machine_cost,
        setup_cost=setup_cost,
        labor_cost=labor_cost,
        total_operation_cost=total_operation_cost,
        formulas=[MACHINE_COST_FORMULA, SETUP_COST_FORMULA, LABOR_COST_FORMULA, TOTAL_COST_FORMULA],
        is_illustrative_rate=operation.is_illustrative_rate,
    )
