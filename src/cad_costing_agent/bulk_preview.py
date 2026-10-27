"""Bulk costing preview: the batch's CSV contract, its row results and its CSV export.

A preview prices many parts at once so a person can see where the money is
before reviewing any of them. It is **not an estimate**. Every time in it is the
system's own unreviewed prediction, used for the preview only, and nothing in
this feature is stored: no estimate, no plan, no accepted time.

This module is the pure half. It parses and validates the CSV a person uploads,
describes one row's outcome, and writes the results back out as CSV. It imports
nothing from this package — so it cannot reach a calculator, a rate set or the
database — and only `main.py` imports it. The pricing itself is
`main.price_cad_part`, the one path `/cost/cad-part` also uses, which is what
keeps a preview figure and a stored estimate from ever disagreeing.

CSV columns (a header row is required; names are case-insensitive):

    required   file, part_id, part_name, material, quantity
    optional   rate_set_id, rate_set_version, route_id, source_unit

- `file` must equal the filename of one uploaded CAD file.
- `quantity` is a whole number of at least 1.
- `rate_set_id` may be left blank when the batch supplies a default.
  `rate_set_version` blank means the latest version.
- `route_id` is a person's route decision from `/process-routes`. Blank means
  no decision, which prices the plan exactly as it would without a
  classification. A route that needs stage rates or a per-kg mass the CSV
  cannot supply is reported as refused, with the reason.
- `source_unit` defaults to `mm`.

A column that is not listed is refused for the whole batch: every row would be
read under a header the application cannot interpret. A problem confined to one
row — a missing file, a quantity of 0, an unknown material — is reported on
that row, and the other rows still run.
"""

from __future__ import annotations

import csv
import io
from typing import Literal

from pydantic import BaseModel, Field, model_validator

PREVIEW_LABEL = "Unreviewed system estimates — not an estimate, not quotable"

PREVIEW_NOTICE = (
    "This is a bulk preview, not an estimate. Every operation time is the system's own "
    "unreviewed prediction, used here only so the part can be priced; no person has accepted "
    "any of them. Nothing has been stored, and nothing here may be quoted. Open a part in the "
    "Estimate tab to review its operations and produce a real estimate."
)

REQUIRED_COLUMNS = ("file", "part_id", "part_name", "material", "quantity")
OPTIONAL_COLUMNS = ("rate_set_id", "rate_set_version", "route_id", "source_unit")
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

DEFAULT_SOURCE_UNIT = "mm"
MAX_FIELD_LENGTH = 200
MAX_QUANTITY = 1_000_000

STATUS_PRICED = "priced"
STATUS_REFUSED = "refused"
STATUS_ERROR = "error"


class BatchRefused(ValueError):
    """The whole batch cannot be read: nothing about it is row-specific."""


class BulkRow(BaseModel):
    """One CSV row, validated as far as the CSV alone allows.

    `errors` is filled for a row that cannot be priced as written. It is kept on
    the row rather than raised, so one bad row never stops the others.
    """

    line: int = Field(description="The row's line number in the CSV file (the header is line 1).")
    file: str
    part_id: str
    part_name: str
    material: str
    quantity: int | None
    rate_set_id: str | None = None
    rate_set_version: str | None = None
    route_id: str | None = None
    source_unit: str = DEFAULT_SOURCE_UNIT
    errors: list[str] = Field(default_factory=list)


class ParsedBatch(BaseModel):
    rows: list[BulkRow]


class OmittedOperation(BaseModel):
    """An operation left out of the preview's cost, and why. Never costed as zero."""

    operation_id: str
    operation_name: str
    reason: str


class BulkTotals(BaseModel):
    """One priced row's figures, in the rate set's currency. Unreviewed estimates."""

    total_cost: float
    per_unit_cost: float
    material_cost: float
    operations_cost: float
    stages_cost: float
    additional_cost: float


class BulkRowResult(BaseModel):
    preview_label: str = PREVIEW_LABEL
    line: int
    file: str
    part_id: str
    part_name: str
    material: str
    quantity: int | None = None
    rate_set_id: str | None = None
    rate_set_version: str | None = None
    status: Literal["priced", "refused", "error"]
    currency: str | None = None
    totals: BulkTotals | None = None
    operations_costed: int | None = None
    operations_omitted: int | None = None
    omitted_operations: list[OmittedOperation] = Field(default_factory=list)
    operations_excluded_by_route: int | None = None
    notes: list[str] = Field(
        default_factory=list,
        description="Things worth knowing about a priced row, such as that it is material only.",
    )
    route_id: str | None = None
    route: str | None = Field(
        default=None, description="The route the part was priced under, when a person chose one."
    )
    reason_code: str | None = None
    reason: str | None = Field(
        default=None, description="Why the row was refused or failed. Empty for a priced row."
    )

    @model_validator(mode="after")
    def _a_failed_row_shows_no_cost(self) -> "BulkRowResult":
        if self.status == STATUS_PRICED:
            if self.totals is None:
                raise ValueError("A priced row must carry its totals.")
        else:
            if self.totals is not None or self.currency is not None:
                raise ValueError("A refused or failed row must not carry a cost.")
            if not self.reason:
                raise ValueError("A refused or failed row must say why.")
        return self


class CurrencyTotal(BaseModel):
    currency: str
    priced_rows: int
    total_cost: float


class BulkSummary(BaseModel):
    rows: int
    priced: int
    refused: int
    error: int
    operations_omitted: int = Field(
        description="Operations across all priced rows that were left out for want of a time."
    )
    by_currency: list[CurrencyTotal]
    currency_note: str
    unreferenced_files: list[str] = Field(
        default_factory=list, description="Uploaded files no CSV row names."
    )


CURRENCY_NOTE = (
    "Currencies are never converted. Row totals are summed only within one currency, and only "
    "over priced rows; each is an unreviewed estimate, so the sum is a preview figure too."
)


# --------------------------------------------------------------------- parsing


def _clean(value: str | None) -> str:
    return (value or "").strip()


def parse_batch_csv(text: str, *, max_rows: int) -> ParsedBatch:
    """Read the batch CSV. Raises `BatchRefused` for anything that spoils the whole batch."""
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise BatchRefused("The CSV is empty: it needs a header row naming its columns.") from None

    columns = [_clean(name).lower() for name in header]
    if not any(columns):
        raise BatchRefused("The CSV's first row is blank: it needs a header row naming its columns.")

    duplicated = sorted({name for name in columns if name and columns.count(name) > 1})
    if duplicated:
        raise BatchRefused(f"The CSV header repeats column(s): {', '.join(duplicated)}.")
    unknown = [name for name in columns if name not in ALL_COLUMNS]
    if unknown:
        shown = ", ".join(repr(name) if name else "(blank)" for name in unknown)
        raise BatchRefused(
            f"Unknown CSV column(s): {shown}. Allowed columns are: {', '.join(ALL_COLUMNS)}."
        )
    missing = [name for name in REQUIRED_COLUMNS if name not in columns]
    if missing:
        raise BatchRefused(f"The CSV header is missing required column(s): {', '.join(missing)}.")

    rows: list[BulkRow] = []
    for cells in reader:
        if not any(_clean(cell) for cell in cells):
            continue  # a blank line is not a part
        if len(rows) >= max_rows:
            raise BatchRefused(
                f"The CSV has more than {max_rows} rows, above this POC's limit of {max_rows} "
                "parts per batch. Split it into smaller batches."
            )
        rows.append(_read_row(reader.line_num, columns, cells))

    if not rows:
        raise BatchRefused("The CSV has a header but no rows.")
    return ParsedBatch(rows=rows)


def _read_row(line: int, columns: list[str], cells: list[str]) -> BulkRow:
    errors: list[str] = []
    if len(cells) > len(columns):
        errors.append(
            f"The row has {len(cells)} cells but the header has {len(columns)} columns."
        )
    values = {name: _clean(cells[index]) if index < len(cells) else "" for index, name in enumerate(columns)}

    for name in REQUIRED_COLUMNS:
        if not values.get(name):
            errors.append(f"'{name}' is required and is blank.")
    for name, value in values.items():
        if len(value) > MAX_FIELD_LENGTH:
            errors.append(f"'{name}' is longer than {MAX_FIELD_LENGTH} characters.")

    quantity: int | None = None
    raw_quantity = values.get("quantity", "")
    if raw_quantity:
        try:
            quantity = int(raw_quantity)
        except ValueError:
            errors.append(f"'quantity' must be a whole number, not {raw_quantity!r}.")
        else:
            if quantity < 1:
                errors.append(f"'quantity' must be at least 1, not {quantity}.")
                quantity = None
            elif quantity > MAX_QUANTITY:
                errors.append(f"'quantity' must be at most {MAX_QUANTITY}, not {quantity}.")
                quantity = None

    return BulkRow(
        line=line,
        file=values.get("file", ""),
        part_id=values.get("part_id", ""),
        part_name=values.get("part_name", ""),
        material=values.get("material", ""),
        quantity=quantity,
        rate_set_id=values.get("rate_set_id") or None,
        rate_set_version=values.get("rate_set_version") or None,
        route_id=values.get("route_id") or None,
        source_unit=values.get("source_unit") or DEFAULT_SOURCE_UNIT,
        errors=errors,
    )


# ---------------------------------------------------------------------- export

CSV_COLUMNS = (
    "preview_label",
    "line",
    "file",
    "part_id",
    "part_name",
    "material",
    "quantity",
    "rate_set_id",
    "rate_set_version",
    "status",
    "currency",
    "total_cost",
    "per_unit_cost",
    "material_cost",
    "operations_cost",
    "stages_cost",
    "additional_cost",
    "operations_costed",
    "operations_omitted",
    "omitted_detail",
    "operations_excluded_by_route",
    "route",
    "notes",
    "reason",
)

_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def _text(value: str | None) -> str:
    """A text cell, made inert if a spreadsheet would read it as a formula."""
    if value is None:
        return ""
    return "'" + value if value.startswith(_FORMULA_LEADERS) else value


def _number(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return f"{value:.2f}"


def render_csv(rows: list[BulkRowResult]) -> str:
    """The results as CSV. Every row repeats the preview label; a failed row has no cost cells."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        totals = row.totals
        omitted = "; ".join(f"{op.operation_id} {op.operation_name}: {op.reason}" for op in row.omitted_operations)
        writer.writerow(
            [
                row.preview_label,
                row.line,
                _text(row.file),
                _text(row.part_id),
                _text(row.part_name),
                _text(row.material),
                _number(row.quantity),
                _text(row.rate_set_id),
                _text(row.rate_set_version),
                row.status,
                row.currency or "",
                _number(totals.total_cost if totals else None),
                _number(totals.per_unit_cost if totals else None),
                _number(totals.material_cost if totals else None),
                _number(totals.operations_cost if totals else None),
                _number(totals.stages_cost if totals else None),
                _number(totals.additional_cost if totals else None),
                _number(row.operations_costed),
                _number(row.operations_omitted),
                _text(omitted),
                _number(row.operations_excluded_by_route),
                _text(row.route),
                _text("; ".join(row.notes)),
                _text(row.reason),
            ]
        )
    return buffer.getvalue()


__all__ = [
    "ALL_COLUMNS",
    "BatchRefused",
    "BulkRow",
    "BulkRowResult",
    "BulkSummary",
    "BulkTotals",
    "CSV_COLUMNS",
    "CURRENCY_NOTE",
    "CurrencyTotal",
    "OmittedOperation",
    "OPTIONAL_COLUMNS",
    "PREVIEW_LABEL",
    "PREVIEW_NOTICE",
    "ParsedBatch",
    "REQUIRED_COLUMNS",
    "STATUS_ERROR",
    "STATUS_PRICED",
    "STATUS_REFUSED",
    "parse_batch_csv",
    "render_csv",
]
