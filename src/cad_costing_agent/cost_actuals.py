"""A client's actual cost, recorded against an estimate, and the variance between them.

This is the first place the system's figures meet a number it did not produce.
Its design is about keeping the two apart.

What an actual is
-----------------

A cost somebody outside this application states for a part — an ERP actual, a
supplier invoice, a supplier's quotation — together with where it came from and
which of those kinds it is.
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

What kind of figure it is
-------------------------

A record states its `figure_kind`: an `actual` (a cost that was incurred) or a
`supplier_quote` (a price a supplier offers). Like the basis, it has no default
and is never guessed from the free-text `source` — a quote read as an actual
would be reported as the estimate being wrong. Rows recorded before the field
existed were all entered and displayed as actuals, and the migration says so.

The two are compared with the same arithmetic and reported in separate fields,
so a reader of one cannot pick up the other by accident:

    actual          variance = estimate - actual,       percent of the actual
    supplier_quote  gap      = quote - should-cost,     percent of the should-cost

An actual's variance says how far the estimate was from what happened. A quote's
gap is a difference to discuss with the supplier, and nothing here says which
side of it is right: no verdict, no judgement of the supplier, no ranking.
Several quotes for one estimate are listed side by side in the order entered,
never sorted by price, and none is marked as preferred.

`supplier_name` and `quote_reference` are free text kept as entered and matched
to nothing. They belong to a quote; on an actual they are refused, since a
record cannot be both.

A correction may change the kind. That is the one way to fix a quote that was
recorded as an actual, and the superseded record still shows what was entered.
"""

import math
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from cad_costing_agent.money import (
    CURRENCY_CODE_PATTERN,
    Money,
    Percent,
    round_money,
    round_percent,
)

PER_UNIT = "per_unit"
PER_BATCH = "per_batch"
COST_BASES = (PER_UNIT, PER_BATCH)

ACTUAL = "actual"
SUPPLIER_QUOTE = "supplier_quote"
FIGURE_KINDS = (ACTUAL, SUPPLIER_QUOTE)

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
QUOTE_GAP_BASIS = (
    "Per unit. Gap = supplier quote - should-cost estimate, so a positive figure means the "
    "quote is above the should-cost. The percentage is relative to the should-cost. A gap is "
    "a difference to discuss, not a finding about either figure. Currencies are never "
    "converted."
)
QUOTE_ORDER_NOTE = (
    "Current supplier quotes, in the order they were entered. Not sorted by price, and none "
    "is marked as preferred: choosing a supplier is not something this comparison does."
)
# The words that differ between the two kinds of figure. The arithmetic does not.
_WORDING = {
    ACTUAL: {
        "the_figure": "the actual",
        "The_figure": "The actual",
        "not_supplied": "Not supplied in the actual cost record. Not treated as zero.",
        "zero": "The actual is zero, so a percentage has no meaning here.",
        "difference": "variance",
    },
    SUPPLIER_QUOTE: {
        "the_figure": "the supplier quote",
        "The_figure": "The supplier quote",
        "not_supplied": "Not itemised in the supplier quote. Not treated as zero.",
        "zero": "The should-cost figure is zero, so a percentage has no meaning here.",
        "difference": "gap",
    },
}
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
    figure_kind: Literal["actual", "supplier_quote"] = Field(
        description=(
            "What kind of figure this is: a cost that was incurred, or a price a supplier "
            "offers. No default, and never read out of the free-text source."
        )
    )
    supplier_name: str | None = Field(
        default=None,
        max_length=200,
        description="Who quoted. Supplier quotes only; kept as entered, matched to nothing.",
    )
    quote_reference: str | None = Field(
        default=None,
        max_length=100,
        description="The supplier's quote number. Supplier quotes only; kept as entered.",
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

    @field_validator(
        "source",
        "supplier_name",
        "quote_reference",
        "client_part_number",
        "drawing_revision",
        "supersedes_actual_id",
    )
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
        if self.figure_kind != SUPPLIER_QUOTE and (self.supplier_name or self.quote_reference):
            raise ValueError(
                "A supplier name or quote reference belongs to a supplier quote, and this "
                "record says it is an actual. Which of the two is wrong cannot be told from "
                "here; where an actual came from goes in its source."
            )
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
    actual_per_unit: Money | None = Field(
        default=None, description="An actual's figure. Never set for a supplier quote."
    )
    variance: Money | None = Field(
        default=None, description="Estimate minus actual. Never set for a supplier quote."
    )
    variance_percent: Percent = None
    quote_per_unit: Money | None = Field(
        default=None, description="A supplier quote's figure. Never set for an actual."
    )
    gap: Money | None = Field(
        default=None, description="Quote minus should-cost. Never set for an actual."
    )
    gap_percent: Percent = Field(
        default=None, description="The gap relative to the should-cost."
    )
    statement: str = Field(
        default="", description="The compared difference in words. States no verdict."
    )
    note: str = ""


class CostVariance(BaseModel):
    estimate_id: str
    actual_id: str
    figure_kind: Literal["actual", "supplier_quote"]
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
    unattributed_quote_per_unit: Money | None = Field(
        default=None,
        description=(
            "The part of a supplier quote's total that no itemised category accounts for. "
            "Reported only when at least one category was itemised."
        ),
    )
    warnings: list[str] = Field(default_factory=list)
    basis: str = VARIANCE_BASIS
    category_mapping: str = CATEGORY_MAPPING


class SupplierQuoteColumn(BaseModel):
    """One current supplier quote, as a column of the side-by-side table."""

    actual_id: str
    supplier_name: str | None = None
    quote_reference: str | None = None
    source: str
    cost_date: date | None = None
    status: Literal["available", "unavailable"]
    unavailable_detail: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SupplierQuoteCell(BaseModel):
    actual_id: str
    state: Literal["compared", "not_supplied", "not_comparable", "unavailable"]
    quote_per_unit: Money | None = None
    gap: Money | None = None
    gap_percent: Percent = None


class SupplierQuoteRow(BaseModel):
    category: str
    label: str
    estimate_per_unit: Money | None = None
    cells: list[SupplierQuoteCell]


class SupplierQuoteComparison(BaseModel):
    """Current supplier quotes for one estimate, side by side, in the order entered."""

    estimate_id: str
    currency: str
    order: Literal["as_entered"] = "as_entered"
    ranked: Literal[False] = False
    preferred: None = None
    quotes: list[SupplierQuoteColumn] = Field(default_factory=list)
    rows: list[SupplierQuoteRow] = Field(default_factory=list)
    note: str = QUOTE_ORDER_NOTE
    basis: str = QUOTE_GAP_BASIS
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


def _amount(value: float) -> str:
    return f"{round_money(abs(value)):.2f}"


def _statement(kind: str, difference: float, percent: float | None, currency: str) -> str:
    """The compared difference in words. It names a direction and a size, nothing more."""
    if kind == SUPPLIER_QUOTE:
        subject, reference, of = "Quote", "should-cost", " of should-cost"
    else:
        subject, reference, of = "Estimate", "the actual", " of the actual"
    if round_money(difference) == 0:
        return f"{subject} equals {reference}."
    direction = "above" if difference > 0 else "below"
    share = "" if percent is None else f" ({abs(round_percent(percent)):.2f}%{of})"
    return f"{subject} is {_amount(difference)} {currency} {direction} {reference}{share}."


def _line(
    category: str,
    estimate: float | None,
    stated: float | None,
    *,
    kind: str,
    currency: str,
    note: str = "",
) -> VarianceLine:
    """One category. The same arithmetic for both kinds, reported in separate fields."""
    label = _LABELS[category]
    words = _WORDING[kind]
    figure = "quote_per_unit" if kind == SUPPLIER_QUOTE else "actual_per_unit"
    if stated is None:
        return VarianceLine(
            category=category,
            label=label,
            state=NOT_SUPPLIED,
            estimate_per_unit=estimate,
            note=note or words["not_supplied"],
        )
    if estimate is None:
        return VarianceLine(
            category=category,
            label=label,
            state=NOT_COMPARABLE,
            note=note or "The estimate has no comparable figure.",
            **{figure: stated},
        )
    if kind == SUPPLIER_QUOTE:
        difference, relative_to = stated - estimate, estimate
        fields = ("gap", "gap_percent")
    else:
        difference, relative_to = estimate - stated, stated
        fields = ("variance", "variance_percent")
    percent = None
    if relative_to == 0:
        note = note or words["zero"]
    else:
        percent = difference / relative_to * 100
    return VarianceLine(
        category=category,
        label=label,
        state=COMPARED,
        estimate_per_unit=estimate,
        statement=_statement(kind, difference, percent, currency),
        note=note,
        **{figure: stated, fields[0]: difference, fields[1]: percent},
    )


def compute_variance(figures: EstimateFigures, record: ActualCostRecord) -> CostVariance:
    """Compare one recorded figure with the estimate it was recorded against. Pure.

    An actual yields a variance and a supplier quote a gap; the module docstring
    says why they are not the same field.
    """
    kind = record.figure_kind
    words = _WORDING[kind]
    base = dict(
        estimate_id=figures.estimate_id,
        actual_id=record.actual_id,
        figure_kind=kind,
        currency=record.currency,
        estimate_quantity=figures.quantity,
        actual_batch_quantity=record.batch_quantity,
        basis=QUOTE_GAP_BASIS if kind == SUPPLIER_QUOTE else VARIANCE_BASIS,
    )
    if record.currency != figures.currency:
        return CostVariance(
            **base,
            status=UNAVAILABLE,
            unavailable_code="currency_mismatch",
            unavailable_detail=(
                f"{words['The_figure']} is in {record.currency} and the estimate in "
                f"{figures.currency}. Currencies are never converted."
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

    stated = {
        TOTAL: actual_per_unit(record.total_cost, record),
        MATERIAL: actual_per_unit(record.material_cost, record),
        MACHINING: actual_per_unit(record.machining_cost, record),
        ADDITIONAL: actual_per_unit(record.additional_cost, record),
    }

    def line(category: str, estimate: float | None, note: str = "") -> VarianceLine:
        return _line(
            category, estimate, stated[category], kind=kind, currency=record.currency, note=note
        )

    lines = [line(TOTAL, per_unit(figures.total))]
    if figures.kind == ASSEMBLY:
        lines += [
            line(category, None, "" if stated[category] is None else ASSEMBLY_BREAKDOWN_NOTE)
            for category in (MATERIAL, MACHINING, ADDITIONAL)
        ]
    else:
        lines += [
            line(MATERIAL, per_unit(figures.material)),
            line(MACHINING, per_unit(figures.machining)),
            line(ADDITIONAL, per_unit(figures.additional)),
        ]

    supplied = [stated[c] for c in (MATERIAL, MACHINING, ADDITIONAL) if stated[c] is not None]
    unattributed = stated[TOTAL] - sum(supplied) if supplied else None
    if unattributed is not None and math.isclose(unattributed, 0.0, abs_tol=1e-9):
        unattributed = 0.0

    warnings = []
    if record.batch_quantity is None:
        warnings.append(
            f"{words['The_figure']} does not state its batch size, so whether it matches the "
            f"estimate's {figures.quantity} cannot be checked. Setup and tooling per unit "
            "depend on it."
        )
    elif record.batch_quantity != figures.quantity:
        warnings.append(
            f"The estimate is for a batch of {figures.quantity} and {words['the_figure']} for "
            f"{record.batch_quantity}. Setup and tooling are spread over the batch, so part of "
            f"this {words['difference']} can come from the batch size alone. Nothing here "
            "adjusts for it."
        )

    unattributed_field = (
        "unattributed_quote_per_unit" if kind == SUPPLIER_QUOTE else "unattributed_actual_per_unit"
    )
    return CostVariance(
        **base,
        status=AVAILABLE,
        lines=lines,
        warnings=warnings,
        **{unattributed_field: unattributed},
    )


def current_records(records: list[ActualCostRecord]) -> list[ActualCostRecord]:
    """The records no later one has corrected, in the order they were entered."""
    superseded = {
        record.supersedes_actual_id
        for record in records
        if record.supersedes_actual_id is not None
    }
    return [record for record in records if record.actual_id not in superseded]


def compare_supplier_quotes(
    figures: EstimateFigures, records: list[ActualCostRecord]
) -> SupplierQuoteComparison:
    """The estimate's current supplier quotes side by side. Pure.

    `records` is every record of the estimate, oldest first, as the repository
    returns them. Actuals are left out — an actual is not an offer — and so is a
    quote a later record corrected. The order is the order entered and nothing
    here reorders it or singles a quote out.
    """
    quotes = [
        record for record in current_records(records) if record.figure_kind == SUPPLIER_QUOTE
    ]
    compared = [compute_variance(figures, record) for record in quotes]
    columns = [
        SupplierQuoteColumn(
            actual_id=record.actual_id,
            supplier_name=record.supplier_name,
            quote_reference=record.quote_reference,
            source=record.source,
            cost_date=record.cost_date,
            status=result.status,
            unavailable_detail=result.unavailable_detail,
            warnings=result.warnings,
        )
        for record, result in zip(quotes, compared)
    ]

    rows = []
    for category in CATEGORIES if quotes else ():
        cells = []
        estimate = None
        for result in compared:
            found = next((item for item in result.lines if item.category == category), None)
            if found is None:
                cells.append(SupplierQuoteCell(actual_id=result.actual_id, state=UNAVAILABLE))
                continue
            if found.estimate_per_unit is not None:
                estimate = found.estimate_per_unit
            cells.append(
                SupplierQuoteCell(
                    actual_id=result.actual_id,
                    state=found.state,
                    quote_per_unit=found.quote_per_unit,
                    gap=found.gap,
                    gap_percent=found.gap_percent,
                )
            )
        rows.append(
            SupplierQuoteRow(
                category=category,
                label=_LABELS[category],
                estimate_per_unit=estimate,
                cells=cells,
            )
        )

    return SupplierQuoteComparison(
        estimate_id=figures.estimate_id,
        currency=figures.currency,
        quotes=columns,
        rows=rows,
    )
