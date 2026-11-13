"""Deterministic manufacturing operation cost calculation.

No AI/LLM involved:

    machine_cost = machine_rate_per_hour * (cycle_time_minutes * quantity / 60)
    setup_cost   = machine_rate_per_hour * (setup_time_minutes / 60)
    labor_cost   = labor_rate_per_hour * (cycle_time_minutes * quantity / 60)
    total_operation_cost = machine_cost + setup_cost + labor_cost

With a per-machine overhead stated by the rate set (both optional, each per hour
of run time — the hours labour is charged on):

    direct_overhead_cost   = direct_overhead_per_hour * (cycle_time_minutes * quantity / 60)
    indirect_overhead_cost = indirect_overhead_per_hour * (cycle_time_minutes * quantity / 60)
    total_operation_cost  += direct_overhead_cost + indirect_overhead_cost

An overhead rate that was not stated adds nothing and leaves no key behind: the
result of an operation without one is exactly what it always was.
"""

from pydantic import BaseModel, Field

from cad_costing_agent.operation import ManufacturingOperation
from cad_costing_agent.money import Money

MACHINE_COST_FORMULA = "machine_cost = machine_rate_per_hour * (cycle_time_minutes * quantity / 60)"
SETUP_COST_FORMULA = "setup_cost = machine_rate_per_hour * (setup_time_minutes / 60)"
LABOR_COST_FORMULA = "labor_cost = labor_rate_per_hour * (cycle_time_minutes * quantity / 60)"
TOTAL_COST_FORMULA = "total_operation_cost = machine_cost + setup_cost + labor_cost"
DIRECT_OVERHEAD_FORMULA = (
    "direct_overhead_cost = direct_overhead_per_hour * (cycle_time_minutes * quantity / 60)"
)
INDIRECT_OVERHEAD_FORMULA = (
    "indirect_overhead_cost = indirect_overhead_per_hour * (cycle_time_minutes * quantity / 60)"
)
TOTAL_WITH_OVERHEAD_FORMULA = (
    "total_operation_cost = machine_cost + setup_cost + labor_cost + direct_overhead_cost "
    "+ indirect_overhead_cost"
)


def _absent(value) -> bool:
    return value is None


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
    # Present only when the rate set stated a per-machine overhead for this
    # process; absent keys, never zeros, for an operation without one.
    direct_overhead_per_hour: float | None = Field(default=None, exclude_if=_absent)
    indirect_overhead_per_hour: float | None = Field(default=None, exclude_if=_absent)
    direct_overhead_cost: Money | None = Field(default=None, exclude_if=_absent)
    indirect_overhead_cost: Money | None = Field(default=None, exclude_if=_absent)


def calculate_operation_cost(operation: ManufacturingOperation) -> OperationCostResult:
    run_time_hours = operation.cycle_time_minutes * operation.quantity / 60
    setup_time_hours = operation.setup_time_minutes / 60

    machine_cost = operation.machine_rate_per_hour * run_time_hours
    setup_cost = operation.machine_rate_per_hour * setup_time_hours
    labor_cost = operation.labor_rate_per_hour * run_time_hours
    total_operation_cost = machine_cost + setup_cost + labor_cost
    formulas = [MACHINE_COST_FORMULA, SETUP_COST_FORMULA, LABOR_COST_FORMULA, TOTAL_COST_FORMULA]

    direct_overhead_cost = (
        None
        if operation.direct_overhead_per_hour is None
        else operation.direct_overhead_per_hour * run_time_hours
    )
    indirect_overhead_cost = (
        None
        if operation.indirect_overhead_per_hour is None
        else operation.indirect_overhead_per_hour * run_time_hours
    )
    if direct_overhead_cost is not None or indirect_overhead_cost is not None:
        total_operation_cost += (direct_overhead_cost or 0.0) + (indirect_overhead_cost or 0.0)
        formulas = [
            MACHINE_COST_FORMULA,
            SETUP_COST_FORMULA,
            LABOR_COST_FORMULA,
            *([DIRECT_OVERHEAD_FORMULA] if direct_overhead_cost is not None else []),
            *([INDIRECT_OVERHEAD_FORMULA] if indirect_overhead_cost is not None else []),
            TOTAL_WITH_OVERHEAD_FORMULA,
        ]

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
        formulas=formulas,
        is_illustrative_rate=operation.is_illustrative_rate,
        direct_overhead_per_hour=operation.direct_overhead_per_hour,
        indirect_overhead_per_hour=operation.indirect_overhead_per_hour,
        direct_overhead_cost=direct_overhead_cost,
        indirect_overhead_cost=indirect_overhead_cost,
    )


def run_overhead_cost(costed: OperationCostResult) -> float:
    """The overhead an operation charged on its run time; 0.0 when none was stated.

    For the places that split an operation's cost into setup and run: overhead
    is charged per hour of run time, so it belongs with the run.
    """
    return float(costed.direct_overhead_cost or 0.0) + float(costed.indirect_overhead_cost or 0.0)
