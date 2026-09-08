"""Rolling component costs up into an assembly total.

What this does, and pointedly what it does not
-----------------------------------------------
It works out what each component contributes and adds the contributions up.
Since Step 26 a component contributes in one of four ways, decided by its
classification and nothing else:

    manufactured   per-unit cost from the unchanged costing pipeline x quantity
    purchased      the unit cost the user stated                     x quantity
    standard       the unit cost the user configured                 x quantity
    excluded       nothing, because a person said so
    unresolved     nothing, and the total stays incomplete and says why

    assembly total = manufactured + purchased + standard

There is still no assembly labour here, no fastening time, no make/buy
inference and no routing. Their absence is stated on every summary this module
produces, because a total that silently omits them would read as a quotation.

The existing costing engine remains the authority for a **manufactured** part.
Nothing in this module calculates a part cost, resolves a rate, or touches a
rate set — it consumes per-part figures that the unchanged pipeline already
produced, and unit costs that a person typed. Its only package import is
`component_classification`, which is itself a vocabulary with no imports of its
own; a structural test asserts both, which is what keeps a calculator and a
repository out of reach.

A component that could not be costed is never zero
--------------------------------------------------
Zero is a number, and a number that joins a sum. A component with no estimate,
or one classified as bought with no price entered, is carried as `costed=False`
with the reason, is counted separately in the summary, and keeps the total
marked incomplete. The alternative — treating "we do not know" as "nothing" —
produces a total that is wrong in the one direction nobody checks.

The single legitimate zero is an **excluded** component, because a person
decided it. It contributes 0, says why, and does *not* make the total
incomplete: a deliberate omission is not a gap.

Occurrence quantity is not production quantity
----------------------------------------------
A definition's `quantity` is how many times *this assembly* uses it. How many
assemblies are being built is a different number that no CAD file contains, and
it arrives here as `assembly_quantity`. The two are multiplied exactly once:

    extended_cost            = per-unit cost x occurrence quantity   (one assembly)
    production_extended_cost = per-unit cost x occurrence quantity x assemblies

Four bearings in an assembly built ten times is forty bearings, and forty is
reached by one multiplication in one place. `assembly_quantity` defaults to 1,
so a caller that does not care about a production run gets exactly the previous
behaviour.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cad_costing_agent.component_classification import (
    COST_FROM_ESTIMATE,
    COST_NONE,
    COST_USER_ENTERED,
    COSTED,
    CURRENCY_MISMATCH,
    ESTIMATE_FAILED,
    EXCLUDED,
    EXCLUDED_FROM_TOTAL,
    MANUFACTURED,
    NEEDS_COSTING_INPUT,
    NO_COMMITTED_OPERATIONS,
    NO_CURRENCY,
    NO_GEOMETRY,
    NO_UNIT_COST,
    NOT_A_COMPONENT,
    NOT_ESTIMATED,
    PURCHASED,
    REASON_TEXT,
    STANDARD,
    SUB_ASSEMBLY,
    UNRESOLVED,
    UNRESOLVED_CLASSIFICATION,
    ComponentCostingProfile,
    build_profiles,
)

# Re-exported so a consumer of the roll-up does not have to know which of the
# two modules a reason string was defined in.
__all__ = [
    "ASSEMBLY_SCOPE_NOTE",
    "CLASSIFICATION_SCOPE_NOTE",
    "CURRENCY_MISMATCH",
    "ESTIMATE_FAILED",
    "NO_COMMITTED_OPERATIONS",
    "NO_CURRENCY",
    "NO_GEOMETRY",
    "NO_UNIT_COST",
    "NOT_ESTIMATED",
    "REASON_TEXT",
    "SUB_ASSEMBLY",
    "UNRESOLVED_CLASSIFICATION",
    "AssemblyCostSummary",
    "ComponentCostLine",
    "ComponentEstimate",
    "roll_up",
]


class ComponentEstimate(BaseModel):
    """A per-part figure that the existing costing engine already produced.

    Deliberately a plain carrier: this module never asks how the number was
    made, and cannot, because it imports nothing that could tell it.
    """

    definition_id: str
    estimate_id: str
    currency: str
    per_unit_cost: float = Field(description="Cost of one of this component, from its estimate.")
    material_per_unit: float = 0.0
    operation_per_unit: float = 0.0
    additional_per_unit: float = 0.0
    # The operation cost split as the costed operations already report it:
    # `setup_cost` is separate on every `OperationCostResult`, and the plan hangs
    # a setup's whole time on the first costable operation in it, so summing
    # gives the part's setup cost rather than one per operation. Splitting a
    # figure the engine produced is not a second formula — and a test asserts
    # setup + run comes back to `operation_per_unit`.
    setup_per_unit: float = 0.0
    run_per_unit: float = 0.0


class ComponentCostLine(BaseModel):
    """One row of the assembly's bill of materials."""

    definition_id: str
    name: str
    part_number: str | None = None
    quantity: int = Field(
        description="Occurrences of this component in one assembly. Not a production quantity."
    )
    is_assembly: bool = False

    # --- what kind of component this is, and who said so ---
    classification: str | None = Field(
        default=None, description="Null for a sub-assembly, which is neither made nor bought."
    )
    classification_source: str = "unclassified"
    system_classification: str | None = None
    user_classification: str | None = None
    classification_changed: bool = False

    costing_status: str = NEEDS_COSTING_INPUT
    costed: bool = False
    excluded: bool = Field(
        default=False,
        description=(
            "Deliberately left out by a person. The one legitimate zero here, and distinct "
            "from a component nobody could cost."
        ),
    )
    exclusion_reason: str = ""
    reason: str | None = Field(
        default=None, description="Why this component was not costed. Null when it was."
    )
    reason_detail: str = ""

    material: str | None = None
    material_source: str = "unresolved"
    supplier: str | None = None
    supplier_part_number: str | None = None

    estimate_id: str | None = None
    currency: str | None = None
    unit_cost_source: str = COST_NONE
    per_unit_cost: float | None = None
    extended_cost: float | None = Field(
        default=None,
        description=(
            "per_unit_cost x quantity for one assembly, 0 for an excluded component, and null "
            "when the component could not be costed at all."
        ),
    )
    material_extended: float | None = None
    operation_extended: float | None = None
    additional_extended: float | None = None
    setup_extended: float | None = Field(
        default=None, description="Setup cost x quantity. Charged once per setup, per part."
    )
    run_extended: float | None = Field(
        default=None, description="Cycle cost x quantity."
    )

    # --- how this component was planned, when it was ---
    #
    # Counts and minutes rather than a plan: the roll-up is about money, and the
    # plans themselves live in the assembly plan. These are here so a bill of
    # materials can show "8 operations across 2 setups" beside a cost without
    # the reader having to join two documents by hand.
    plan_status: str | None = None
    operation_count: int = 0
    setup_count: int = 0
    setup_time_minutes: float | None = None
    cycle_time_minutes: float | None = None
    manufacturing_committed: bool | None = Field(
        default=None,
        description=(
            "True when this component's plan committed some time — setup, cycle or both — so "
            "the cost includes something for making it. False when the caller supplied plan "
            "facts and they show nothing committed: the cost is then material and tooling "
            "only, a real number for an unreal state. **None means the caller said nothing "
            "about plans at all**, which is not the same as saying there are none — a roll-up "
            "computed without plan facts is the pre-plan contract and this module will not "
            "invent an opinion about it. Read off the facts; nothing here can compute a time."
        ),
    )

    # --- the production run, multiplied exactly once ---
    assembly_quantity: int = 1
    total_quantity: int = Field(
        default=0, description="quantity x assembly_quantity. The only place the two ever meet."
    )
    production_extended_cost: float | None = None


class AssemblyCostSummary(BaseModel):
    """What the assembly adds up to, and how much of it is actually known."""

    assembly_name: str
    estimate_id: str | None = Field(
        default=None,
        description=(
            "The estimate this roll-up was saved as, so a quotation can be produced "
            "from it. Set by the endpoint after persisting; this module neither "
            "reads nor computes it, and a roll-up that was never saved leaves it None."
        ),
    )
    currency: str | None = None
    currencies_present: list[str] = Field(
        default_factory=list,
        description="Every currency found among the components. More than one is reported, "
        "never converted.",
    )
    is_aggregatable: bool = Field(
        default=True,
        description=(
            "False when components are denominated in more than one currency. The figures "
            "below then cover the base currency only and the rest are listed separately."
        ),
    )

    definition_count: int = 0
    part_definition_count: int = 0
    occurrence_count: int = Field(
        default=0, description="Every placement in the assembly, sub-assemblies included."
    )
    part_occurrence_count: int = Field(
        default=0,
        description=(
            "Placements of parts only. This is the figure the costed count is measured "
            "against: a sub-assembly placement is not a thing to be costed, so counting it in "
            "the denominator would make a complete roll-up look like it had missed one."
        ),
    )
    assembly_quantity: int = Field(
        default=1,
        description=(
            "How many of this assembly are being built. Not a property of the CAD file, and "
            "never confused with an occurrence count."
        ),
    )

    costed_definition_count: int = 0
    uncosted_definition_count: int = 0
    costed_occurrence_count: int = 0
    uncosted_occurrence_count: int = 0
    excluded_definition_count: int = 0
    unresolved_definition_count: int = 0
    needs_input_definition_count: int = 0
    unplanned_definition_count: int = Field(
        default=0,
        description=(
            "Manufactured component definitions that were costed but have committed no "
            "manufacturing time at all. They are in the total — their material and tooling are "
            "real — but nothing is charged for making them, so the total is partial. Distinct "
            "from needs_input_definition_count, which counts components left out of the total "
            "entirely."
        ),
    )

    classification_counts: dict[str, int] = Field(
        default_factory=dict, description="Definitions per classification."
    )

    # --- what each kind of component contributed ---
    manufactured_cost: float | None = None
    purchased_cost: float | None = None
    standard_cost: float | None = None
    excluded_cost: float = Field(
        default=0.0,
        description="Always zero, and shown rather than hidden so the exclusion is visible.",
    )

    # --- the manufactured breakdown, unchanged ---
    material_cost: float | None = None
    operation_cost: float | None = None
    additional_cost: float | None = None
    setup_cost: float | None = Field(
        default=None, description="The setup half of the manufactured operation cost."
    )
    run_cost: float | None = Field(
        default=None, description="The cycle half of the manufactured operation cost."
    )

    total_cost: float | None = None
    production_total_cost: float | None = Field(
        default=None, description="total_cost x assembly_quantity. One multiplication."
    )

    is_complete: bool = Field(
        default=False,
        description=(
            "True only when every part definition in the assembly was costed or deliberately "
            "excluded, *and* every manufactured one that was costed has committed manufacturing "
            "time behind it. A false value means the total is a partial sum and must not be "
            "presented as a quotation. The second condition exists because a manufactured "
            "component with no committed time still produces a plausible figure — its material "
            "and its tooling — and a roll-up of those alone once read as a finished assembly "
            "cost at less than half the real one."
        ),
    )
    completeness_note: str = ""
    currency_note: str = ""
    lines: list[ComponentCostLine] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


ASSEMBLY_SCOPE_NOTE = (
    "This total is the sum of each component's contribution and nothing else. It contains no "
    "assembly labour and no fastening or joining time, neither of which this application "
    "calculates. It is a roll-up of the parts, not a price for the assembly."
)

CLASSIFICATION_SCOPE_NOTE = (
    "Manufactured components are costed by the unchanged pipeline. Purchased and standard "
    "components carry the unit cost a person entered: no price is looked up, no supplier is "
    "contacted and no cost is derived from a component's weight. Nothing in a CAD file states "
    "whether a component is made or bought."
)


def _uncosted_reason(definition, classification, profile) -> str:
    """Why a component with no contribution has none. Stated, never inferred at
    display time."""
    if classification == UNRESOLVED or classification is None:
        if getattr(definition, "geometry_status", None) != "measured":
            return NO_GEOMETRY
        return UNRESOLVED_CLASSIFICATION
    if classification in (PURCHASED, STANDARD):
        # An amount with no currency is not a cost, and saying "no unit cost"
        # about a component whose cost *was* entered would send someone to
        # re-type a number that is already there.
        if profile.unit_cost is not None and not profile.currency:
            return NO_CURRENCY
        return NO_UNIT_COST
    if getattr(definition, "geometry_status", None) != "measured":
        return NO_GEOMETRY
    return NOT_ESTIMATED


def roll_up(
    assembly,
    estimates: dict[str, ComponentEstimate] | None = None,
    *,
    currency: str | None = None,
    profiles: list[ComponentCostingProfile] | None = None,
    assembly_quantity: int = 1,
    plans: dict | None = None,
) -> AssemblyCostSummary:
    """Add up what is known, and say plainly what is not.

    `assembly` is a `CADAssembly`; `profiles` carries each component's
    classification and any unit cost a person stated; `estimates` maps a
    component definition id to the per-part figure the unchanged pipeline
    produced for it, and is consulted only for **manufactured** components.

    With no profiles, every measurable component takes the system's suggestion —
    manufactured — which is exactly the behaviour before classification existed,
    and every line says the classification came from the system rather than from
    a person.

    The currency is taken from the first costed component unless one is given.
    Components denominated differently are excluded rather than converted, and
    the summary reports that the assembly is not directly aggregatable.
    """
    estimates = estimates or {}
    plans = plans or {}
    if assembly_quantity < 1:
        raise ValueError(
            f"assembly_quantity must be at least 1, got {assembly_quantity}. A production "
            "quantity of zero is a decision not to build, not a cost of nothing."
        )
    if profiles is None:
        profiles = build_profiles(assembly)
    by_id = {profile.definition_id: profile for profile in profiles}

    part_ids = {definition.definition_id for definition in assembly.part_definitions}
    summary = AssemblyCostSummary(
        assembly_name=assembly.name,
        definition_count=len(assembly.definitions),
        part_definition_count=len(assembly.part_definitions),
        occurrence_count=len(assembly.occurrences),
        part_occurrence_count=sum(
            1 for occurrence in assembly.occurrences if occurrence.definition_id in part_ids
        ),
        assembly_quantity=assembly_quantity,
        notes=[ASSEMBLY_SCOPE_NOTE, CLASSIFICATION_SCOPE_NOTE],
    )

    for definition in assembly.definitions:
        profile = by_id.get(definition.definition_id) or ComponentCostingProfile(
            definition_id=definition.definition_id,
            name=definition.name,
            is_assembly=definition.is_assembly,
            quantity=definition.quantity,
        )
        quantity = definition.quantity
        classification = profile.classification

        line = ComponentCostLine(
            definition_id=definition.definition_id,
            name=definition.name,
            part_number=definition.part_number,
            quantity=quantity,
            is_assembly=definition.is_assembly,
            classification=classification,
            classification_source=profile.classification_source,
            system_classification=profile.system_classification,
            user_classification=profile.user_classification,
            classification_changed=profile.classification_changed,
            material=profile.material,
            material_source=profile.material_source,
            supplier=profile.supplier,
            supplier_part_number=profile.supplier_part_number,
            assembly_quantity=assembly_quantity,
            total_quantity=quantity * assembly_quantity,
        )

        # How this component was planned, when it was. Read off a plain carrier
        # the caller supplies, so this module still imports nothing that could
        # plan or price anything.
        planned = plans.get(definition.definition_id)
        if planned is not None:
            line.plan_status = planned.get("status")
            line.operation_count = int(planned.get("operation_count") or 0)
            line.setup_count = int(planned.get("setup_count") or 0)
            line.setup_time_minutes = planned.get("setup_time_minutes")
            line.cycle_time_minutes = planned.get("cycle_time_minutes")
            # These two are *committed* minutes — the plan's own time summary
            # counts only operations whose time someone accepted or typed — so
            # their sum being zero is exactly "nothing has been committed".
            line.manufacturing_committed = (
                (line.setup_time_minutes or 0.0) + (line.cycle_time_minutes or 0.0)
            ) > 0
        # else it stays None: no facts were supplied, so nothing is claimed.

        # A sub-assembly is a container, not a thing that is made or bought.
        # Costing it would count every part inside it a second time.
        if definition.is_assembly:
            line.costing_status = NOT_A_COMPONENT
            line.reason = SUB_ASSEMBLY
            line.reason_detail = REASON_TEXT[SUB_ASSEMBLY]
            summary.lines.append(line)
            continue

        summary.classification_counts[classification or UNRESOLVED] = (
            summary.classification_counts.get(classification or UNRESOLVED, 0) + 1
        )

        # Deliberately out. The one zero here that a person authorized.
        if classification == EXCLUDED:
            line.costing_status = EXCLUDED_FROM_TOTAL
            line.excluded = True
            line.exclusion_reason = profile.exclusion_reason or "user_selected"
            line.extended_cost = 0.0
            line.production_extended_cost = 0.0
            line.reason_detail = (
                f"Excluded from the assembly total. Reason: {line.exclusion_reason}."
            )
            summary.lines.append(line)
            continue

        # Where the per-unit figure comes from depends entirely on the
        # classification, and on nothing else.
        per_unit: float | None = None
        line_currency: str | None = None
        material_unit = operation_unit = additional_unit = 0.0
        setup_unit = run_unit = 0.0

        if classification in (PURCHASED, STANDARD):
            if profile.unit_cost is not None:
                per_unit = profile.unit_cost
                line_currency = profile.currency
                line.unit_cost_source = COST_USER_ENTERED
        elif classification == MANUFACTURED:
            estimate = estimates.get(definition.definition_id)
            if estimate is not None:
                per_unit = estimate.per_unit_cost
                line_currency = estimate.currency
                line.estimate_id = estimate.estimate_id
                line.unit_cost_source = COST_FROM_ESTIMATE
                material_unit = estimate.material_per_unit
                operation_unit = estimate.operation_per_unit
                additional_unit = estimate.additional_per_unit
                setup_unit = estimate.setup_per_unit
                run_unit = estimate.run_per_unit

        if per_unit is None or line_currency is None:
            reason = _uncosted_reason(definition, classification, profile)
            line.costing_status = NEEDS_COSTING_INPUT
            line.reason = reason
            line.reason_detail = REASON_TEXT[reason]
            summary.lines.append(line)
            continue

        if currency is None:
            currency = line_currency

        line.currency = line_currency
        line.per_unit_cost = per_unit

        if line_currency != currency:
            line.costing_status = NEEDS_COSTING_INPUT
            line.reason = CURRENCY_MISMATCH
            line.reason_detail = REASON_TEXT[CURRENCY_MISMATCH]
            summary.lines.append(line)
            continue

        line.costing_status = COSTED
        line.costed = True
        line.extended_cost = per_unit * quantity
        line.production_extended_cost = per_unit * line.total_quantity
        if classification == MANUFACTURED:
            line.material_extended = material_unit * quantity
            line.operation_extended = operation_unit * quantity
            line.additional_extended = additional_unit * quantity
            line.setup_extended = setup_unit * quantity
            line.run_extended = run_unit * quantity
        summary.lines.append(line)

    _summarise(summary, currency)
    return summary


def _by_classification(lines, classification) -> float:
    return sum(
        line.extended_cost or 0.0
        for line in lines
        if line.costed and line.classification == classification
    )


def _summarise(summary: AssemblyCostSummary, currency: str | None) -> None:
    """Fill in the totals and say honestly how complete they are."""
    lines = summary.lines
    costed = [line for line in lines if line.costed]
    parts = [line for line in lines if not line.is_assembly]
    excluded = [line for line in parts if line.excluded]
    needs_input = [line for line in parts if line.costing_status == NEEDS_COSTING_INPUT]

    summary.currency = currency
    summary.currencies_present = sorted(
        {line.currency for line in parts if line.currency is not None}
    )
    summary.is_aggregatable = len(summary.currencies_present) <= 1

    summary.costed_definition_count = len(costed)
    summary.uncosted_definition_count = len(needs_input)
    summary.costed_occurrence_count = sum(line.quantity for line in costed)
    summary.uncosted_occurrence_count = sum(line.quantity for line in needs_input)
    summary.excluded_definition_count = len(excluded)
    summary.needs_input_definition_count = len(needs_input)
    summary.unresolved_definition_count = sum(
        1 for line in parts if line.classification in (None, UNRESOLVED)
    )

    if costed:
        summary.manufactured_cost = _by_classification(costed, MANUFACTURED)
        summary.purchased_cost = _by_classification(costed, PURCHASED)
        summary.standard_cost = _by_classification(costed, STANDARD)
        summary.material_cost = sum(line.material_extended or 0.0 for line in costed)
        summary.operation_cost = sum(line.operation_extended or 0.0 for line in costed)
        summary.setup_cost = sum(line.setup_extended or 0.0 for line in costed)
        summary.run_cost = sum(line.run_extended or 0.0 for line in costed)
        summary.additional_cost = sum(line.additional_extended or 0.0 for line in costed)
        summary.total_cost = sum(line.extended_cost or 0.0 for line in costed)
        summary.production_total_cost = summary.total_cost * summary.assembly_quantity

    # `excluded_cost` stays at its default 0.0 whether or not anything was
    # excluded. It is a field rather than an omission so that a reader can see
    # the exclusion contributed nothing, instead of having to notice an absence.

    # A manufactured component that was costed with no committed time is in the
    # total and is not a gap in coverage — every definition was accounted for —
    # but the number it contributed is material and tooling with nothing for the
    # making. That is a different kind of incompleteness from a component left
    # out, and it needs its own count rather than being folded into the other.
    unplanned = [
        line
        for line in costed
        if line.classification == MANUFACTURED and line.manufacturing_committed is False
    ]
    for line in unplanned:
        line.reason = NO_COMMITTED_OPERATIONS
        line.reason_detail = REASON_TEXT[NO_COMMITTED_OPERATIONS]
    summary.unplanned_definition_count = len(unplanned)

    summary.is_complete = bool(costed) and not needs_input and not unplanned

    if not summary.is_aggregatable:
        summary.currency_note = (
            "This assembly's components are priced in more than one currency "
            f"({', '.join(summary.currencies_present)}), so the total is not directly "
            f"aggregatable. Nothing is converted. The figures below cover {currency} only; "
            "the rest are listed with the reason. Choose one currency for the whole assembly "
            "to get a single total."
        )

    if not costed:
        summary.completeness_note = (
            f"None of this assembly's {len(parts)} component definitions could be costed, so "
            "there is no total to show. Nothing is reported as zero."
        )
    elif needs_input:
        summary.completeness_note = (
            f"{len(needs_input)} of {len(parts)} component definitions "
            f"({summary.uncosted_occurrence_count:,} of {summary.part_occurrence_count:,} part "
            f"occurrences) need costing input, so this is a partial sum covering "
            f"{summary.costed_definition_count} definition(s) only. It is not a quotation and "
            "the missing components are listed with the reason for each."
        )
        if unplanned:
            summary.completeness_note += (
                f" Separately, {len(unplanned)} manufactured component(s) that were costed have "
                "committed no manufacturing time, so their contribution is material and tooling "
                "only."
            )
    elif unplanned:
        named = ", ".join(line.name for line in unplanned[:4]) + (
            ", …" if len(unplanned) > 4 else ""
        )
        summary.completeness_note = (
            f"Partial assembly estimate — manufacturing plans incomplete. "
            f"{len(unplanned)} of {summary.classification_counts.get(MANUFACTURED, 0)} manufactured "
            f"component definitions ({named}) have committed no manufacturing time, so this "
            "total carries their material and tooling and nothing for making them. It is not a "
            "quotation. Review each component's plan and accept or enter its setup and cycle "
            "times, then cost the assembly again."
        )
    else:
        excluded_note = (
            f" {len(excluded)} were deliberately excluded and contribute nothing."
            if excluded
            else ""
        )
        summary.completeness_note = (
            f"Every one of the {len(parts)} component definitions was costed or explicitly "
            f"excluded, covering all {summary.part_occurrence_count:,} part "
            f"occurrences, and every manufactured one has committed manufacturing time behind "
            f"it.{excluded_note}"
        )

    if summary.assembly_quantity > 1:
        summary.notes.append(
            f"The per-assembly figures above cover one assembly. Building "
            f"{summary.assembly_quantity} of them multiplies each component's occurrence "
            "quantity by that number exactly once — a component used 4 times in an assembly "
            f"built {summary.assembly_quantity} times is "
            f"{4 * summary.assembly_quantity} of that component."
        )
