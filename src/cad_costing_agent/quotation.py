"""Turning a stored estimate into a business document.

What this is, and what it deliberately is not
---------------------------------------------
A quotation here is a **presentation of an estimate that already exists**. It
reads the snapshots the estimate persisted and arranges them; it calculates no
cost, resolves no rate, reads no CAD file and re-runs nothing. That is the whole
design constraint, and it is what makes the second promise below possible.

This module therefore imports only `estimates` — for the plain `EstimateDetail`
carrier and the assumptions reader that already exists — and `money`. A
structural test asserts it reaches no calculator, no repository, no rate set and
no CAD module. There is no second costing path, because there is nowhere for one
to come from.

Every number is traceable, and an assumption is never a measurement
-------------------------------------------------------------------
The totals are the ones the estimate stored. The cost lines are stored figures
regrouped, and because regrouping is the one place a document could quietly
disagree with the estimate it presents, the lines are chosen so that they
**partition** the total — no overlaps — and any remainder between their sum and
the stored total is reported as `reconciliation` rather than absorbed. That is
the same rule `estimates.py` follows when it decomposes a delta: a gap can never
pass as an explanation.

Where a volume was supplied by a person rather than measured from the file, the
document says so in those words and never calls it a CAD volume. That phrasing
is asserted by a test, because it is the single most consequential sentence in
the document: a mass is a volume times a density, and a price is a mass times a
rate, so an assumed volume silently relabelled as measured makes the entire
quotation a fabrication.

A quotation is refused rather than made misleading
--------------------------------------------------
Some estimates must not become customer-facing documents at all:

- an assembly with components still needing costing input — a partial sum
  presented as a price is worse than no document;
- an assembly priced in more than one currency, because nothing here converts;
- an estimate with no total;
- geometry that still requires user input.

Those produce `available=False` with a `blocker` naming the reason. The
engineering view is still rendered, because the person fixing the problem needs
to see it — only the customer document is withheld.

Customer view and engineering view
----------------------------------
The customer view carries what a buyer needs: subject, quantity, material,
scope, cost categories, total, per-unit, and every warning. The engineering view
adds setups, operations, feature references, profile versions, component
decisions and the system/user corrections. The internal feedback pairs the
learning architecture records are **omitted entirely** from the customer view
rather than merely hidden by the template — a test serializes a customer
quotation and greps it for those keys, because a field a template does not print
is still a field in the JSON someone forwards.

Nothing here is issued twice from different numbers
---------------------------------------------------
`build_quotation` is deterministic: the same estimate and the same metadata
render identically, and `issued_at` is a parameter rather than a clock read, so
a preview and the document it previews cannot differ. Rendering the same
quotation next year reads the same snapshot and produces the same figures, which
is why immutability needs no second copy of the totals.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from cad_costing_agent.estimates import (
    EstimateAssumptions,
    EstimateDetail,
    extract_assumptions,
)
from cad_costing_agent.money import Money

# --------------------------------------------------------------------- views

CUSTOMER = "customer"
ENGINEERING = "engineering"
VIEWS: tuple[str, ...] = (CUSTOMER, ENGINEERING)

PART = "part"
ASSEMBLY = "assembly"

# Where a committed time came from. A label, not a recomputation: the numbers
# come from the operations that actually reached the cost.
TIME_FROM_ENGINEER = "engineer_entered"
TIME_FROM_SYSTEM = "system_estimate_accepted"

# How the volume behind the mass was arrived at. The distinction the whole
# document rests on.
VOLUME_MEASURED = "measured"
VOLUME_ASSUMED = "assumed"
VOLUME_UNKNOWN = "unknown"

ASSUMED_VOLUME_HEADLINE = "Volume assumed, not measured from CAD."

# Beyond this, the difference between the cost lines and the stored total is a
# real gap rather than the rounding of a dozen two-decimal amounts. The same
# allowance the cost-impact decomposition uses, and for the same reason.
ROUNDING_ALLOWANCE = 0.5


class QuoteMetadata(BaseModel):
    """What a person types onto the document. Deliberately small.

    There is no customer record, no address book and no CRM here: these are the
    fields a quotation needs printed on it, and nothing is inferred from them.
    """

    customer_name: str = ""
    project_name: str = ""
    quote_reference: str = Field(
        default="",
        description=(
            "The reference a person wants on the document. When empty the "
            "estimate's own id is used, so a quotation always has one identifier that "
            "leads back to the numbers."
        ),
    )
    prepared_by: str = ""
    notes: str = ""
    validity: str = Field(
        default="",
        description="How long the quotation stands, as written. Never computed into a date.",
    )


class QuoteBlocker(BaseModel):
    """Why this estimate must not become a customer-facing document."""

    code: str
    title: str
    detail: str


class QuoteWarning(BaseModel):
    """Something the reader must be told, whatever the document looks like.

    `severity` is `critical` for anything that changes how a figure should be
    read — an assumed volume, an illustrative rate — and `advisory` for scope
    limits. Neither is ever suppressed to produce a cleaner document.
    """

    code: str
    severity: str = "critical"
    title: str
    detail: str


class QuoteLine(BaseModel):
    """One cost category on the document.

    The lines of a quotation **partition** its cost basis: no line contains any
    part of another, so they sum, and the sum is checked against the figure the
    estimate stored. Informational splits live in `breakdown` instead.
    """

    category: str
    label: str
    amount: Money | None = None
    detail: str = ""


class QuoteTotals(BaseModel):
    """The money, with the basis of every figure stated.

    `lines_total` is what the lines add up to and `lines_basis` says what that
    covers — one part, one assembly, or the whole batch — because a document
    that shows per-unit lines under a batch total is the classic way to be
    wrong by the quantity.
    """

    currency: str
    quantity: int = 1
    quantity_label: str = "off"
    lines: list[QuoteLine] = Field(default_factory=list)
    breakdown: list[QuoteLine] = Field(
        default_factory=list,
        description="Informational splits inside a line. Never added to the total.",
    )
    lines_total: Money | None = None
    lines_basis: str = ""
    total: Money | None = Field(
        default=None, description="Exactly the figure the estimate stored. Never recomputed."
    )
    per_unit: Money | None = None
    reconciliation: str = Field(
        default="",
        description=(
            "Empty when the lines account for the stored total. Otherwise it states the "
            "remainder, so a gap is never presented as an explanation."
        ),
    )
    quantity_note: str = Field(
        default="",
        description=(
            "Set when the per-unit figure multiplied by the quantity does not come to the "
            "total to the paisa. It is the money policy's own consequence — every amount is "
            "rounded from its own exact value, so the total is the rounded true total rather "
            "than the sum of rounded parts — and a document has to say so, because a "
            "procurement reviewer will multiply the two figures and find the difference."
        ),
    )


class QuoteComponentLine(BaseModel):
    """One component of an assembly, as the document shows it."""

    name: str
    part_number: str | None = None
    quantity: int = 0
    classification: str | None = None
    classification_label: str = ""
    unit_cost: Money | None = None
    extended_cost: Money | None = None
    currency: str | None = None
    costed: bool = False
    excluded: bool = False
    status: str = ""
    reason: str = Field(
        default="", description="Why this component contributes nothing, in plain words."
    )
    supplier: str | None = None


class QuoteAssemblySummary(BaseModel):
    """The counts a reviewer needs to trust an assembly total."""

    assembly_name: str = ""
    definition_count: int = 0
    part_definition_count: int = 0
    occurrence_count: int = 0
    part_occurrence_count: int = 0
    costed_definition_count: int = 0
    uncosted_definition_count: int = 0
    excluded_definition_count: int = 0
    manufactured_cost: Money | None = None
    purchased_cost: Money | None = None
    standard_cost: Money | None = None
    excluded_cost: Money = 0.0
    is_complete: bool = False
    completeness_note: str = ""
    sub_assembly_note: str = Field(
        default="",
        description="States that sub-assemblies are containers and are not counted again.",
    )


class QuoteOperation(BaseModel):
    """One costed operation. Every figure is the one that reached the money."""

    operation_id: str
    operation_name: str = ""
    process_type: str = ""
    feature_ids: list[str] = Field(default_factory=list)
    feature_label: str = ""
    setup_time_minutes: float | None = None
    cycle_time_minutes: float | None = None
    time_source: str = ""
    time_source_label: str = ""
    setup_cost: Money | None = None
    run_cost: Money | None = None
    total_cost: Money | None = None
    # The pair, for the engineering view only.
    system_suggested_operation: str | None = None
    system_suggested_cycle_time: float | None = None
    process_changed: bool = False
    time_changed: bool = False


class QuoteSetup(BaseModel):
    """One fixturing, its time charged once, and the operations inside it."""

    setup_id: str
    name: str = ""
    sequence_position: int = 0
    setup_time_minutes: float | None = None
    setup_time_source: str = ""
    operation_count: int = 0
    operations: list[QuoteOperation] = Field(default_factory=list)


class QuoteManufacturing(BaseModel):
    """The concise manufacturing summary the customer document carries."""

    setup_count: int = 0
    operation_count: int = 0
    total_setup_time_minutes: float = 0.0
    total_cycle_time_minutes: float = 0.0
    time_basis: str = Field(
        default="",
        description="States that setup is charged once per setup and cycle time per part.",
    )
    processes: list[str] = Field(default_factory=list)
    uncosted_operation_count: int = 0
    uncosted_note: str = ""


class QuoteProvenance(BaseModel):
    """Where every input came from. The document's audit trail."""

    source_file: str | None = None
    source_format: str | None = None
    geometry_source: str | None = None
    geometry_status: str | None = None

    volume_mm3: float | None = None
    volume_basis: str = VOLUME_UNKNOWN
    volume_method: str | None = None
    volume_statement: str = Field(
        default="",
        description=(
            "How the volume is described on the document. For a supplied volume this "
            "leads with 'Volume assumed, not measured from CAD.' and never says 'CAD volume'."
        ),
    )
    user_supplied_volume_mm3: float | None = None
    volume_used_mm3: float | None = None

    material: str | None = None
    density_kg_m3: float | None = None
    material_cost_per_kg: float | None = None
    mass_kg: float | None = None
    # When material was costed from the stock the part is cut from: what stock,
    # and its mass. `mass_kg` stays the finished part's own.
    material_basis: str | None = None
    stock_description: str | None = None
    stock_mass_kg: float | None = None

    rate_set_id: str | None = None
    rate_set_version: str | None = None
    rate_set_name: str | None = None
    rate_set_is_illustrative: bool | None = None

    time_profile_id: str | None = None
    time_profile_version: str | None = None
    time_profile_name: str | None = None
    time_profile_is_illustrative: bool | None = None

    plan_state: str = Field(
        default="",
        description="Whether a manufacturing plan produced the times, and in what condition.",
    )
    overhead_rate_percent: float | None = None
    scrap_rate_percent: float | None = None
    tooling_cost_per_batch: float | None = None


class QuoteComponentDecision(BaseModel):
    """What the system proposed about a component and what a person chose."""

    name: str
    quantity: int = 0
    system_classification: str | None = None
    user_classification: str | None = None
    final_classification: str | None = None
    changed: bool = False
    reason: str = ""
    material: str | None = None
    material_source: str = ""
    unit_cost: Money | None = None
    currency: str | None = None
    supplier: str | None = None
    supplier_part_number: str | None = None


class QuoteEngineeringDetail(BaseModel):
    """The appendix. Present only in the engineering view.

    Everything here is read out of the estimate's own snapshots. The customer
    view does not carry this object at all, so the internal correction records
    cannot travel in a document meant to leave the building.
    """

    setups: list[QuoteSetup] = Field(default_factory=list)
    operations: list[QuoteOperation] = Field(default_factory=list)
    sequence: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    time_assumptions: dict = Field(default_factory=dict)
    component_decisions: list[QuoteComponentDecision] = Field(default_factory=list)
    corrections: list[str] = Field(
        default_factory=list,
        description="System/user differences, as sentences rather than raw feedback rows.",
    )
    rate_set_provenance: dict = Field(default_factory=dict)
    time_profile_provenance: dict = Field(default_factory=dict)
    geometry: dict = Field(default_factory=dict)


class QuoteRevision(BaseModel):
    """This quotation against an earlier estimate of the same subject.

    Figures only. No causal claim is made here — the cost-impact analysis is a
    separate thing a reader can open, and repeating a guess at *why* on a
    customer document would be exactly the speculation this project avoids.
    """

    previous_estimate_id: str
    previous_total: Money | None = None
    current_total: Money | None = None
    change: Money | None = None
    currency: str | None = None
    comparable: bool = True
    note: str = ""


class QuoteSubject(BaseModel):
    """What is being quoted."""

    kind: str = PART
    name: str = ""
    reference: str = ""
    model_reference: str | None = None
    quantity: int = 1


class Quotation(BaseModel):
    """A quotation, rendered entirely from one estimate's stored snapshots."""

    estimate_id: str
    quote_reference: str
    view: str = CUSTOMER
    estimate_created_at: datetime | None = None
    issued_at: datetime | None = Field(
        default=None,
        description="Null for an unissued preview. Passed in rather than read from a clock, "
        "so a preview and the document it previews are identical.",
    )

    metadata: QuoteMetadata = Field(default_factory=QuoteMetadata)
    subject: QuoteSubject = Field(default_factory=QuoteSubject)

    available: bool = True
    blockers: list[QuoteBlocker] = Field(default_factory=list)

    totals: QuoteTotals | None = None
    components: list[QuoteComponentLine] = Field(default_factory=list)
    assembly: QuoteAssemblySummary | None = None
    manufacturing: QuoteManufacturing | None = None
    provenance: QuoteProvenance = Field(default_factory=QuoteProvenance)
    warnings: list[QuoteWarning] = Field(default_factory=list)
    scope_notes: list[str] = Field(default_factory=list)
    revision: QuoteRevision | None = None
    engineering: QuoteEngineeringDetail | None = None

    basis_note: str = Field(
        default="",
        description="States that every figure is read from the estimate and nothing is recomputed.",
    )


BASIS_NOTE = (
    "Every figure on this document is read from the estimate it was made from, exactly as that "
    "estimate recorded it. Nothing is recalculated when the document is produced, so publishing "
    "a newer rate set or time profile cannot change this quotation."
)

SUB_ASSEMBLY_NOTE = (
    "A sub-assembly is a container, not a part. Its cost is the sum of the components inside it "
    "and is not counted again, which would double every part it holds."
)

TIME_BASIS_NOTE = (
    "Setup time is charged once per setup however many operations share it; cycle time is "
    "charged per part. The two are reported separately rather than combined."
)

CLASSIFICATION_LABELS = {
    "manufactured": "Manufactured",
    "purchased": "Purchased",
    "standard": "Standard",
    "excluded": "Excluded",
    "unresolved": "Unresolved",
}

TIME_SOURCE_LABELS = {
    TIME_FROM_ENGINEER: "entered by the engineer",
    TIME_FROM_SYSTEM: "system estimate, accepted",
}


# --------------------------------------------------------------------- reading


def _part_cost_of(result: dict) -> dict:
    """The part-cost block, wherever this estimate's source put it.

    `/cost/cad-part` nests it under `part_cost`; `/cost/part` stores it at the
    top level. Both are read rather than one being assumed.
    """
    nested = result.get("part_cost")
    if isinstance(nested, dict) and nested:
        return nested
    if "total_part_cost" in result:
        return result
    return {}


def _kind_of(detail: EstimateDetail) -> str:
    return ASSEMBLY if "assembly_name" in (detail.result_snapshot or {}) else PART


def _money(value) -> float | None:
    return None if value is None else float(value)


# --------------------------------------------------------------------- blockers


def _blockers(detail: EstimateDetail, kind: str, assumptions: EstimateAssumptions):
    result = detail.result_snapshot or {}
    request = detail.request_snapshot or {}
    found: list[QuoteBlocker] = []

    if kind == ASSEMBLY:
        needs_input = int(result.get("needs_input_definition_count") or 0)
        if needs_input:
            found.append(
                QuoteBlocker(
                    code="assembly_incomplete",
                    title="Quotation unavailable",
                    detail=(
                        f"{needs_input} component(s) still require costing input. A partial sum "
                        "must not be presented as a price, so no customer quotation is produced "
                        "until every component is costed or explicitly excluded."
                    ),
                )
            )
        # Costed, but with nothing charged for making it. Coverage is complete
        # and the arithmetic is right, which is exactly why this needs saying:
        # the figure looks finished. It is refused here for the same reason a
        # partial sum is — a document that goes to a customer must not be a
        # cost with the manufacturing left out.
        unplanned = int(result.get("unplanned_definition_count") or 0)
        if unplanned:
            found.append(
                QuoteBlocker(
                    code="assembly_manufacturing_incomplete",
                    title="Quotation unavailable",
                    detail=(
                        f"{unplanned} manufactured component(s) have committed no manufacturing "
                        "time, so this total carries their material and tooling and nothing for "
                        "making them. Review each component's manufacturing plan, accept or "
                        "enter its times, and cost the assembly again."
                    ),
                )
            )
        if result.get("is_aggregatable") is False:
            currencies = ", ".join(result.get("currencies_present") or [])
            found.append(
                QuoteBlocker(
                    code="mixed_currency",
                    title="Quotation unavailable",
                    detail=(
                        f"This assembly's components are priced in more than one currency "
                        f"({currencies}). Nothing here converts between currencies, so there is "
                        "no aggregated total to quote. Price every component in one currency "
                        "first."
                    ),
                )
            )
        if result.get("total_cost") is None and not needs_input:
            found.append(
                QuoteBlocker(
                    code="no_total",
                    title="Quotation unavailable",
                    detail="This estimate has no total, so there is nothing to quote.",
                )
            )
    else:
        part_cost = _part_cost_of(result)
        if part_cost.get("total_part_cost") is None:
            found.append(
                QuoteBlocker(
                    code="no_total",
                    title="Quotation unavailable",
                    detail="This estimate has no total, so there is nothing to quote.",
                )
            )

    # Geometry that was never resolved. `/cost/cad-part` refuses such a request
    # outright, so this is a guard on the stored record rather than a path a
    # current upload can reach — and it is checked because a document is the
    # last place an unresolved volume should first become visible.
    geometry = (result.get("geometry") or request.get("geometry") or {})
    completion = request.get("geometry_completion")
    if geometry:
        status = geometry.get("geometry_status")
        volume = geometry.get("volume")
        if status == "incomplete_requires_input" or (not volume and not completion):
            found.append(
                QuoteBlocker(
                    code="geometry_requires_input",
                    title="Quotation unavailable",
                    detail=(
                        "Geometry requires user input before a complete cost can be quoted. The "
                        "CAD file does not yield a volume for this part and none was supplied, "
                        "so there is no mass, and therefore no material cost, to quote."
                    ),
                )
            )
    return found


# --------------------------------------------------------------------- warnings


def _warnings(detail: EstimateDetail, kind: str, assumptions: EstimateAssumptions):
    result = detail.result_snapshot or {}
    request = detail.request_snapshot or {}
    found: list[QuoteWarning] = []

    if assumptions.volume_basis_is_assumed:
        found.append(
            QuoteWarning(
                code="assumed_volume",
                severity="critical",
                title=ASSUMED_VOLUME_HEADLINE,
                detail=(
                    "The volume behind this price was supplied by a person because the CAD file "
                    "could not be measured. Mass, material cost and every figure derived from "
                    "them rest on that stated assumption, not on a measurement."
                ),
            )
        )

    if assumptions.rate_set_is_illustrative:
        found.append(
            QuoteWarning(
                code="illustrative_rates",
                severity="critical",
                title="Illustrative rates — not a commercial price",
                detail=(
                    f"The rate set behind these figures "
                    f"({assumptions.rate_set_name or assumptions.rate_set_id}) is marked "
                    "illustrative: its material prices and machine rates are placeholders for "
                    "demonstration, not this shop's actual costs. Publish a rate set with real "
                    "rates before quoting commercially."
                ),
            )
        )

    if assumptions.time_profile_is_illustrative:
        found.append(
            QuoteWarning(
                code="illustrative_time_profile",
                severity="critical",
                title="Illustrative machining-time assumptions",
                detail=(
                    f"The time profile behind the operation times "
                    f"({assumptions.time_profile_name or assumptions.time_profile_id}) is marked "
                    "illustrative. The feeds, speeds and allowances are demonstration values, "
                    "not measured shop data."
                ),
            )
        )

    geometry = result.get("geometry") or request.get("geometry") or {}
    if geometry.get("volume_confidence") == "repaired":
        found.append(
            QuoteWarning(
                code="repaired_geometry",
                severity="advisory",
                title="Geometry was repaired before measurement",
                detail=(
                    "The file's surfaces did not close on their own and were sewn into a solid "
                    "before the volume was integrated. The volume is a measurement, but of "
                    "repaired topology rather than of the file as written."
                ),
            )
        )
    if geometry.get("geometry_source") in ("mesh", "faceted"):
        found.append(
            QuoteWarning(
                code="reconstructed_geometry",
                severity="advisory",
                title="Volume derived from polygons, not exact surfaces",
                detail=(
                    "This file carries flat polygons rather than analytic surfaces, so the "
                    "volume is the enclosed polygon volume. It is a measurement of what the "
                    "file contains, which is an approximation of the shape the designer drew."
                ),
            )
        )

    uncosted = (detail.plan_snapshot or {}).get("uncosted_operation_ids") or []
    if uncosted:
        found.append(
            QuoteWarning(
                code="partial_costing",
                severity="critical",
                title=f"{len(uncosted)} operation(s) carry no committed time",
                detail=(
                    "Those operations were left out of this price rather than defaulted to zero, "
                    "so the manufacturing cost covers less work than the plan describes. "
                    f"Omitted: {', '.join(uncosted)}."
                ),
            )
        )

    if kind == ASSEMBLY:
        excluded = int(result.get("excluded_definition_count") or 0)
        if excluded:
            found.append(
                QuoteWarning(
                    code="excluded_components",
                    severity="advisory",
                    title=f"{excluded} component(s) deliberately excluded",
                    detail=(
                        "These components were excluded from the manufacturing cost by an "
                        "explicit decision and contribute nothing. They are listed with their "
                        "stated reason, so the exclusion reads as a choice rather than an "
                        "omission."
                    ),
                )
            )

    return found


# --------------------------------------------------------------------- totals


def _line(category: str, label: str, amount, detail: str = "") -> QuoteLine:
    return QuoteLine(category=category, label=label, amount=_money(amount), detail=detail)


def _part_totals(detail: EstimateDetail, assumptions: EstimateAssumptions) -> QuoteTotals:
    """Cost lines for a part: stored figures, regrouped so they partition."""
    part_cost = _part_cost_of(detail.result_snapshot or {})
    operations = part_cost.get("operations") or []
    additional = part_cost.get("additional_costs") or {}
    quantity = int(part_cost.get("material", {}).get("quantity") or assumptions.quantity or 1)

    # Setup and run are separated because they answer different questions and
    # both figures already exist per operation. `setup_cost` is non-zero only on
    # the first costable operation of each setup, so summing it gives the setup
    # cost of the whole part rather than one per operation.
    setup_cost = sum(float(op.get("setup_cost") or 0.0) for op in operations)
    run_cost = sum(
        float(op.get("machine_cost") or 0.0) + float(op.get("labor_cost") or 0.0)
        for op in operations
    )

    # Non-machining route stages, present only when the estimate priced some, so
    # a document for an estimate without them has exactly the lines it always had.
    stage_costs = part_cost.get("stage_costs") or []
    stage_lines = (
        [
            _line(
                "route_stages",
                "Route stages (" + ", ".join(str(s.get("label")) for s in stage_costs) + ")",
                part_cost.get("total_stage_cost"),
                # A stage whose tool charge was amortised carries only a share
                # of it; the line says so in the estimate's own stored words.
                " ".join(
                    [
                        "Priced from the rate set's stage rates.",
                        *(
                            f"{s.get('label')}: {s.get('batch_charge_note')}"
                            for s in stage_costs
                            if s.get("batch_charge_note")
                        ),
                    ]
                ),
            )
        ]
        if stage_costs
        else []
    )

    lines = [
        _line(
            "material",
            "Material",
            part_cost.get("total_material_cost"),
            part_cost.get("material_note") or "",
        ),
        *stage_lines,
        _line(
            "operation_setup",
            "Manufacturing — setup",
            setup_cost,
            "Charged once per setup, not per operation.",
        ),
        _line("operation_run", "Manufacturing — run", run_cost, "Cycle time x quantity."),
        _line("overhead", "Overhead", additional.get("overhead_cost")),
        _line("scrap", "Scrap allowance", additional.get("scrap_cost")),
        _line("tooling", "Tooling", additional.get("tooling_cost")),
    ]

    totals = QuoteTotals(
        currency=part_cost.get("currency") or assumptions.currency or "",
        quantity=quantity,
        quantity_label="off",
        lines=lines,
        breakdown=[
            _line(
                "operations_total",
                "Manufacturing, total",
                part_cost.get("total_operation_cost"),
                "Setup and run together.",
            ),
            _line(
                "additional_total",
                "Additional costs, total",
                part_cost.get("total_additional_cost"),
                "Overhead, scrap and tooling together.",
            ),
            _line("direct_cost_base", "Direct cost base", part_cost.get("direct_cost_base")),
        ],
        lines_total=_money(part_cost.get("total_part_cost")),
        lines_basis=f"the whole batch of {quantity}",
        total=_money(part_cost.get("total_part_cost")),
        per_unit=_money(part_cost.get("per_unit_cost")),
    )
    _reconcile(totals)
    return totals


def _assembly_totals(detail: EstimateDetail, assumptions: EstimateAssumptions) -> QuoteTotals:
    """Cost lines for an assembly: one per classification, plus the run.

    The lines cover **one assembly**, which is what the roll-up's own
    per-classification figures cover. The production total is stated separately
    rather than scaling the lines, so the quantity is applied in exactly one
    visible place.
    """
    result = detail.result_snapshot or {}
    quantity = int(result.get("assembly_quantity") or 1)

    lines = [
        _line(
            "manufactured",
            "Manufactured components",
            result.get("manufactured_cost"),
            "Costed from geometry, material and operations.",
        ),
        _line(
            "purchased",
            "Purchased components",
            result.get("purchased_cost"),
            "At the unit cost stated for each. No price is looked up.",
        ),
        _line(
            "standard",
            "Standard components",
            result.get("standard_cost"),
            "Catalogue items at their configured unit cost.",
        ),
        _line(
            "excluded",
            "Excluded components",
            result.get("excluded_cost") or 0.0,
            "Excluded by explicit decision; contributes nothing.",
        ),
    ]

    per_assembly = _money(result.get("total_cost"))
    production = _money(result.get("production_total_cost"))
    totals = QuoteTotals(
        currency=result.get("currency") or "",
        quantity=quantity,
        quantity_label="assemblies" if quantity != 1 else "assembly",
        lines=lines,
        breakdown=[
            _line(
                "material",
                "of which material",
                result.get("material_cost"),
                "Inside the manufactured components.",
            ),
            _line(
                "operations",
                "of which operations",
                result.get("operation_cost"),
                "Inside the manufactured components.",
            ),
            _line(
                "operation_setup",
                "of which setup",
                result.get("setup_cost"),
                "Charged once per setup, per component.",
            ),
            _line(
                "operation_run",
                "of which run",
                result.get("run_cost"),
                "Cycle time x quantity, per component.",
            ),
        ],
        lines_total=per_assembly,
        lines_basis="one assembly",
        total=production if production is not None else per_assembly,
        per_unit=per_assembly,
    )
    _reconcile(totals)
    return totals


def _quantity_note(totals: QuoteTotals) -> None:
    """State the rounding difference a reader will find by multiplying.

    A quotation shows a per-unit figure beside a total, and anyone checking it
    multiplies one by the other. Both are rounded from their own exact values —
    that is the project's money policy, chosen so the total is the rounded true
    total rather than the sum of rounded parts — so the product can be a few
    paisa out. Left unstated that reads as an arithmetic error on a document
    somebody is about to pay against.
    """
    if totals.total is None or totals.per_unit is None or totals.quantity < 2:
        return
    difference = totals.total - totals.per_unit * totals.quantity
    if abs(difference) < 0.005:
        return
    totals.quantity_note = (
        f"The per-unit figure multiplied by {totals.quantity} comes to "
        f"{totals.per_unit * totals.quantity:,.2f}, which differs from the total by "
        f"{difference:,.2f}. Every amount here is rounded to two decimals from its own exact "
        "value, so the total is the correctly rounded true total rather than the sum of "
        "rounded parts. No figure has been adjusted to make the two agree."
    )


def _reconcile(totals: QuoteTotals) -> None:
    """Check the lines against the figure they are meant to account for.

    A remainder is stated, never absorbed. Two decimals rounded a dozen times
    can drift by pennies; anything more is a real gap and says so.
    """
    _quantity_note(totals)
    if totals.lines_total is None:
        return
    summed = sum(line.amount or 0.0 for line in totals.lines)
    remainder = totals.lines_total - summed
    if abs(remainder) < 0.005:
        return
    if abs(remainder) <= ROUNDING_ALLOWANCE:
        totals.reconciliation = (
            f"The lines above sum to {summed:,.2f} against a recorded {totals.lines_total:,.2f}; "
            f"the {remainder:,.2f} difference is the rounding of individually rounded amounts."
        )
    else:
        totals.reconciliation = (
            f"The lines above sum to {summed:,.2f} but the estimate recorded "
            f"{totals.lines_total:,.2f}, leaving {remainder:,.2f} unaccounted for. The recorded "
            "total is the authority; this difference is reported rather than absorbed."
        )


# --------------------------------------------------------------------- components


def _components(detail: EstimateDetail) -> list[QuoteComponentLine]:
    result = detail.result_snapshot or {}
    request = detail.request_snapshot or {}
    suppliers = {
        entry.get("definition_id"): entry.get("supplier")
        for entry in (request.get("component_costing") or [])
    }

    rows: list[QuoteComponentLine] = []
    for line in result.get("lines") or []:
        if line.get("is_assembly"):
            continue
        classification = line.get("classification")
        rows.append(
            QuoteComponentLine(
                name=line.get("name", ""),
                part_number=line.get("part_number"),
                quantity=int(line.get("quantity") or 0),
                classification=classification,
                classification_label=CLASSIFICATION_LABELS.get(
                    classification or "", classification or ""
                ),
                unit_cost=_money(line.get("per_unit_cost")),
                extended_cost=_money(line.get("extended_cost")),
                currency=line.get("currency"),
                costed=bool(line.get("costed")),
                excluded=bool(line.get("excluded")),
                status=line.get("costing_status", ""),
                reason=line.get("reason_detail", ""),
                supplier=suppliers.get(line.get("definition_id")),
            )
        )
    return rows


def _assembly_summary(detail: EstimateDetail) -> QuoteAssemblySummary:
    result = detail.result_snapshot or {}
    return QuoteAssemblySummary(
        assembly_name=result.get("assembly_name", ""),
        definition_count=int(result.get("definition_count") or 0),
        part_definition_count=int(result.get("part_definition_count") or 0),
        occurrence_count=int(result.get("occurrence_count") or 0),
        part_occurrence_count=int(result.get("part_occurrence_count") or 0),
        costed_definition_count=int(result.get("costed_definition_count") or 0),
        uncosted_definition_count=int(result.get("uncosted_definition_count") or 0),
        excluded_definition_count=int(result.get("excluded_definition_count") or 0),
        manufactured_cost=_money(result.get("manufactured_cost")),
        purchased_cost=_money(result.get("purchased_cost")),
        standard_cost=_money(result.get("standard_cost")),
        excluded_cost=_money(result.get("excluded_cost")) or 0.0,
        is_complete=bool(result.get("is_complete")),
        completeness_note=result.get("completeness_note", ""),
        sub_assembly_note=SUB_ASSEMBLY_NOTE,
    )


# --------------------------------------------------------------------- manufacturing


def _costed_operations(detail: EstimateDetail) -> list[dict]:
    return _part_cost_of(detail.result_snapshot or {}).get("operations") or []


def _plan_operations(detail: EstimateDetail) -> dict[str, dict]:
    plan = (detail.plan_snapshot or {}).get("plan") or {}
    return {op.get("operation_id"): op for op in (plan.get("operations") or [])}


def _operation_rows(detail: EstimateDetail) -> list[QuoteOperation]:
    """The costed operations, with where their time came from.

    The numbers are the ones that reached the cost. The *source* label is read
    off the plan and is deliberately a two-branch test: an operation only
    appears here if it had a committed time, so if the engineer did not type it,
    it was the system's estimate being accepted. That avoids restating the
    three-branch resolution rule a third time, where it could drift out of
    agreement with the plan and the browser.
    """
    planned = _plan_operations(detail)
    rows: list[QuoteOperation] = []
    for op in _costed_operations(detail):
        origin = planned.get(op.get("operation_id")) or {}
        by_user = origin.get("user_cycle_time") is not None
        source = TIME_FROM_ENGINEER if by_user else TIME_FROM_SYSTEM
        machine = float(op.get("machine_cost") or 0.0)
        labor = float(op.get("labor_cost") or 0.0)
        rows.append(
            QuoteOperation(
                operation_id=op.get("operation_id", ""),
                operation_name=op.get("operation_name", ""),
                process_type=op.get("process_type", ""),
                feature_ids=list(origin.get("feature_ids") or []),
                feature_label=origin.get("feature_label", ""),
                setup_time_minutes=op.get("setup_time_minutes"),
                cycle_time_minutes=op.get("cycle_time_minutes"),
                time_source=source if planned else "",
                time_source_label=TIME_SOURCE_LABELS.get(source, "") if planned else "",
                setup_cost=_money(op.get("setup_cost")),
                run_cost=machine + labor,
                total_cost=_money(op.get("total_operation_cost")),
                system_suggested_operation=origin.get("system_suggested_operation"),
                system_suggested_cycle_time=origin.get("system_suggested_cycle_time"),
                process_changed=bool(
                    origin.get("user_operation_type")
                    and origin.get("system_suggested_operation")
                    and origin["user_operation_type"] != origin["system_suggested_operation"]
                ),
                time_changed=by_user
                and origin.get("system_suggested_cycle_time") is not None
                and origin.get("user_cycle_time") != origin.get("system_suggested_cycle_time"),
            )
        )
    return rows


def _setup_count(detail: EstimateDetail, assumptions: EstimateAssumptions, rows) -> int:
    """How many setups were actually charged.

    A plan states its setups. An estimate made from hand-entered operation
    requirements has no plan, and its setup time is carried on the requirements
    themselves — so the count comes from the operations that carry a non-zero
    setup time. Reporting the plan's zero beside twenty minutes of setup time
    would put a contradiction on the document.
    """
    if assumptions.setup_count:
        return assumptions.setup_count
    return sum(1 for row in rows if (row.setup_time_minutes or 0.0) > 0.0)


def _manufacturing(detail: EstimateDetail, assumptions: EstimateAssumptions) -> QuoteManufacturing:
    rows = _operation_rows(detail)
    uncosted = (detail.plan_snapshot or {}).get("uncosted_operation_ids") or []
    return QuoteManufacturing(
        setup_count=_setup_count(detail, assumptions, rows),
        operation_count=len(rows),
        total_setup_time_minutes=assumptions.total_setup_time_minutes,
        total_cycle_time_minutes=assumptions.total_cycle_time_minutes,
        time_basis=TIME_BASIS_NOTE,
        processes=sorted({row.process_type for row in rows if row.process_type}),
        uncosted_operation_count=len(uncosted),
        uncosted_note=(
            f"{len(uncosted)} planned operation(s) had no committed cycle time and were left out "
            "of the cost rather than defaulted."
            if uncosted
            else ""
        ),
    )


def _setups(detail: EstimateDetail, rows: list[QuoteOperation]) -> list[QuoteSetup]:
    plan = (detail.plan_snapshot or {}).get("plan") or {}
    planned = _plan_operations(detail)
    by_id = {row.operation_id: row for row in rows}

    setups: list[QuoteSetup] = []
    for setup in sorted(plan.get("setups") or [], key=lambda s: s.get("sequence_position", 0)):
        setup_id = setup.get("setup_id", "")
        members = [
            by_id[op_id]
            for op_id, origin in planned.items()
            if (origin.get("user_setup_id") or origin.get("system_setup_id")) == setup_id
            and op_id in by_id
        ]
        user_time = setup.get("user_setup_time_minutes")
        accepted = setup.get("system_setup_time_accepted")
        setups.append(
            QuoteSetup(
                setup_id=setup_id,
                name=setup.get("name", ""),
                sequence_position=int(setup.get("sequence_position") or 0),
                setup_time_minutes=(
                    user_time
                    if user_time is not None
                    else (setup.get("system_setup_time_minutes") if accepted else None)
                ),
                setup_time_source=(
                    TIME_SOURCE_LABELS[TIME_FROM_ENGINEER]
                    if user_time is not None
                    else (TIME_SOURCE_LABELS[TIME_FROM_SYSTEM] if accepted else "no committed time")
                ),
                operation_count=len(members),
                operations=sorted(members, key=lambda row: row.operation_id),
            )
        )
    return setups


# --------------------------------------------------------------------- provenance


def _plan_state(detail: EstimateDetail, kind: str) -> str:
    """What produced the times, in words that fit what was actually costed.

    An assembly has no plan of its own: each manufactured component went
    through the part pipeline. Reporting "no operations were costed; this is a
    material-only estimate" — the honest answer for a *part* with no
    operations — would be plainly wrong on an assembly whose components were
    costed with material and machining.
    """
    plan = (detail.plan_snapshot or {}).get("plan") or {}
    if plan:
        return (
            f"Manufacturing plan with {len(plan.get('setups') or [])} setup(s) and "
            f"{len(plan.get('operations') or [])} planned operation(s), of which "
            f"{len(_costed_operations(detail))} carried a committed time and were costed."
        )
    if kind == ASSEMBLY:
        result = detail.result_snapshot or {}
        return (
            f"No assembly-level plan: each of the "
            f"{int(result.get('costed_definition_count') or 0)} costed component(s) was costed "
            "in its own right, and this application calculates no assembly labour or "
            "fastening time."
        )
    if _costed_operations(detail):
        return "Operations were supplied directly rather than through a manufacturing plan."
    return "No manufacturing operations were costed; this is a material-only estimate."


def _provenance(
    detail: EstimateDetail, assumptions: EstimateAssumptions, kind: str = PART
) -> QuoteProvenance:
    result = detail.result_snapshot or {}
    request = detail.request_snapshot or {}
    geometry = result.get("geometry") or request.get("geometry") or {}
    completion = request.get("geometry_completion") or {}

    assumed = assumptions.volume_basis_is_assumed
    if assumed:
        supplied = completion.get("volume_mm3")
        used = completion.get("resulting_volume_mm3") or geometry.get("volume")
        statement = (
            f"{ASSUMED_VOLUME_HEADLINE} User-supplied assumption: "
            f"{supplied:,.1f} mm³. Volume used: {used:,.1f} mm³."
            if supplied is not None and used is not None
            else ASSUMED_VOLUME_HEADLINE
        )
    else:
        supplied = None
        used = geometry.get("volume")
        statement = (
            f"Volume measured from the CAD geometry: {used:,.1f} mm³."
            if used is not None
            else ""
        )

    return QuoteProvenance(
        source_file=assumptions.source_file,
        source_format=assumptions.source_format,
        geometry_source=assumptions.geometry_source,
        geometry_status=assumptions.geometry_status,
        volume_mm3=assumptions.volume_mm3,
        volume_basis=VOLUME_ASSUMED if assumed else (VOLUME_MEASURED if used else VOLUME_UNKNOWN),
        volume_method=assumptions.volume_method,
        volume_statement=statement,
        user_supplied_volume_mm3=supplied,
        volume_used_mm3=used,
        material=assumptions.material,
        density_kg_m3=assumptions.density_kg_m3,
        material_cost_per_kg=assumptions.material_cost_per_kg,
        mass_kg=assumptions.mass_kg,
        material_basis=assumptions.material_basis,
        stock_description=assumptions.stock_description,
        stock_mass_kg=assumptions.stock_mass_kg,
        rate_set_id=assumptions.rate_set_id,
        rate_set_version=assumptions.rate_set_version,
        rate_set_name=assumptions.rate_set_name,
        rate_set_is_illustrative=assumptions.rate_set_is_illustrative,
        time_profile_id=assumptions.time_profile_id,
        time_profile_version=assumptions.time_profile_version,
        time_profile_name=assumptions.time_profile_name,
        time_profile_is_illustrative=assumptions.time_profile_is_illustrative,
        plan_state=_plan_state(detail, kind),
        overhead_rate_percent=assumptions.overhead_rate_percent,
        scrap_rate_percent=assumptions.scrap_rate_percent,
        tooling_cost_per_batch=assumptions.tooling_cost_per_batch,
    )


# --------------------------------------------------------------------- engineering


def _corrections(detail: EstimateDetail, rows: list[QuoteOperation]) -> list[str]:
    """System/user differences as sentences.

    Written out rather than passed through as feedback rows: the appendix is for
    a person reviewing a quotation, and the raw records exist for a purpose this
    document is not part of.
    """
    sentences = []
    for row in rows:
        if row.process_changed:
            sentences.append(
                f"{row.operation_id}: the system suggested {row.system_suggested_operation} and "
                f"the engineer chose {row.process_type}."
            )
        if row.time_changed:
            sentences.append(
                f"{row.operation_id}: the system estimated "
                f"{row.system_suggested_cycle_time} min of cycle time and the engineer entered "
                f"{row.cycle_time_minutes} min."
            )
    for entry in (detail.result_snapshot or {}).get("classification_feedback") or []:
        if entry.get("classification_changed"):
            sentences.append(
                f"{entry.get('component_name')}: the system suggested "
                f"{entry.get('system_classification')} and the engineer chose "
                f"{entry.get('user_classification')}."
            )
    return sentences


def _component_decisions(detail: EstimateDetail) -> list[QuoteComponentDecision]:
    decisions = []
    for entry in (detail.request_snapshot or {}).get("component_costing") or []:
        if entry.get("is_assembly"):
            continue
        decisions.append(
            QuoteComponentDecision(
                name=entry.get("name", ""),
                quantity=int(entry.get("quantity") or 0),
                system_classification=entry.get("system_classification"),
                user_classification=entry.get("user_classification"),
                final_classification=entry.get("classification"),
                changed=bool(entry.get("classification_changed")),
                reason=entry.get("classification_reason", ""),
                material=entry.get("material"),
                material_source=entry.get("material_source", ""),
                unit_cost=_money(entry.get("unit_cost")),
                currency=entry.get("currency"),
                supplier=entry.get("supplier"),
                supplier_part_number=entry.get("supplier_part_number"),
            )
        )
    return decisions


def _engineering(
    detail: EstimateDetail, assumptions: EstimateAssumptions
) -> QuoteEngineeringDetail:
    rows = _operation_rows(detail)
    result = detail.result_snapshot or {}
    request = detail.request_snapshot or {}
    geometry = result.get("geometry") or request.get("geometry") or {}

    features = sorted({fid for row in rows for fid in row.feature_ids})

    return QuoteEngineeringDetail(
        setups=_setups(detail, rows),
        operations=rows,
        sequence=list(assumptions.sequence),
        features=features,
        time_assumptions=(detail.time_profile_snapshot or {}),
        component_decisions=_component_decisions(detail),
        corrections=_corrections(detail, rows),
        rate_set_provenance={
            "rate_set_id": assumptions.rate_set_id,
            "version": assumptions.rate_set_version,
            "name": assumptions.rate_set_name,
            "is_illustrative": assumptions.rate_set_is_illustrative,
            "currency": assumptions.currency,
        },
        time_profile_provenance={
            "time_profile_id": assumptions.time_profile_id,
            "version": assumptions.time_profile_version,
            "name": assumptions.time_profile_name,
            "is_illustrative": assumptions.time_profile_is_illustrative,
        },
        geometry=geometry,
    )


# --------------------------------------------------------------------- the document


def build_quotation(
    detail: EstimateDetail,
    *,
    metadata: QuoteMetadata | None = None,
    view: str = CUSTOMER,
    issued_at: datetime | None = None,
    created_at: datetime | None = None,
    scope_notes: list[str] | None = None,
    revision: QuoteRevision | None = None,
) -> Quotation:
    """Render one estimate as a quotation.

    `detail` is the estimate's own persisted snapshots. Nothing else is read and
    nothing is recalculated, which is what makes a quotation rendered today and
    the same quotation rendered next year identical.

    `issued_at` is a parameter rather than a clock read so that a preview and
    the document it previews cannot differ by a timestamp.
    """
    if view not in VIEWS:
        raise ValueError(f"Unknown quotation view '{view}'. Known: {', '.join(VIEWS)}.")

    metadata = metadata or QuoteMetadata()
    assumptions = extract_assumptions(detail)
    kind = _kind_of(detail)
    result = detail.result_snapshot or {}
    request = detail.request_snapshot or {}

    blockers = _blockers(detail, kind, assumptions)
    warnings = _warnings(detail, kind, assumptions)

    if kind == ASSEMBLY:
        totals = _assembly_totals(detail, assumptions)
        subject = QuoteSubject(
            kind=ASSEMBLY,
            name=result.get("assembly_name", "") or assumptions.source_file or "",
            reference=metadata.quote_reference or detail.estimate_id,
            model_reference=assumptions.source_file,
            quantity=totals.quantity,
        )
    else:
        totals = _part_totals(detail, assumptions)
        part_cost = _part_cost_of(result)
        subject = QuoteSubject(
            kind=PART,
            name=part_cost.get("part_name") or request.get("part_name") or "",
            reference=metadata.quote_reference or detail.estimate_id,
            model_reference=assumptions.source_file,
            quantity=totals.quantity,
        )

    notes = list(scope_notes or [])
    for note in result.get("notes") or []:
        if note not in notes:
            notes.append(note)

    document = Quotation(
        estimate_id=detail.estimate_id,
        quote_reference=metadata.quote_reference or detail.estimate_id,
        view=view,
        estimate_created_at=created_at,
        issued_at=issued_at,
        metadata=metadata,
        subject=subject,
        available=not blockers,
        blockers=blockers,
        totals=totals,
        components=_components(detail) if kind == ASSEMBLY else [],
        assembly=_assembly_summary(detail) if kind == ASSEMBLY else None,
        manufacturing=_manufacturing(detail, assumptions),
        provenance=_provenance(detail, assumptions, kind),
        warnings=warnings,
        scope_notes=notes,
        revision=revision,
        # Absent, not merely unrendered: the customer document must not carry
        # the internal correction records at all.
        engineering=_engineering(detail, assumptions) if view == ENGINEERING else None,
        basis_note=BASIS_NOTE,
    )
    return document
