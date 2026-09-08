"""Deterministic part-level cost estimate.

    direct_cost_base = total_material_cost + total_operation_cost
    total_part_cost  = direct_cost_base + total_additional_cost

where `total_additional_cost` covers overhead, scrap allowance, and tooling —
see `additional_costs.py` for those formulas. Every component stays separately
visible in the result rather than being folded into the total.

Quantity semantics (to avoid double-counting):

The material calculation already multiplies by quantity
(``total_material_cost = mass_kg * material_cost_per_kg * quantity``), and each
operation's calculation already multiplies its machine/labor run cost by its
own ``quantity`` (setup cost is a one-time batch cost). Both therefore already
represent the cost for the *whole batch*, not a single unit.

For a part-level estimate to be meaningful, every operation must be costed for
the same batch as the part itself, so each operation's ``quantity`` must equal
the part's ``quantity``. The part-level calculator validates this and sums the
already-quantity-scaled `total_material_cost` and `total_operation_cost`
values as-is, applying no further multiplication by quantity. The additional
cost components are batch-level for the same reason and are likewise never
re-scaled by quantity.

The additional cost inputs all default to zero, so an estimate that does not
ask for them is identical to one produced before they existed.
"""

from pydantic import BaseModel, Field

from cad_costing_agent.additional_costs import (
    AdditionalCostInputs,
    AdditionalCostsResult,
    calculate_additional_costs,
)
from cad_costing_agent.costing import MaterialCostResult, calculate_material_cost
from cad_costing_agent.operation import ManufacturingOperation
from cad_costing_agent.operation_costing import OperationCostResult, calculate_operation_cost
from cad_costing_agent.money import DEFAULT_CURRENCY, Money
from cad_costing_agent.part import Part
from cad_costing_agent.rate_sets import (
    PRECEDENCE_RULE,
    OperationRequirement,
    RateSet,
    RateSetReference,
    resolve_material,
    resolve_operation,
)

TOTAL_PART_COST_FORMULA = (
    "total_part_cost = total_material_cost + total_operation_cost + total_additional_cost"
)
PER_UNIT_COST_FORMULA = "per_unit_cost = total_part_cost / part.quantity"
QUANTITY_ASSUMPTION = (
    "Each operation's quantity must equal the part's quantity: material cost and each "
    "operation's cost are each already scaled for the full batch quantity, so the "
    "part-level total simply sums them without multiplying by quantity again."
)


class PartCostEstimateRequest(BaseModel):
    part: Part
    operations: list[ManufacturingOperation] = Field(
        default_factory=list,
        description="Operations carrying their own rates. A rate set never overrides these.",
    )
    operation_requirements: list[OperationRequirement] = Field(
        default_factory=list,
        description="Operations whose rates are resolved from the selected rate set.",
    )
    additional_costs: AdditionalCostInputs | None = Field(
        default=None,
        description=(
            "Explicit overhead/scrap/tooling inputs. When supplied these replace the rate "
            "set's additional costs wholesale; when omitted the rate set's values are used, "
            "and with no rate set they default to zero."
        ),
    )
    rate_set_id: str | None = None
    rate_set_version: str | None = Field(
        default=None, description="Exact version to pin. Omit to use the latest version of the id."
    )


class PartCostEstimateResult(BaseModel):
    estimate_id: str | None = Field(
        default=None,
        description="Set once the estimate has been persisted; retrievable via /estimates/{id}.",
    )
    part_id: str
    part_name: str
    rate_set: RateSetReference | None = None
    material: MaterialCostResult
    operations: list[OperationCostResult]
    currency: str = Field(
        default=DEFAULT_CURRENCY,
        description="Currency of the rate set that produced this estimate. No conversion is applied.",
    )
    total_material_cost: Money
    total_operation_cost: Money
    direct_cost_base: Money
    additional_costs: AdditionalCostsResult
    total_additional_cost: Money
    total_part_cost: Money
    per_unit_cost: Money = Field(
        description=(
            "total_part_cost / part.quantity. The batch total already includes quantity, "
            "so dividing it back out is the only place quantity is undone — it is never "
            "applied a second time."
        )
    )
    formula: str
    per_unit_formula: str
    quantity_assumption: str
    rate_precedence: str


def _validate_rate_set_selection(
    request: PartCostEstimateRequest, rate_set: RateSet | None
) -> None:
    if rate_set is None:
        if request.rate_set_version is not None and request.rate_set_id is None:
            raise ValueError("rate_set_version was given without a rate_set_id.")
        if request.rate_set_id is not None:
            raise ValueError(
                f"Request selects rate set '{request.rate_set_id}' but no resolved rate set was "
                "supplied. Rate sets are looked up in the persistence layer, so the caller must "
                "resolve one and pass it in."
            )
        if request.operation_requirements:
            raise ValueError(
                "operation_requirements take their rates from a rate set, so a rate_set_id "
                "must be given. Use 'operations' instead to supply rates explicitly."
            )
        return

    if request.rate_set_id is not None and rate_set.rate_set_id != request.rate_set_id:
        raise ValueError(
            f"Resolved rate set '{rate_set.rate_set_id}' does not match the requested "
            f"'{request.rate_set_id}'."
        )
    if request.rate_set_version is not None and rate_set.version != request.rate_set_version:
        raise ValueError(
            f"Resolved rate set version '{rate_set.version}' does not match the requested "
            f"'{request.rate_set_version}'."
        )


def calculate_part_cost(
    request: PartCostEstimateRequest, rate_set: RateSet | None = None
) -> PartCostEstimateResult:
    """Cost a part deterministically.

    `rate_set` is injected by the caller — the API layer resolves it from the
    repository — so this module stays free of any database dependency.
    """
    part = request.part
    _validate_rate_set_selection(request, rate_set)

    # Material rates come from the rate set when one is selected; otherwise
    # `calculate_material_cost` falls back to the built-in material catalog.
    material = resolve_material(rate_set, part.material) if rate_set is not None else None
    material_result = calculate_material_cost(part, material=material)

    # Explicitly-rated operations first, then those priced by the rate set.
    operations: list[ManufacturingOperation] = list(request.operations)
    if rate_set is not None:
        operations += [
            resolve_operation(rate_set, requirement)
            for requirement in request.operation_requirements
        ]

    operation_results: list[OperationCostResult] = []
    for operation in operations:
        if operation.quantity != part.quantity:
            raise ValueError(
                f"Operation '{operation.operation_id}' quantity ({operation.quantity}) must "
                f"match part quantity ({part.quantity})."
            )
        operation_results.append(calculate_operation_cost(operation))

    total_material_cost = material_result.total_material_cost
    total_operation_cost = sum(result.total_operation_cost for result in operation_results)

    direct_cost_base = total_material_cost + total_operation_cost

    # Explicit additional costs replace the rate set's wholesale; omitting them
    # falls back to the rate set, and with no rate set to zero as before.
    if request.additional_costs is not None:
        additional_inputs = request.additional_costs
    elif rate_set is not None:
        additional_inputs = rate_set.additional_costs
    else:
        additional_inputs = AdditionalCostInputs()

    additional_costs_result = calculate_additional_costs(direct_cost_base, additional_inputs)
    # Full precision throughout; `Money` fields round only when serialized, so no
    # intermediate is ever rounded and then reused.
    total_part_cost = direct_cost_base + additional_costs_result.total_additional_cost
    # The batch total already accounts for quantity, so dividing it back out is the
    # only place quantity is undone — it is never applied a second time.
    per_unit_cost = total_part_cost / part.quantity

    return PartCostEstimateResult(
        part_id=part.part_id,
        part_name=part.part_name,
        rate_set=rate_set.reference() if rate_set is not None else None,
        material=material_result,
        operations=operation_results,
        total_material_cost=total_material_cost,
        total_operation_cost=total_operation_cost,
        direct_cost_base=direct_cost_base,
        additional_costs=additional_costs_result,
        total_additional_cost=additional_costs_result.total_additional_cost,
        total_part_cost=total_part_cost,
        per_unit_cost=per_unit_cost,
        currency=rate_set.currency if rate_set is not None else DEFAULT_CURRENCY,
        formula=TOTAL_PART_COST_FORMULA,
        per_unit_formula=PER_UNIT_COST_FORMULA,
        quantity_assumption=QUANTITY_ASSUMPTION,
        rate_precedence=PRECEDENCE_RULE,
    )
