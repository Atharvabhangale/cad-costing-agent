"""What makes up one stored estimate's cost: a driver table read from its snapshot.

What this is, and what it deliberately is not
---------------------------------------------
A breakdown of **one** estimate's stored total into the figures that built it —
material, each priced route stage, the operations of each process type,
overhead, scrap allowance, tooling — each with its share of that total. For an
assembly roll-up the drivers are the roll-up's own stored categories and its
components.

It reads; it calculates no cost. Every amount is a figure the estimate stored,
or a plain sum of stored figures of the same kind (the operations of one process
type). Nothing is re-derived from a rate and a time, nothing is looked up, and a
field the snapshot does not hold is reported as not recorded — never as zero.

This module imports `money` and nothing else from the package, and only
`main.py` imports it. It is handed the stored result snapshot as a plain dict,
so it cannot reach a calculator, a rate set, the repository, the quotation or
the comparison layer; a structural test holds both directions.

The drivers partition the stored total
--------------------------------------
No driver contains any part of another, so they add up, and the sum is checked
against the stored total. The stored total is the authority: whatever the
drivers as shown do not account for — the rounding of individually rounded
amounts, or a field an older snapshot never recorded — is its own
`reconciliation` driver, never absorbed into a neighbour. Amounts are compared
at the money policy's two decimals, so the drivers as shown always sum to the
total as shown.

A share is the driver's own amount over the stored total, from the unrounded
values, rounded half-up to two decimals. Rounded shares need not add to exactly
100.00, and when they do not the breakdown says so instead of nudging one.

Order is the build-up, never the size
-------------------------------------
Drivers are listed in the order the cost is built up — material, route stages
in route order, operations by process type in the order the plan first costs
them, then overhead, scrap and tooling — whatever their amounts. Nothing here
sorts, ranks or marks a driver; which one matters is the engineer's reading.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from cad_costing_agent.money import Money, Percent, round_money, round_percent

PART = "part"
ASSEMBLY = "assembly"
UNKNOWN = "unknown"

AVAILABLE = "available"
PARTIAL = "partial"
NOT_AVAILABLE = "not_available"

NOT_RECORDED = "Not recorded in this estimate's stored result."
ORDER_NOTE = (
    "Listed in the order the cost is built up, not by size. Nothing here is ranked."
)
READ_NOTE = (
    "Read from the figures this estimate stored. Nothing is recalculated: no rate is looked "
    "up and no time is multiplied."
)
SETUP_NOTE = (
    "A setup's time is charged once, on the first costed operation in it, so a setup shared "
    "with another process is shown here in full."
)
# Same allowance the quotation uses before calling a remainder more than rounding:
# two decimals rounded a dozen times drift by pennies; more than this is a real gap.
ROUNDING_ALLOWANCE = Decimal("0.5")
_NEGLIGIBLE = Decimal("0.005")
_HUNDRED = Decimal("100")


class DriverPart(BaseModel):
    """A stored split inside one driver. Informational: never added to the total."""

    label: str
    amount: Money | None = None


class CostDriver(BaseModel):
    key: str
    category: str
    label: str
    amount: Money | None = Field(
        default=None, description="The stored figure. None when it was not recorded."
    )
    share_percent: Percent = Field(
        default=None, description="amount / the stored total x 100, from unrounded values."
    )
    recorded: bool = True
    note: str = ""
    of_which: list[DriverPart] = Field(default_factory=list)


class CostDriverBreakdown(BaseModel):
    estimate_id: str
    estimate_kind: str
    status: str = Field(description="'available', 'partial' or 'not_available'.")
    reason: str = ""
    currency: str | None = None
    quantity: int | None = None
    basis: str = ""
    total: Money | None = Field(
        default=None, description="Exactly the total the estimate stored. Never recomputed."
    )
    drivers: list[CostDriver] = Field(default_factory=list)
    component_drivers: list[CostDriver] = Field(
        default_factory=list,
        description="Assembly roll-ups only: each component's stored contribution.",
    )
    share_total_percent: Percent = None
    rounding_note: str = ""
    order_note: str = ORDER_NOTE
    note: str = READ_NOTE


# --------------------------------------------------------------------- reading


def _part_cost_of(result: dict) -> dict:
    """`/cost/cad-part` nests the part cost under `part_cost`; `/cost/part` stores it flat."""
    nested = result.get("part_cost")
    if isinstance(nested, dict) and nested:
        return nested
    if "total_part_cost" in result:
        return result
    return {}


def _dec(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _shown(value: Decimal) -> Decimal:
    """A figure as the money policy presents it."""
    return Decimal(str(round_money(float(value))))


class _Row:
    """A driver while it is being assembled: its exact amount kept beside it."""

    def __init__(self, key, category, label, exact, note="", of_which=None, recorded=None):
        self.key = key
        self.category = category
        self.label = label
        self.exact = exact
        self.note = note
        self.of_which = of_which or []
        self.recorded = exact is not None if recorded is None else recorded


def _part(label: str, value) -> DriverPart:
    exact = _dec(value)
    return DriverPart(label=label, amount=None if exact is None else float(exact))


def _not_recorded(key: str, category: str, label: str) -> _Row:
    return _Row(key, category, label, None, NOT_RECORDED, recorded=False)


# --------------------------------------------------------------------- partition


def _partition(
    rows: list[_Row], total: Decimal, *, missing_is_in_total: bool = True
) -> tuple[list[CostDriver], Decimal | None]:
    """Shares for every row, plus the remainder as its own driver when there is one.

    `missing_is_in_total` says whether a row with no amount is money the total
    holds but the snapshot did not split out (a part's unrecorded field), or a
    row the total leaves out (an assembly component that was never costed).
    """
    shown_sum = sum((_shown(row.exact) for row in rows if row.exact is not None), Decimal(0))
    remainder = _shown(total) - shown_sum
    if abs(remainder) >= _NEGLIGIBLE:
        unrecorded = (
            [row.label for row in rows if not row.recorded] if missing_is_in_total else []
        )
        if unrecorded:
            note = (
                "The stored total less every recorded driver as shown. It includes "
                + ", ".join(unrecorded)
                + ", which this estimate did not record separately. The stored total is the "
                "authority; the difference is reported, not absorbed."
            )
        elif abs(remainder) <= ROUNDING_ALLOWANCE:
            note = (
                "The rounding of individually rounded amounts: each figure is rounded to two "
                "decimals from its own exact value. Reported, not absorbed into a driver."
            )
        else:
            note = (
                "Left unaccounted for by the recorded drivers. The stored total is the "
                "authority; the difference is reported, not absorbed."
            )
        rows = [*rows, _Row("reconciliation", "reconciliation", "Reconciliation", remainder, note)]

    drivers = []
    share_sum = Decimal(0)
    for row in rows:
        share = None
        if row.exact is not None and total != 0:
            # Exact in memory, rounded on the way out by `Percent`, like `Money`.
            share = float(row.exact * _HUNDRED / total)
            share_sum += Decimal(str(round_percent(share)))
        drivers.append(
            CostDriver(
                key=row.key,
                category=row.category,
                label=row.label,
                amount=None if row.exact is None else float(row.exact),
                share_percent=share,
                recorded=row.recorded,
                note=row.note,
                of_which=row.of_which,
            )
        )
    return drivers, (share_sum if total != 0 else None)


def _rounding_note(share_sum: Decimal | None) -> str:
    if share_sum is None:
        return "The stored total is zero, so no share can be stated."
    if share_sum == _HUNDRED:
        return ""
    return (
        f"The shares as shown add up to {share_sum:.2f}%, not 100.00%: each is rounded to two "
        "decimals from its own exact value, and none has been adjusted to make them agree."
    )


# --------------------------------------------------------------------- a part


def _stage_note(line: dict) -> str:
    parts = []
    basis, rate = line.get("unit_basis"), line.get("unit_rate")
    if basis and rate is not None:
        parts.append(f"{rate} {basis.replace('_', ' ')}")
    if line.get("mass_kg") is not None:
        mass = f"on {line['mass_kg']} kg"
        if line.get("mass_source"):
            mass += f" ({line['mass_source'].replace('_', ' ')})"
        parts.append(mass)
    if line.get("yield_percent") is not None:
        parts.append(
            f"yield {line['yield_percent']}% applied to {line.get('base_mass_kg')} kg"
            + (
                f" ({line['base_mass_source'].replace('_', ' ')})"
                if line.get("base_mass_source")
                else ""
            )
        )
    if line.get("quantity") is not None:
        parts.append(f"x {line['quantity']} off")
    if line.get("supplies_material"):
        parts.append("includes the material")
    return ", ".join(parts)


def _operation_rows(part_cost: dict) -> list[_Row]:
    operations = part_cost.get("operations")
    if not isinstance(operations, list):
        if part_cost.get("total_operation_cost") is None:
            return [_not_recorded("operations", "operations", "Operations")]
        return [
            _Row(
                "operations",
                "operations",
                "Operations",
                _dec(part_cost.get("total_operation_cost")),
                "Per-operation costs were not recorded, so this cannot be split by process type.",
            )
        ]

    groups: dict[str, list[dict]] = {}
    for operation in operations:
        if isinstance(operation, dict):
            groups.setdefault(str(operation.get("process_type") or "unspecified"), []).append(
                operation
            )

    rows = []
    for process_type, members in groups.items():
        totals = [_dec(op.get("total_operation_cost")) for op in members]
        if any(value is None for value in totals):
            rows.append(
                _not_recorded(f"process:{process_type}", "operations", f"Operations — {process_type}")
            )
            continue

        def summed(field: str) -> float | None:
            values = [_dec(op.get(field)) for op in members]
            return None if any(v is None for v in values) else float(sum(values, Decimal(0)))

        rows.append(
            _Row(
                f"process:{process_type}",
                "operations",
                f"Operations — {process_type}",
                sum(totals, Decimal(0)),
                f"{len(members)} costed operation(s), as stored."
                + (
                    " " + SETUP_NOTE
                    if any(_dec(op.get("setup_cost")) for op in members)
                    else ""
                ),
                [
                    _part("Setup", summed("setup_cost")),
                    _part("Machine (run)", summed("machine_cost")),
                    _part("Labour (run)", summed("labor_cost")),
                ],
            )
        )
    return rows


def _part_breakdown(estimate_id: str, result: dict) -> CostDriverBreakdown:
    part_cost = _part_cost_of(result)
    total = _dec(part_cost.get("total_part_cost"))
    material = part_cost.get("material") if isinstance(part_cost.get("material"), dict) else {}
    quantity = material.get("quantity")
    base = CostDriverBreakdown(
        estimate_id=estimate_id,
        estimate_kind=PART,
        status=NOT_AVAILABLE,
        currency=part_cost.get("currency"),
        quantity=quantity,
        basis=f"the whole batch of {quantity}" if quantity else "the whole batch",
    )
    if total is None:
        base.reason = "This estimate stored no part total, so there is nothing to break down."
        return base

    rows: list[_Row] = []

    material_note = part_cost.get("material_note") or ""
    if result.get("material_basis") == "bar_stock":
        material_note = (
            "Costed from the confirmed bar stock, not the finished part's own mass. "
            + material_note
        ).strip()
    if part_cost.get("total_material_cost") is None:
        rows.append(_not_recorded("material", "material", "Material"))
    else:
        rows.append(
            _Row("material", "material", "Material", _dec(part_cost["total_material_cost"]),
                 material_note)
        )

    for line in part_cost.get("stage_costs") or []:
        if not isinstance(line, dict):
            continue
        stage = str(line.get("stage") or "stage")
        label = f"Route stage — {line.get('label') or stage}"
        if line.get("total_cost") is None:
            rows.append(_not_recorded(f"stage:{stage}", "route_stage", label))
            continue
        of_which = [_part("Run", line.get("run_cost"))]
        if _dec(line.get("batch_charge")):
            of_which.append(_part("Batch charge (one-off)", line.get("batch_charge")))
        rows.append(
            _Row(f"stage:{stage}", "route_stage", label, _dec(line["total_cost"]),
                 _stage_note(line), of_which)
        )

    rows.extend(_operation_rows(part_cost))

    additional = part_cost.get("additional_costs")
    if isinstance(additional, dict):
        for key, label, amount_field, rate_note in (
            ("overhead", "Overhead", "overhead_cost", "overhead_rate_percent"),
            ("scrap", "Scrap allowance", "scrap_cost", "scrap_rate_percent"),
            ("tooling", "Tooling", "tooling_cost", None),
        ):
            if additional.get(amount_field) is None:
                rows.append(_not_recorded(key, key, label))
                continue
            note = ""
            if rate_note and additional.get(rate_note) is not None:
                note = f"{additional[rate_note]}% of the direct cost base."
            elif key == "tooling":
                note = "One-off for the batch."
            rows.append(_Row(key, key, label, _dec(additional[amount_field]), note))
    elif part_cost.get("total_additional_cost") is not None:
        rows.append(
            _Row("additional", "additional", "Additional costs",
                 _dec(part_cost["total_additional_cost"]),
                 "Overhead, scrap and tooling were not recorded separately.")
        )
    else:
        rows.append(_not_recorded("additional", "additional", "Additional costs"))

    drivers, share_sum = _partition(rows, total)
    base.status = AVAILABLE
    base.total = float(total)
    base.drivers = drivers
    base.share_total_percent = None if share_sum is None else float(share_sum)
    base.rounding_note = _rounding_note(share_sum)
    return base


# --------------------------------------------------------------------- an assembly


def _assembly_breakdown(estimate_id: str, result: dict) -> CostDriverBreakdown:
    quantity = result.get("assembly_quantity")
    base = CostDriverBreakdown(
        estimate_id=estimate_id,
        estimate_kind=ASSEMBLY,
        status=NOT_AVAILABLE,
        currency=result.get("currency"),
        quantity=quantity,
        basis=(
            "one assembly — the roll-up's per-assembly figures"
            + (
                f"; building {quantity} multiplies every line by {quantity} and leaves every "
                "share unchanged"
                if isinstance(quantity, int) and quantity > 1
                else ""
            )
        ),
    )
    total = _dec(result.get("total_cost"))
    if total is None:
        base.reason = (
            result.get("completeness_note")
            or "This roll-up stored no total, so there is nothing to break down."
        )
        return base
    if result.get("is_aggregatable") is False:
        base.reason = result.get("currency_note") or (
            "The components are priced in more than one currency; nothing is converted, so "
            "no share of one total can be stated."
        )
        return base

    rows: list[_Row] = []
    split = [result.get(field) for field in ("material_cost", "operation_cost", "additional_cost")]
    if result.get("manufactured_cost") is None:
        rows.append(_not_recorded("manufactured", "manufactured", "Manufactured components"))
    elif all(value is not None for value in split):
        rows += [
            _Row("manufactured:material", "manufactured", "Manufactured components — material",
                 _dec(split[0])),
            _Row("manufactured:operations", "manufactured",
                 "Manufactured components — operations", _dec(split[1]), "",
                 [_part("Setup", result.get("setup_cost")), _part("Run", result.get("run_cost"))]),
            _Row("manufactured:additional", "manufactured",
                 "Manufactured components — overhead, scrap and tooling", _dec(split[2]),
                 "Stored together per component, so not split further here."),
        ]
    else:
        rows.append(
            _Row("manufactured", "manufactured", "Manufactured components",
                 _dec(result["manufactured_cost"]),
                 "Material, operations and additional costs were not recorded separately.")
        )
    for key, label, note in (
        ("purchased", "Purchased components", "At the unit cost stated for each."),
        ("standard", "Standard components", "At the configured unit cost."),
    ):
        if result.get(f"{key}_cost") is None:
            rows.append(_not_recorded(key, key, label))
        else:
            rows.append(_Row(key, key, label, _dec(result[f"{key}_cost"]), note))
    if result.get("excluded_cost") is None:
        rows.append(_not_recorded("excluded", "excluded", "Excluded components"))
    else:
        rows.append(
            _Row("excluded", "excluded", "Excluded components", _dec(result["excluded_cost"]),
                 "Excluded by explicit decision; contributes nothing.")
        )

    component_rows: list[_Row] = []
    for line in result.get("lines") or []:
        if not isinstance(line, dict) or line.get("is_assembly"):
            continue
        name = str(line.get("name") or line.get("definition_id") or "component")
        key = f"component:{line.get('definition_id') or name}"
        described = (
            f"{line.get('classification') or 'unclassified'}, "
            f"{line.get('quantity', '?')} per assembly"
        )
        if line.get("costed") and line.get("extended_cost") is not None:
            component_rows.append(
                _Row(key, "component", name, _dec(line["extended_cost"]), described)
            )
        elif line.get("excluded"):
            component_rows.append(
                _Row(key, "component", name, Decimal(0),
                     described + ". Excluded by explicit decision.")
            )
        else:
            reason = line.get("reason_detail") or line.get("reason") or "not costed"
            component_rows.append(
                _Row(key, "component", name, None, f"{described}. Not in the total: {reason}",
                     recorded=False)
            )

    drivers, share_sum = _partition(rows, total)
    components, _ = _partition(component_rows, total, missing_is_in_total=False)
    base.status = AVAILABLE if result.get("is_complete") else PARTIAL
    if base.status == PARTIAL:
        base.reason = result.get("completeness_note") or (
            "The roll-up is not complete, so this breakdown covers only what it costed."
        )
    base.total = float(total)
    base.drivers = drivers
    base.component_drivers = components
    base.share_total_percent = None if share_sum is None else float(share_sum)
    base.rounding_note = _rounding_note(share_sum)
    return base


# --------------------------------------------------------------------- entry


def cost_drivers(estimate_id: str, result_snapshot: dict | None) -> CostDriverBreakdown:
    """The driver table of one stored estimate, from its result snapshot alone."""
    result = result_snapshot if isinstance(result_snapshot, dict) else {}
    if "assembly_name" in result:
        return _assembly_breakdown(estimate_id, result)
    if _part_cost_of(result):
        return _part_breakdown(estimate_id, result)
    return CostDriverBreakdown(
        estimate_id=estimate_id,
        estimate_kind=UNKNOWN,
        status=NOT_AVAILABLE,
        reason=(
            "This estimate's stored result holds neither a part cost nor an assembly roll-up, "
            "so no breakdown can be read from it."
        ),
    )
