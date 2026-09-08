"""Deterministic additional cost components: overhead, scrap allowance, tooling.

These sit alongside — never inside — the existing material and operation cost
calculations, so each component stays separately visible in the result rather
than being folded into a single opaque total. No AI/LLM involved.

Cost base
---------

Both percentage-based components are applied to the same explicit base:

    direct_cost_base = total_material_cost + total_operation_cost

That base deliberately EXCLUDES overhead, scrap, and tooling, so the
components never compound: overhead is never charged on scrap, scrap is never
charged on overhead, and neither is charged on tooling. Each is computed once,
from the same base.

Formulas
--------

    overhead_cost         = overhead_rate_percent / 100 * direct_cost_base
    scrap_cost            = scrap_rate_percent / 100 * direct_cost_base
    tooling_cost          = tooling_cost_per_batch
    total_additional_cost = overhead_cost + scrap_cost + tooling_cost

Quantity semantics
------------------

All three components are BATCH-LEVEL totals, consistent with the rest of the
model:

- `direct_cost_base` is already scaled for the full batch quantity (material
  cost multiplies by quantity; operation run cost multiplies by quantity while
  setup is a one-time batch cost). The percentage components therefore inherit
  that batch scaling and must NOT be multiplied by quantity again.
- `tooling_cost_per_batch` is an explicit one-time cost for the batch, like
  operation setup cost, so it is likewise not multiplied by quantity.

Rates
-----

All rates are ILLUSTRATIVE POC INPUTS supplied per request. There is no
built-in rate table and no real-world pricing here; every value defaults to
zero, so an estimate that does not ask for these components is unchanged.
"""

from pydantic import BaseModel, Field
from cad_costing_agent.money import Money

OVERHEAD_COST_FORMULA = "overhead_cost = overhead_rate_percent / 100 * direct_cost_base"
SCRAP_COST_FORMULA = "scrap_cost = scrap_rate_percent / 100 * direct_cost_base"
TOOLING_COST_FORMULA = "tooling_cost = tooling_cost_per_batch"
TOTAL_ADDITIONAL_COST_FORMULA = (
    "total_additional_cost = overhead_cost + scrap_cost + tooling_cost"
)
DIRECT_COST_BASE_FORMULA = "direct_cost_base = total_material_cost + total_operation_cost"

ASSUMPTIONS = [
    "Overhead and scrap are both percentages of direct_cost_base (material + operations only), "
    "so they never compound on each other or on tooling.",
    "direct_cost_base is already scaled for the full batch quantity, so the percentage-based "
    "components are batch-level and are not multiplied by quantity again.",
    "tooling_cost_per_batch is a one-time cost for the whole batch, like operation setup cost, "
    "so it is not multiplied by quantity either.",
    "Scrap is modelled as a linear allowance on direct cost, not as a yield-based uplift of "
    "1 / (1 - scrap_rate); it therefore understates cost at high scrap rates.",
    "All rates are illustrative POC inputs, not real market or customer pricing.",
]


class AdditionalCostInputs(BaseModel):
    """Explicit, caller-supplied rates for the additional cost components.

    Every field defaults to zero so that omitting them leaves an estimate
    identical to one produced before these components existed.
    """

    overhead_rate_percent: float = Field(
        default=0.0,
        ge=0,
        le=1000,
        description=(
            "Overhead as a percentage of direct_cost_base. ILLUSTRATIVE POC VALUE. "
            "Capped at 1000 to catch a fraction (0.15) being passed where a percent (15) "
            "was meant."
        ),
    )
    scrap_rate_percent: float = Field(
        default=0.0,
        ge=0,
        lt=100,
        description=(
            "Scrap allowance as a percentage of direct_cost_base. ILLUSTRATIVE POC VALUE. "
            "Must be below 100: at 100% no good parts are produced, so a cost per part "
            "would be meaningless."
        ),
    )
    tooling_cost_per_batch: float = Field(
        default=0.0,
        ge=0,
        description="One-time tooling cost for the whole batch. ILLUSTRATIVE POC VALUE.",
    )


class AdditionalCostsResult(BaseModel):
    direct_cost_base: Money
    overhead_rate_percent: float
    overhead_cost: Money
    scrap_rate_percent: float
    scrap_cost: Money
    tooling_cost_per_batch: float
    tooling_cost: Money
    total_additional_cost: Money
    formulas: list[str]
    assumptions: list[str]


def calculate_additional_costs(
    direct_cost_base: float, inputs: AdditionalCostInputs
) -> AdditionalCostsResult:
    """Compute overhead, scrap, and tooling from an already batch-scaled base."""
    if direct_cost_base < 0:
        raise ValueError(f"direct_cost_base must not be negative, got {direct_cost_base}.")

    overhead_cost = inputs.overhead_rate_percent / 100 * direct_cost_base
    scrap_cost = inputs.scrap_rate_percent / 100 * direct_cost_base
    tooling_cost = inputs.tooling_cost_per_batch
    total_additional_cost = overhead_cost + scrap_cost + tooling_cost

    return AdditionalCostsResult(
        direct_cost_base=direct_cost_base,
        overhead_rate_percent=inputs.overhead_rate_percent,
        overhead_cost=overhead_cost,
        scrap_rate_percent=inputs.scrap_rate_percent,
        scrap_cost=scrap_cost,
        tooling_cost_per_batch=inputs.tooling_cost_per_batch,
        tooling_cost=tooling_cost,
        total_additional_cost=total_additional_cost,
        formulas=[
            DIRECT_COST_BASE_FORMULA,
            OVERHEAD_COST_FORMULA,
            SCRAP_COST_FORMULA,
            TOOLING_COST_FORMULA,
            TOTAL_ADDITIONAL_COST_FORMULA,
        ],
        assumptions=list(ASSUMPTIONS),
    )
