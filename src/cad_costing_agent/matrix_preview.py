"""One part priced as a grid: rate-set versions down, quantities across.

A matrix preview answers "what would this part be estimated at under each of these
rate sets, at each of these quantities?" in one request. Every cell is the figure
`/cost/cad-part` would give for the same inputs with that rate set and that
quantity, because every cell is priced by the same path. It is a preview: it is
not an estimate, stores nothing, and does not choose.

What is deliberately absent here:

- **No ranking.** Rows are the rate sets in the order the caller listed them and
  cells are the quantities in the order the caller listed them. Nothing is
  ordered by cost and no cell carries a "cheapest" or "best" flag.
- **No arithmetic.** Every figure is copied from the result `price_cad_part`
  produced for that cell. This module cannot price anything.
- **No conversion.** A grid may list rate sets in different currencies; each cell
  is in its own rate set's currency and `currency_note` says the rows are not
  comparable. Nothing is converted and nothing is totalled.
- **No dropped cell.** A cell that cannot be priced has status `refused` and the
  pricing path's own reason. A missing figure is `None`, never 0, and a validator
  refuses a refused cell that carries one.
- **No accepted times.** The plan is priced as it was sent. An operation without a
  committed time is omitted from a cell exactly as it is from an estimate.

This module imports only `money` (so a figure is rounded exactly as an estimate's
is): it is handed plain values and returns plain values. `main.py` is the only
caller, and is where the pricing happens.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from pydantic import BaseModel, Field, model_validator

from cad_costing_agent.money import Money

STATUS_PRICED = "priced"
STATUS_REFUSED = "refused"

PREVIEW_NOTE = (
    "These are previews, not estimates. Each cell is the same part, material, plan, route and "
    "entered masses priced with one rate-set version at one quantity, exactly as an estimate "
    "would be; nothing was stored. Rows and columns are in the order you listed them and are not "
    "ranked: a lower figure is not a recommendation. Operations without a committed time are "
    "omitted from a cell exactly as they are from an estimate, so a figure covers only the time "
    "that has been committed. A cell that could not be priced is shown with the reason, not "
    "dropped or shown as zero."
)

MIXED_CURRENCY_NOTE = (
    "This grid lists rate sets in more than one currency ({currencies}). Each row is in its own "
    "rate set's currency. Nothing is converted, so figures in different currencies cannot be "
    "compared with each other."
)

_FIGURES = (
    "currency",
    "total_part_cost",
    "per_unit_cost",
    "total_material_cost",
    "total_operation_cost",
    "total_stage_cost",
    "direct_cost_base",
    "overhead_cost",
    "scrap_cost",
    "tooling_cost",
    "total_additional_cost",
    "material_charged",
)


class MatrixRefused(ValueError):
    """The request cannot be laid out as a grid at all. Answered as a 400."""


class RateSetRequest(BaseModel):
    """One rate set as the caller named it. `version` None means the latest."""

    rate_set_id: str
    version: str | None = None


def parse_rate_set_requests(text: str) -> list[RateSetRequest]:
    """The rate sets to price under, in the caller's order."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MatrixRefused(f"'rate_sets' must be valid JSON: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise MatrixRefused(
            "'rate_sets' must be a non-empty JSON list of {\"rate_set_id\": ..., \"version\": ...} "
            "objects; omit 'version' (or send null) for the latest."
        )
    requests: list[RateSetRequest] = []
    for item in raw:
        if not isinstance(item, dict):
            raise MatrixRefused("Each entry of 'rate_sets' must be an object with a 'rate_set_id'.")
        rate_set_id = item.get("rate_set_id")
        version = item.get("version")
        if not isinstance(rate_set_id, str) or not rate_set_id.strip():
            raise MatrixRefused("Each entry of 'rate_sets' needs a non-empty 'rate_set_id'.")
        if version is not None and (not isinstance(version, str) or not version.strip()):
            raise MatrixRefused("A rate set 'version' must be a version string, or null for the latest.")
        request = RateSetRequest(rate_set_id=rate_set_id.strip(), version=version)
        if request in requests:
            raise MatrixRefused(
                f"Rate set '{request.rate_set_id}'"
                + (f" version {request.version}" if request.version else " (latest)")
                + " is listed twice."
            )
        requests.append(request)
    return requests


def parse_quantities(text: str) -> list[int]:
    """The quantities to price at, in the caller's order."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MatrixRefused(f"'quantities' must be valid JSON: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise MatrixRefused("'quantities' must be a non-empty JSON list of whole numbers, e.g. [1, 10, 100].")
    quantities: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise MatrixRefused(
                f"Every quantity must be a whole number of at least 1; got {json.dumps(item)}."
            )
        if item in quantities:
            raise MatrixRefused(f"Quantity {item} is listed twice.")
        quantities.append(item)
    return quantities


def check_grid_size(rate_set_count: int, quantity_count: int, max_cells: int) -> None:
    """Refuse a grid over the cell budget. Each cell reads and prices the CAD file once."""
    cells = rate_set_count * quantity_count
    if cells > max_cells:
        raise MatrixRefused(
            f"{rate_set_count} rate set(s) x {quantity_count} quantity(ies) is {cells} cells; "
            f"one matrix preview prices at most {max_cells}. List fewer rate sets or quantities."
        )


def currency_note(currencies: Iterable[str | None]) -> str | None:
    """Says the rows are not comparable when the grid holds more than one currency."""
    seen: list[str] = []
    for currency in currencies:
        if currency is not None and currency not in seen:
            seen.append(currency)
    if len(seen) < 2:
        return None
    return MIXED_CURRENCY_NOTE.format(currencies=", ".join(seen))


class MatrixRateSet(BaseModel):
    """A row's rate set: what was asked for, and what it resolved to."""

    requested_rate_set_id: str
    requested_version: str | None = Field(
        default=None, description="The version asked for; null means the latest was asked for."
    )
    resolved: bool
    reason: str | None = Field(default=None, description="Why it did not resolve, when it did not.")
    rate_set_id: str | None = None
    name: str | None = None
    version: str | None = None
    currency: str | None = None
    is_illustrative: bool | None = None
    # The factory the rate set says it describes, so a grid of factories reads as
    # one. A label: absent (not null) when the version states none.
    region: str | None = Field(default=None, exclude_if=lambda value: value is None)
    factory_name: str | None = Field(default=None, exclude_if=lambda value: value is None)


class MatrixRefusal(BaseModel):
    code: str
    message: str
    stages: list = Field(default_factory=list, description="Per-stage problems, when given.")


class MatrixCell(BaseModel):
    rate_set: MatrixRateSet
    quantity: int
    status: str = Field(description="'priced' or 'refused'.")
    refusal: MatrixRefusal | None = None

    # Copied from the priced result; None on a refused cell, never 0.
    currency: str | None = None
    total_part_cost: Money | None = None
    per_unit_cost: Money | None = None
    total_material_cost: Money | None = None
    total_operation_cost: Money | None = None
    total_stage_cost: Money | None = None
    direct_cost_base: Money | None = None
    overhead_cost: Money | None = None
    scrap_cost: Money | None = None
    tooling_cost: Money | None = None
    total_additional_cost: Money | None = None
    material_charged: bool | None = None

    # What the cell's cost leaves out of the plan, from the plan itself; None
    # when no plan was sent (operations then came as entered requirements).
    operations_costed: int | None = None
    operations_not_timed: int | None = None

    @model_validator(mode="after")
    def _a_refused_cell_carries_no_figure(self) -> "MatrixCell":
        if self.status == STATUS_PRICED:
            if self.currency is None or self.total_part_cost is None or self.per_unit_cost is None:
                raise ValueError("A priced cell must carry its currency, total and per-unit cost.")
            return self
        if self.status != STATUS_REFUSED:
            raise ValueError(f"A cell is '{STATUS_PRICED}' or '{STATUS_REFUSED}', not '{self.status}'.")
        if self.refusal is None:
            raise ValueError("A refused cell must say why.")
        carried = [name for name in _FIGURES if getattr(self, name) is not None]
        if carried:
            raise ValueError(f"A refused cell must not carry a figure; got {', '.join(carried)}.")
        return self


class MatrixRow(BaseModel):
    rate_set: MatrixRateSet
    cells: list[MatrixCell]


class MatrixPreview(BaseModel):
    kind: str = "preview"
    stored: bool = Field(default=False, description="Always false: a preview stores nothing.")
    chosen: str | None = Field(
        default=None, description="Always null: a matrix never picks a rate set or a quantity."
    )
    note: str = PREVIEW_NOTE
    currency_note: str | None = Field(
        default=None, description="Present only when the grid holds more than one currency."
    )
    quantities: list[int] = Field(description="The columns, in the order they were listed.")
    rows: list[MatrixRow] = Field(description="One per rate set, in the order they were listed.")
