"""A client's actual cost, recorded against an estimate, and the variance between them.

This is the first place the system's figures meet a number it did not produce.
Its design is about keeping the two apart.

What an actual is
-----------------

A cost somebody outside this application states for a part — an ERP actual, a
historical quotation, a supplier invoice — together with where it came from.
It is a recorded fact, never an input to costing: nothing here can reach a
calculator, a rate set or a time profile, and no estimate changes because an
actual was recorded against it. The module imports `money` and nothing else, and
a test holds that.

Only the total is required. A breakdown into material, machining and additional
cost is optional, and a category the client did not supply is reported as
**not supplied** — never as zero, because a zero would read as "this part had
no material cost" and produce a variance of the whole estimated figure.

The basis is never guessed
--------------------------

Shops state costs per unit or per batch, and the two differ by the batch size.
So the basis is a required field with no default, and a per-batch figure without
the batch it covers is refused rather than assumed to be one unit. A per-unit
figure may carry its batch size too; without it the per-unit value is still
exact, but whether the batch matches the estimate's cannot be checked, and the
variance says so.

Immutable, corrected by a new record
------------------------------------

An actual is never edited. A correction is a new record naming the one it
supersedes, so what was entered first stays on the record beside what replaced
it. The repository has no update or delete method.

The variance
------------

Reported per unit, because that is the only basis both sides can always be put
on. For each category:

    variance         = estimate_per_unit - actual_per_unit
    variance_percent = variance / actual_per_unit * 100

A positive variance means the estimate is higher than the actual. A percentage
is not reported where the actual is zero, since it has no meaning there.

Which estimate figure a category is compared with:

    material   <- total_material_cost
    machining  <- total_operation_cost   (machine + labour + setup, as costed)
    additional <- total_additional_cost  (overhead + scrap allowance + tooling)

An **assembly** estimate is compared on its total only. Its material, operation
and additional figures cover the manufactured components alone — purchased and
standard components are outside them — so setting them beside a client's
whole-assembly breakdown would compare different things. An assembly with no
complete total (components still needing input, mixed currencies) has nothing to
compare against; its stored row carries 0.0 there, and that zero is never read
as a figure.

Per-unit cost depends on the batch: setup and tooling are spread over it. A
different batch size on the two sides is reported as a warning beside the
variance, not corrected for — correcting it would need the setup share of the
actual, which the client data does not state.
"""

import math
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from cad_costing_agent.money import CURRENCY_CODE_PATTERN, Money, Percent, round_money

PER_UNIT = "per_unit"
PER_BATCH = "per_batch"
COST_BASES = (PER_UNIT, PER_BATCH)

PART = "part"
ASSEMBLY = "assembly"

TOTAL = "total"
MATERIAL = "material"
MACHINING = "machining"
ADDITIONAL = "additional"
CATEGORIES = (TOTAL, MATERIAL, MACHINING, ADDITIONAL)
_LABELS = {
    TOTAL: "Total",
    MATERIAL: "Material",
    MACHINING: "Machining (machine, labour, setup)",
    ADDITIONAL: "Additional (overhead, scrap, tooling)",
}

# Line states. Every category ends in exactly one.
COMPARED = "compared"
NOT_SUPPLIED = "not_supplied"
NOT_COMPARABLE = "not_comparable"

AVAILABLE = "available"
UNAVAILABLE = "unavailable"

VARIANCE_BASIS = (
    "Per unit. Variance = estimate - actual, so a positive figure means the estimate is "
    "higher than the actual. The percentage is relative to the actual. Currencies are never "
    "converted."
)
CATEGORY_MAPPING = (
    "Material is compared with the estimate's material cost; machining with its operation "
    "cost, which includes machine time, labour and setup; additional with its overhead, scrap "
    "allowance and tooling."
)
ASSEMBLY_BREAKDOWN_NOTE = (
    "Not compared: an assembly estimate's material, operation and additional figures cover "
    "its manufactured components only, so they are not the same quantity as a breakdown of "
    "the whole assembly."
)


class ActualCostInput(BaseModel):
    """What a person enters. Validated here so the rules hold for any caller."""

    source: str = Field(
        min_length=1,
        max_length=200,
        description="Where this figure came from, e.g. 'Client ERP actual, FY2025'. Required.",
    )
    cost_basis: Literal["per_unit", "per_batch"] = Field(
        description="Whether the amounts are for one unit or for the whole batch. No default."
    )
    batch_quantity: int | None = Field(
        default=None,
        gt=0,
        description="Units the cost covers. Required for a per-batch cost; optional per unit.",
    )
    currency: str = Field(pattern=CURRENCY_CODE_PATTERN)
    total_cost: float = Field(gt=0, allow_inf_nan=False)
    material_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    machining_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    additional_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cost_date: date | None = None
    client_part_number: str | None = Field(default=None, max_length=64)
    drawing_revision: str | None = Field(default=None, max_length=32)
    notes: str = Field(default="", max_length=2000)
    supersedes_actual_id: str | None = Field(
        default=None,
        max_length=32,
        description="The record this one corrects. The corrected record is kept unchanged.",
    )

    @field_validator("source", "client_part_number", "drawing_revision", "supersedes_actual_id")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("source")
    @classmethod
    def _source_present(cls, value: str | None) -> str:
        if not value:
            raise ValueError("State where this figure came from.")
        return value

    @model_validator(mode="after")
    def _unambiguous(self) -> "ActualCostInput":
        if self.cost_basis == PER_BATCH and self.batch_quantity is None:
            raise ValueError(
                "A per-batch cost needs the batch quantity it covers. Without it the cost per "
                "unit is unknown, and it is not assumed to be one unit."
            )
        supplied = [
            value
            for value in (self.material_cost, self.machining_cost, self.additional_cost)
            if value is not None
        ]
        if supplied and round_money(sum(supplied)) > round_money(self.total_cost):
            raise ValueError(
                f"The supplied breakdown adds up to {round_money(sum(supplied))}, more than the "
                f"total of {round_money(self.total_cost)}. One of them is wrong, and which one "
                "cannot be told from here."
            )
        return self


class ActualCostRecord(ActualCostInput):
    """A stored actual: the input as entered, plus its identity."""

    actual_id: str
    estimate_id: str
    recorded_at: datetime = Field(description="When it was recorded in this application.")


class EstimateFigures(BaseModel):
    """The stored estimate, reduced to what a variance needs.

    A plain carrier built by the caller from the stored row and snapshot, so this
    module reads no repository. Batch figures, exactly as the estimate stored
    them. `None` means the estimate has no comparable figure, never zero.
    """

    estimate_id: str
    kind: Literal["part", "assembly"]
    currency: str
    quantity: int = Field(gt=0)
    total: float | None
    material: float | None = None
    machining: float | None = None
    additional: float | None = None
    unavailable_code: str | None = None
    unavailable_detail: str | None = None


class VarianceLine(BaseModel):
    category: str
    label: str
    state: Literal["compared", "not_supplied", "not_comparable"]
    estimate_per_unit: Money | None = None
    actual_per_unit: Money | None = None
    variance: Money | None = None
    variance_percent: Percent = None
    note: str = ""


class CostVariance(BaseModel):
    estimate_id: str
    actual_id: str
    status: Literal["available", "unavailable"]
    unavailable_code: str | None = None
    unavailable_detail: str | None = None
    currency: str
    estimate_quantity: int
    actual_batch_quantity: int | None = None
    lines: list[VarianceLine] = Field(default_factory=list)
    unattributed_actual_per_unit: Money | None = Field(
        default=None,
        description=(
            "The part of the actual total no supplied category accounts for. Reported only "
            "when at least one category was supplied; it is not assigned to any of them."
        ),
    )
    warnings: list[str] = Field(default_factory=list)
    basis: str = VARIANCE_BASIS
    category_mapping: str = CATEGORY_MAPPING


def figures_from_estimate(
    *,
    estimate_id: str,
    source: str,
    quantity: int,
    currency: str,
    total_material_cost: float,
    total_operation_cost: float,
    total_additional_cost: float,
    total_part_cost: float,
    result_snapshot: dict | None,
) -> EstimateFigures:
    """Build the carrier from a stored estimate's own fields.

    A part estimate's columns are its batch figures. An assembly's are read from
    its snapshot instead, because the row stores 0.0 where the roll-up had no
    total, and that zero must not become a comparison.
    """
    if source != "cost/assembly":
        return EstimateFigures(
            estimate_id=estimate_id,
            kind=PART,
            currency=currency,
            quantity=quantity,
            total=total_part_cost,
            material=total_material_cost,
            machining=total_operation_cost,
            additional=total_additional_cost,
        )

    result = result_snapshot or {}
    assembly_quantity = int(result.get("assembly_quantity") or quantity or 1)
    code = detail = None
    if result.get("is_aggregatable") is False:
        code = "mixed_currency"
        detail = (
            "This assembly's components are priced in more than one currency, so it has no "
            "single total to compare with."
        )
    elif not result.get("is_complete"):
        code = "assembly_incomplete"
        detail = (
            "This assembly estimate is a partial sum: some components still need costing input "
            "or committed manufacturing time. Comparing it with an actual would report the gap "
            "as a variance."
        )
    elif result.get("production_total_cost") is None:
        code = "no_total"
        detail = "This estimate has no total to compare with."

    return EstimateFigures(
        estimate_id=estimate_id,
        kind=ASSEMBLY,
        currency=result.get("currency") or currency,
        quantity=assembly_quantity,
        total=None if code else result.get("production_total_cost"),
        unavailable_code=code,
        unavailable_detail=detail,
    )


def actual_per_unit(amount: float | None, record: ActualCostInput) -> float | None:
    """One stated amount on a per-unit basis. Exact; nothing is rounded here."""
    if amount is None:
        return None
    if record.cost_basis == PER_UNIT:
        return amount
    # The input model refuses a per-batch cost without its quantity.
    return amount / record.batch_quantity


def _line(category: str, estimate: float | None, actual: float | None, note: str = "") -> VarianceLine:
    label = _LABELS[category]
    if actual is None:
        return VarianceLine(
            category=category,
            label=label,
            state=NOT_SUPPLIED,
            estimate_per_unit=estimate,
            note=note or "Not supplied in the actual cost record. Not treated as zero.",
        )
    if estimate is None:
        return VarianceLine(
            category=category,
            label=label,
            state=NOT_COMPARABLE,
            actual_per_unit=actual,
            note=note or "The estimate has no comparable figure.",
        )
    variance = estimate - actual
    percent = None
    if actual == 0:
        note = note or "The actual is zero, so a percentage has no meaning here."
    else:
        percent = variance / actual * 100
    return VarianceLine(
        category=category,
        label=label,
        state=COMPARED,
        estimate_per_unit=estimate,
        actual_per_unit=actual,
        variance=variance,
        variance_percent=percent,
        note=note,
    )


def compute_variance(figures: EstimateFigures, record: ActualCostRecord) -> CostVariance:
    """Compare one actual with the estimate it was recorded against. Pure."""
    base = dict(
        estimate_id=figures.estimate_id,
        actual_id=record.actual_id,
        currency=record.currency,
        estimate_quantity=figures.quantity,
        actual_batch_quantity=record.batch_quantity,
    )
    if record.currency != figures.currency:
        return CostVariance(
            **base,
            status=UNAVAILABLE,
            unavailable_code="currency_mismatch",
            unavailable_detail=(
                f"The actual is in {record.currency} and the estimate in {figures.currency}. "
                "Currencies are never converted."
            ),
        )
    if figures.unavailable_code or figures.total is None:
        return CostVariance(
            **base,
            status=UNAVAILABLE,
            unavailable_code=figures.unavailable_code or "no_total",
            unavailable_detail=figures.unavailable_detail or "The estimate has no total.",
        )

    def per_unit(amount: float | None) -> float | None:
        return None if amount is None else amount / figures.quantity

    actual = {
        TOTAL: actual_per_unit(record.total_cost, record),
        MATERIAL: actual_per_unit(record.material_cost, record),
        MACHINING: actual_per_unit(record.machining_cost, record),
        ADDITIONAL: actual_per_unit(record.additional_cost, record),
    }

    lines = [_line(TOTAL, per_unit(figures.total), actual[TOTAL])]
    if figures.kind == ASSEMBLY:
        for category in (MATERIAL, MACHINING, ADDITIONAL):
            lines.append(
                VarianceLine(
                    category=category,
                    label=_LABELS[category],
                    state=NOT_SUPPLIED if actual[category] is None else NOT_COMPARABLE,
                    actual_per_unit=actual[category],
                    note=(
                        "Not supplied in the actual cost record. Not treated as zero."
                        if actual[category] is None
                        else ASSEMBLY_BREAKDOWN_NOTE
                    ),
                )
            )
    else:
        lines += [
            _line(MATERIAL, per_unit(figures.material), actual[MATERIAL]),
            _line(MACHINING, per_unit(figures.machining), actual[MACHINING]),
            _line(ADDITIONAL, per_unit(figures.additional), actual[ADDITIONAL]),
        ]

    supplied = [actual[c] for c in (MATERIAL, MACHINING, ADDITIONAL) if actual[c] is not None]
    unattributed = actual[TOTAL] - sum(supplied) if supplied else None
    if unattributed is not None and math.isclose(unattributed, 0.0, abs_tol=1e-9):
        unattributed = 0.0

    warnings = []
    if record.batch_quantity is None:
        warnings.append(
            f"The actual does not state its batch size, so whether it matches the estimate's "
            f"{figures.quantity} cannot be checked. Setup and tooling per unit depend on it."
        )
    elif record.batch_quantity != figures.quantity:
        warnings.append(
            f"The estimate is for a batch of {figures.quantity} and the actual for "
            f"{record.batch_quantity}. Setup and tooling are spread over the batch, so part of "
            "this variance can come from the batch size alone. Nothing here adjusts for it."
        )

    return CostVariance(
        **base,
        status=AVAILABLE,
        lines=lines,
        unattributed_actual_per_unit=unattributed,
        warnings=warnings,
    )
