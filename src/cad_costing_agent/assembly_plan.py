"""A manufacturing plan per component, rather than one flat plan per assembly.

Why a plan cannot be assembly-wide
----------------------------------
A manufacturing plan is a routing for **one thing clamped in a fixture**. An
assembly is not that. Giving an assembly one plan would produce a setup holding
a housing's facing operation next to a shaft's turning operation, and setup time
is charged once per setup — so the two components would share a fixturing they
cannot physically share, and the assembly would be under-costed by exactly the
setups nobody charged for.

So each **manufactured component definition** owns a complete plan of its own:
its own features, its own suggestions, its own estimated times, its own setups
and its own sequence. A housing and a shaft that happen to use the same machine
still get separate setups, because a shared process type has never been evidence
of shared fixturing in this codebase and is not evidence of it here.

One plan per definition, not per occurrence
-------------------------------------------
Twelve identical bolts are one definition placed twelve times. They are made the
same way, so they have **one** plan; its per-part cost is then multiplied by the
occurrence count in the roll-up, and by the production quantity exactly once
after that. Duplicating the plan per occurrence would multiply the setup time by
twelve, which is the same class of error as charging setup per operation — the
one Step 21 exists to prevent.

Classification decides who gets one
-----------------------------------
Only a `manufactured` component enters features → suggestions → times → setups →
plan → costing. A purchased or standard component is bought, so a machining plan
for it would be fiction; an excluded one contributes nothing on purpose. Those
are `not_applicable`, which is a different thing from "no plan yet" and says so.
An `unresolved` component is `blocked`, and names what is missing.

**"Not generated" is never zero operations.** A component nobody has planned yet
and a component genuinely needing no work would otherwise look identical, and the
first must never be costed as though it were the second.

This module plans; it does not cost
-----------------------------------
It imports `manufacturing_plan` and `component_classification` and nothing else
from the package — no calculator, no rate set, no repository. `manufacturing_plan`
remains the sole bridge to money: each component plan emits its own
`OperationRequirement`s, which the unchanged costing consumes exactly as it does
for a standalone part. A structural test asserts the import set in both
directions.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cad_costing_agent.component_classification import (
    EXCLUDED,
    MANUFACTURED,
    PURCHASED,
    STANDARD,
    UNRESOLVED,
)
from cad_costing_agent.manufacturing_plan import (
    ManufacturingPlan,
    PlanTimeSummary,
    build_plan,
)

# --------------------------------------------------------------------- status

NOT_GENERATED = "not_generated"
"""Plannable, but nobody has generated it yet. **Not** zero operations."""

GENERATED = "generated"
"""A plan exists and every operation in it carries a committed time."""

NEEDS_INPUT = "needs_user_input"
"""A plan exists but something in it is missing: an operation with no committed
cycle time, or a setup with no committed setup time. Named individually."""

NOT_APPLICABLE = "not_applicable"
"""Bought, catalogue or deliberately excluded. There is nothing to machine, so
the absence of a plan is the correct answer rather than a gap."""

BLOCKED = "blocked"
"""Plannable in principle but not yet: unclassified, or the file carries no
geometry to recognize features on."""

PLAN_STATUSES: tuple[str, ...] = (
    NOT_GENERATED,
    GENERATED,
    NEEDS_INPUT,
    NOT_APPLICABLE,
    BLOCKED,
)

STATUS_LABELS = {
    NOT_GENERATED: "Not generated",
    GENERATED: "Generated",
    NEEDS_INPUT: "Needs user input",
    NOT_APPLICABLE: "No manufacturing plan",
    BLOCKED: "Cannot be planned yet",
}

# --------------------------------------------------------------------- reasons

NO_GEOMETRY = "no_geometry_in_file"
NOT_CLASSIFIED = "not_classified"
BOUGHT_NOT_MADE = "bought_not_made"
DELIBERATELY_EXCLUDED = "deliberately_excluded"
SUB_ASSEMBLY = "sub_assembly"
NO_FEATURES = "no_features_recognized"

REASON_TEXT = {
    NO_GEOMETRY: (
        "The file carries no geometry for this component, so there are no features to "
        "recognize and no operations to plan. It is named, counted and placed; it simply "
        "cannot be machined from what the file contains."
    ),
    NOT_CLASSIFIED: (
        "Nobody has said whether this component is made, bought, standard or excluded, and "
        "nothing in the CAD file says. A manufacturing plan cannot be created until someone "
        "classifies it as manufactured."
    ),
    BOUGHT_NOT_MADE: (
        "This component is bought rather than made, so it has no machining plan. Its cost is "
        "the unit cost stated for it. Reclassify it as manufactured if it is made here."
    ),
    DELIBERATELY_EXCLUDED: (
        "This component was excluded from the manufacturing cost by an explicit decision, so "
        "there is nothing to plan."
    ),
    SUB_ASSEMBLY: (
        "This is a sub-assembly, not a part. Its manufacturing is its components', and "
        "planning it as one would count every part inside it a second time."
    ),
    NO_FEATURES: (
        "Feature recognition found nothing to machine on this component. A plan can still be "
        "built by hand — the absence of recognized features is not a claim that no work is "
        "needed."
    ),
}


REFUSAL_TEXT = {
    BOUGHT_NOT_MADE: "it is classified as bought rather than made",
    DELIBERATELY_EXCLUDED: "it has been deliberately excluded from the manufacturing cost",
    SUB_ASSEMBLY: "it is a sub-assembly, and its manufacturing is its components'",
    NO_GEOMETRY: "the file carries no geometry for it",
    NOT_CLASSIFIED: "nobody has classified it yet",
}
"""How the refusal reads in a sentence. Separate from `STATUS_LABELS`, whose
wording is a column heading — "Component 'HOUSING' is no manufacturing plan" is
what happens when a label is reused as a clause."""


class ComponentPlan(BaseModel):
    """One component definition's manufacturing plan, or why it has none.

    Carries the plan and the *reason there is no plan* in the same object,
    because a caller that has to infer the second from the absence of the first
    is one `or 0` away from costing an unplanned component as free.
    """

    definition_id: str
    component_name: str = ""
    part_number: str | None = None
    quantity: int = Field(
        default=0,
        description=(
            "Occurrences of this component in one assembly. The plan is per definition; this "
            "is what its per-part cost is multiplied by, once, in the roll-up."
        ),
    )
    classification: str | None = None
    is_assembly: bool = False

    status: str = NOT_GENERATED
    reason: str | None = None
    reason_detail: str = ""
    missing: list[str] = Field(
        default_factory=list,
        description="Exactly what is missing, named individually. Never summarised as a count.",
    )

    plan: ManufacturingPlan | None = Field(
        default=None, description="Null whenever `status` is anything but generated or needing input."
    )
    feature_count: int = 0
    feature_ids: list[str] = Field(default_factory=list)
    declined_feature_count: int = 0
    time_summary: PlanTimeSummary | None = Field(
        default=None,
        description=(
            "**Committed** setup and cycle minutes, kept apart. Never combined into a single "
            "figure here: setup is charged once per setup and cycle once per part, and a "
            "combined number cannot be multiplied by anything correctly. Zero until someone "
            "commits a time — which is why the estimated totals below exist beside it."
        ),
    )
    # The system's own predictions, before anything is committed. Without these
    # a freshly generated plan reports 0.00 minutes of committed time, which is
    # true and reads exactly like "this component takes no time to make". Both
    # figures are shown so the distinction cannot be missed.
    estimated_setup_time_minutes: float | None = Field(
        default=None, description="What the system estimates the setup takes. Not committed."
    )
    estimated_cycle_time_minutes: float | None = Field(
        default=None,
        description="What the system estimates one part's cycle time to be. Not committed.",
    )
    committed_time_is_complete: bool = Field(
        default=False,
        description=(
            "Whether every operation and setup in the plan carries a committed time. False "
            "means `time_summary` is a partial figure and must not be presented as the "
            "component's manufacturing time."
        ),
    )

    @property
    def plannable(self) -> bool:
        """Whether this component is one a plan could be made for at all."""
        return self.status in (NOT_GENERATED, GENERATED, NEEDS_INPUT)

    @property
    def has_plan(self) -> bool:
        return self.plan is not None

    @property
    def costable(self) -> bool:
        """Whether this plan can contribute machining to a cost.

        True only when a plan exists and at least one operation in it carries a
        committed time. A generated plan whose operations are all still pending
        contributes nothing, and says so rather than contributing zero.
        """
        return bool(self.plan and any(o.costable for o in self.plan.active))


class AssemblyManufacturingPlan(BaseModel):
    """Every component's plan, kept apart, under one assembly.

    Deliberately a mapping of definition to plan rather than a merged routing:
    there is no assembly-level sequence here, because this application knows
    nothing about the order components are made in and inventing one would be a
    manufacturing claim it cannot support.
    """

    assembly_id: str = ""
    assembly_name: str = ""
    source_filename: str = ""
    component_plans: list[ComponentPlan] = Field(default_factory=list)
    units: str = "mm"
    time_profile_id: str | None = None
    time_profile_version: str | None = None
    time_profile_name: str | None = None
    note: str = ""

    def component(self, definition_id: str) -> ComponentPlan:
        for entry in self.component_plans:
            if entry.definition_id == definition_id:
                return entry
        raise KeyError(f"No component plan for '{definition_id}' in this assembly.")

    def plan_for(self, definition_id: str) -> ManufacturingPlan | None:
        try:
            return self.component(definition_id).plan
        except KeyError:
            return None

    @property
    def planned(self) -> list[ComponentPlan]:
        return [entry for entry in self.component_plans if entry.has_plan]

    @property
    def plannable(self) -> list[ComponentPlan]:
        return [entry for entry in self.component_plans if entry.plannable]

    @property
    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.component_plans:
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return counts


ASSEMBLY_PLAN_NOTE = (
    "Each manufactured component has its own plan, its own setups and its own sequence. There "
    "is no assembly-level routing here: nothing in this application knows what order the "
    "components are made or joined in, and a setup is one physical fixturing of one part, so "
    "two components never share one however alike their processes look."
)


def status_for(definition, classification: str | None) -> tuple[str, str | None]:
    """Whether this component can be planned, and the reason when it cannot.

    Reads the classification and whether the file carries geometry, and nothing
    else — the same two facts the classification suggestion is allowed to read.
    """
    if getattr(definition, "is_assembly", False):
        return NOT_APPLICABLE, SUB_ASSEMBLY
    if classification in (PURCHASED, STANDARD):
        return NOT_APPLICABLE, BOUGHT_NOT_MADE
    if classification == EXCLUDED:
        return NOT_APPLICABLE, DELIBERATELY_EXCLUDED
    if classification in (None, UNRESOLVED):
        # An unresolved component with no geometry has two problems; the
        # geometry is the more actionable one to name.
        if getattr(definition, "geometry_status", None) != "measured":
            return BLOCKED, NO_GEOMETRY
        return BLOCKED, NOT_CLASSIFIED
    if classification == MANUFACTURED:
        if getattr(definition, "geometry_status", None) != "measured":
            return BLOCKED, NO_GEOMETRY
        return NOT_GENERATED, None
    # A classification this vocabulary does not know. Refuse to plan rather
    # than fall through into planning it: an unrecognised label is not
    # permission to machine something.
    return BLOCKED, NOT_CLASSIFIED


def _missing_inputs(plan: ManufacturingPlan) -> list[str]:
    """Exactly what stops this plan being complete, named one by one.

    A count would be useless to the person who has to fix it, and a plan
    reported as "needs input" without saying what input is the reason engineers
    stop trusting a tool.
    """
    missing: list[str] = []
    # `active` is already everything the engineer has not removed, in setup
    # order — so a rejected operation cannot be reported as a missing input.
    for operation in plan.active:
        if operation.final_cycle_time is None:
            missing.append(
                f"{operation.operation_id} ({operation.operation_name}) has no committed "
                "cycle time"
            )
    for setup in plan.ordered_setups:
        # `setup_time_minutes` is the *committed* time and is None until someone
        # commits one. `charged_setup_time` is what reaches costing and is 0.0
        # in that case by design, so testing it here would never fire.
        if setup.setup_time_minutes is None and plan.operations_in(setup.setup_id):
            missing.append(f"{setup.setup_id} ({setup.name}) has no committed setup time")
    return missing


def summarise(entry: ComponentPlan) -> ComponentPlan:
    """Fill in a component plan's status, missing inputs and time summary.

    Separated so a plan edited by a person can be re-summarised without being
    rebuilt: the status of a plan is a reading of it, never a decision about it.
    """
    if entry.plan is None:
        return entry

    missing = _missing_inputs(entry.plan)
    status = NEEDS_INPUT if missing else GENERATED

    # The system's estimates, summed straight off the operations and setups.
    # Deliberately separate from `time_summary`, which reports what has been
    # *committed* and is legitimately zero until someone commits it.
    estimated_cycle = sum(
        operation.system_suggested_cycle_time or 0.0 for operation in entry.plan.active
    )
    estimated_setup = sum(
        setup.system_setup_time_minutes or 0.0
        for setup in entry.plan.ordered_setups
        if entry.plan.operations_in(setup.setup_id)
    )

    return entry.model_copy(
        update={
            "status": status,
            "missing": missing,
            "estimated_setup_time_minutes": round(estimated_setup, 6) or None,
            "estimated_cycle_time_minutes": round(estimated_cycle, 6) or None,
            "committed_time_is_complete": not missing,
            "reason": None if not missing else None,
            "reason_detail": (
                ""
                if not missing
                else (
                    "This plan is not finished: the inputs below are missing. The operations "
                    "they belong to are left out of the cost rather than defaulted to zero."
                )
            ),
            "time_summary": entry.plan.time_summary(),
        }
    )


def build_component_plan(
    definition,
    classification: str | None,
    suggestion_result=None,
    *,
    units: str = "mm",
    profile=None,
    assumptions=None,
    source_filename: str = "",
    id_prefix: str | None = None,
) -> ComponentPlan:
    """Build one component's plan, or say why it has none.

    `suggestion_result` is an `OperationSuggestionResult` for *this component's
    own* features — never the assembly's aggregate, which would attribute one
    component's holes to another. `None` means recognition was not run, which
    leaves the component `not_generated` rather than planned with nothing in it.
    """
    entry = ComponentPlan(
        definition_id=definition.definition_id,
        component_name=definition.name,
        part_number=getattr(definition, "part_number", None),
        quantity=definition.quantity,
        classification=classification,
        is_assembly=getattr(definition, "is_assembly", False),
    )

    status, reason = status_for(definition, classification)
    if status != NOT_GENERATED:
        return entry.model_copy(
            update={
                "status": status,
                "reason": reason,
                "reason_detail": REASON_TEXT.get(reason or "", ""),
            }
        )

    if suggestion_result is None:
        return entry

    suggestions = list(getattr(suggestion_result, "suggestions", []) or [])
    declined = list(getattr(suggestion_result, "declined", []) or [])
    feature_ids = sorted(
        {s.feature_id for s in suggestions if getattr(s, "feature_id", None)}
    )

    if not suggestions:
        return entry.model_copy(
            update={
                "status": NOT_GENERATED,
                "reason": NO_FEATURES,
                "reason_detail": REASON_TEXT[NO_FEATURES],
                "declined_feature_count": len(declined),
            }
        )

    # The component's name namespaces its ids, so `HOUSING/OP-001` and
    # `SHAFT/OP-001` are different operations rather than a collision — the same
    # rule `recognize_component_features` already applies to feature ids.
    prefix = id_prefix if id_prefix is not None else f"{definition.name}/"
    plan = build_plan(
        suggestions,
        units=units,
        assumptions=assumptions,
        profile=profile,
        source_filename=source_filename,
        id_prefix=prefix,
    )

    return summarise(
        entry.model_copy(
            update={
                "plan": plan,
                "feature_count": len(feature_ids),
                "feature_ids": feature_ids,
                "declined_feature_count": len(declined),
            }
        )
    )


def build_assembly_plan(
    assembly,
    classifications: dict[str, str | None],
    suggestions: dict | None = None,
    *,
    units: str = "mm",
    profile=None,
    assumptions=None,
    source_filename: str = "",
) -> AssemblyManufacturingPlan:
    """A plan per manufactured component, and a stated reason for every other.

    `classifications` maps a definition id to its effective classification;
    `suggestions` maps a definition id to that component's own
    `OperationSuggestionResult`. A definition missing from `suggestions` is
    reported `not_generated` — never planned with an empty operation list, which
    would read as "this component needs no work".
    """
    suggestions = suggestions or {}
    plans = [
        build_component_plan(
            definition,
            classifications.get(definition.definition_id),
            suggestions.get(definition.definition_id),
            units=units,
            profile=profile,
            assumptions=assumptions,
            source_filename=source_filename,
        )
        for definition in assembly.definitions
    ]

    return AssemblyManufacturingPlan(
        assembly_id=getattr(assembly, "assembly_id", ""),
        assembly_name=getattr(assembly, "name", ""),
        source_filename=source_filename,
        component_plans=plans,
        units=units,
        time_profile_id=getattr(profile, "time_profile_id", None),
        time_profile_version=getattr(profile, "version", None),
        time_profile_name=getattr(profile, "name", None),
        note=ASSEMBLY_PLAN_NOTE,
    )


def apply_edited_plans(
    assembly_plan: AssemblyManufacturingPlan, edited: dict
) -> AssemblyManufacturingPlan:
    """Replace component plans with the versions a person edited, and re-read them.

    The status, the missing inputs and the time summary are recomputed from the
    edited plan rather than trusted from the request: they are readings of a
    plan, and a caller that could assert "complete" would be able to assert a
    component into a cost it has not earned.
    """
    updated = []
    for entry in assembly_plan.component_plans:
        replacement = edited.get(entry.definition_id)
        if replacement is None:
            updated.append(entry)
            continue
        if not entry.plannable:
            raise ValueError(
                f"Component '{entry.component_name or entry.definition_id}' cannot carry a "
                f"manufacturing plan: {REFUSAL_TEXT.get(entry.reason or '', entry.status)}. "
                "Change its classification first."
            )
        updated.append(summarise(entry.model_copy(update={"plan": replacement})))

    unknown = set(edited) - {entry.definition_id for entry in assembly_plan.component_plans}
    if unknown:
        raise ValueError(
            f"No component definition(s) {', '.join(sorted(unknown))} in this assembly. A plan "
            "for a component the file does not contain is more likely a mismatched file than a "
            "typo, so nothing was applied."
        )

    return assembly_plan.model_copy(update={"component_plans": updated})
