"""What kind of component this is, and therefore how it gets a cost.

The problem this solves
-----------------------
Until now an assembly was a bag of components that were all assumed to be
machined from stock. That is wrong for most real assemblies: a pump has a
housing that is made, a bearing that is bought, a bolt that is a catalogue
item, and a shipping washer that should not be in the manufacturing cost at
all. Costing all four the same way produces a number that is confidently
wrong — a bearing costed by its volume of steel is off by an order of
magnitude in whichever direction the market happens to sit.

So a component is now one of five things:

    manufactured   made here: geometry -> mass -> features -> operations -> cost
    purchased      bought: a unit cost the user states, with a supplier
    standard       a catalogue item: a unit cost the user states
    excluded       deliberately not part of the manufacturing cost
    unresolved     not enough information to cost it, and said so

Nothing in a CAD file says which
--------------------------------
This is the honest core of the module. **No CAD format states make/buy.** A
purchased bearing is modelled with exactly the same kind of solid as a machined
housing; a bolt looks like a turned shaft; a filename is a filename. So make/buy
is never inferred from a name, a shape, an appearance, a material or a file
name — a rule that is asserted by a test reading this source, because it is the
single most tempting shortcut here and the one that would quietly produce
fiction.

What the system may say is narrower, and it says only that:

    "The file carries measurable solid geometry for this component, which is
     what this application knows how to cost."

That is evidence about what *this application can do*, not evidence about what
the part *is*. It is offered as a `low`-confidence suggestion carrying its own
caveat, and the engineer's answer replaces it. The suggestion is never
overwritten: `system_classification` and `user_classification` sit side by side
for the life of the estimate, which is the same rule the operation suggestions
and the setup grouping already follow.

A suggestion is applied, and labelled — not applied silently
------------------------------------------------------------
An assembly with no classification decisions still has to cost something, or
the roll-up would go blank the moment this step shipped. So the suggestion is
the effective classification *until* a user states one, exactly as an assumed
setup grouping is used until an engineer regroups it. What makes that honest
rather than silent is that every line reports `classification_source`, and a
`system_suggested` line says so in the UI, in the API and in the stored
snapshot. Nothing is applied without being visible.

Missing information is never zero
---------------------------------
A purchased component with no unit cost entered is `needs_costing_input` with
the reason, not a component that costs nothing. The distinction matters most
precisely where it is most tempting to skip: a zero joins a sum and disappears,
and a total containing three invisible zeroes is wrong in the one direction
nobody audits.

An excluded component is the one legitimate zero, because a person said so. It
carries `excluded=True` and a stated reason, contributes 0, and — unlike an
unresolved one — does not make the total incomplete.

This module imports nothing from the rest of the application. It defines a
vocabulary and carries decisions; it resolves no rate, reads no CAD file and
calculates no part cost.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, computed_field

# --------------------------------------------------------------------- the five kinds

MANUFACTURED = "manufactured"
PURCHASED = "purchased"
STANDARD = "standard"
EXCLUDED = "excluded"
UNRESOLVED = "unresolved"

CLASSIFICATIONS: tuple[str, ...] = (MANUFACTURED, PURCHASED, STANDARD, EXCLUDED, UNRESOLVED)

CLASSIFICATION_LABELS = {
    MANUFACTURED: "Manufactured",
    PURCHASED: "Purchased",
    STANDARD: "Standard",
    EXCLUDED: "Excluded",
    UNRESOLVED: "Unresolved",
}

CLASSIFICATION_DESCRIPTIONS = {
    MANUFACTURED: (
        "Made here. Costed through the unchanged pipeline: geometry, mass, material, features, "
        "operations, setups and rates."
    ),
    PURCHASED: (
        "Bought from a supplier at a price the user states. It receives no machining "
        "operations, because nobody here is machining it."
    ),
    STANDARD: (
        "A catalogue item — a bolt, a nut, a bearing — at a unit cost the user configures. "
        "There is no standards database here and no price is looked up."
    ),
    EXCLUDED: (
        "Deliberately left out of the manufacturing cost. It stays in the assembly tree and "
        "keeps its quantity; it simply contributes nothing, on purpose."
    ),
    UNRESOLVED: (
        "Not enough information to cost this component. It is listed with the reason and "
        "leaves the total incomplete. It is never counted as zero."
    ),
}

# A classification that requires the user to state a unit cost.
USER_PRICED: tuple[str, ...] = (PURCHASED, STANDARD)

# --------------------------------------------------------------------- provenance

SYSTEM_SUGGESTED = "system_suggested"
"""The effective classification is the system's suggestion, not yet confirmed."""

USER_SELECTED = "user_selected"
"""An engineer stated it. Authoritative, and it replaces the suggestion for
costing while leaving the suggestion itself on the record."""

UNCLASSIFIED = "unclassified"
"""Neither: nothing to suggest and nobody has said. Costs nothing and blocks
the total, rather than defaulting to anything."""

CLASSIFICATION_SOURCES: tuple[str, ...] = (SYSTEM_SUGGESTED, USER_SELECTED, UNCLASSIFIED)

# Where a material came from, kept distinct because a material read out of a
# CAD file and a material a person picked are different kinds of fact.
MATERIAL_FROM_CAD = "cad_file"
MATERIAL_USER_SELECTED = "user_selected"
MATERIAL_ASSEMBLY_DEFAULT = "assembly_default"
MATERIAL_UNRESOLVED = "unresolved"

# Where a unit cost came from.
COST_FROM_ESTIMATE = "manufacturing_estimate"
COST_USER_ENTERED = "user_entered"
COST_NONE = "none"

# --------------------------------------------------------------------- costing status

COSTED = "costed"
NEEDS_COSTING_INPUT = "needs_costing_input"
EXCLUDED_FROM_TOTAL = "excluded_from_total"
NOT_A_COMPONENT = "not_a_component"
"""A sub-assembly. It is a container, not a thing to be made or bought, and its
cost is its children's. Classifying it would double every part inside it."""

COSTING_STATUSES: tuple[str, ...] = (
    COSTED,
    NEEDS_COSTING_INPUT,
    EXCLUDED_FROM_TOTAL,
    NOT_A_COMPONENT,
)

# --------------------------------------------------------------------- why not costed

NO_GEOMETRY = "no_geometry_in_file"
NOT_ESTIMATED = "not_estimated"
SUB_ASSEMBLY = "sub_assembly"
ESTIMATE_FAILED = "estimate_failed"
CURRENCY_MISMATCH = "currency_mismatch"
NO_UNIT_COST = "no_unit_cost"
NO_CURRENCY = "no_currency"
UNRESOLVED_CLASSIFICATION = "unresolved_classification"

# Not a reason a component could not be costed — it was — but a reason the cost
# is not the whole cost. A manufactured component whose plan has committed no
# time at all contributes its material and its tooling and nothing for making
# it, which is a smaller number that looks exactly like a finished one.
NO_COMMITTED_OPERATIONS = "no_committed_operations"

REASON_TEXT = {
    NO_COMMITTED_OPERATIONS: (
        "This component is costed for its material and tooling, but no manufacturing time has "
        "been committed for it, so nothing is charged for making it. Generate or review its "
        "manufacturing plan and accept or enter the times; until then this component's cost is "
        "incomplete rather than low."
    ),
    NO_GEOMETRY: (
        "The file carries no geometry for this component, so it has no volume, no mass and "
        "nothing to cost as a manufactured part. It is counted and placed but not priced. If "
        "it is bought rather than made, classify it as purchased or standard and state its "
        "unit cost."
    ),
    NOT_ESTIMATED: (
        "No part estimate has been made for this component yet. Cost it as a part first; its "
        "figure will then roll up here."
    ),
    SUB_ASSEMBLY: (
        "This is a sub-assembly, not a part. Its cost is the sum of its own components and is "
        "not counted again here, which would double it."
    ),
    ESTIMATE_FAILED: "Costing this component failed, so it contributes nothing and is listed.",
    CURRENCY_MISMATCH: (
        "This component's cost is denominated in a different currency from the rest of the "
        "assembly. Currencies are never converted, so it is left out of the total."
    ),
    NO_CURRENCY: (
        "A unit cost was entered for this component but no currency was stated. An amount with "
        "no currency is not a cost — nothing here converts between currencies, so it cannot be "
        "added to anything until the currency is said."
    ),
    NO_UNIT_COST: (
        "This component is classified as bought, but no unit cost has been entered for it. No "
        "price is looked up or invented here, so it needs costing input. It is not zero."
    ),
    UNRESOLVED_CLASSIFICATION: (
        "Nobody has said whether this component is made, bought, a standard item or excluded, "
        "and nothing in the CAD file says. Until someone does, it cannot be costed, and it is "
        "not counted as zero."
    ),
}

# --------------------------------------------------------------------- the suggestion

GEOMETRY_EVIDENCE = "measurable_solid_geometry_in_file"
NO_GEOMETRY_EVIDENCE = "no_geometry_in_file"

SUGGESTION_CAVEAT = (
    "The file carries measurable solid geometry for this component, which is what this "
    "application knows how to cost. That is evidence about what can be costed here, not "
    "evidence that the part is made rather than bought: a purchased bearing is modelled in "
    "CAD exactly like a machined housing. No CAD format states make/buy. Confirm it or "
    "change it."
)

NO_GEOMETRY_CAVEAT = (
    "The file carries no geometry for this component, so nothing here can measure it or cost "
    "it as a manufactured part. That is not evidence that it is bought either — it is the "
    "absence of evidence, which is why this is left unresolved rather than guessed."
)


class ClassificationSuggestion(BaseModel):
    """What the system proposes, the evidence for it, and what it is not.

    `confidence` is a three-way classification of which branch fired, not a
    probability, and it is `low` for every make/buy suggestion this module can
    make — because the evidence available is genuinely weak, and saying so is
    the point.
    """

    classification: str
    confidence: str = "low"
    evidence: list[str] = Field(default_factory=list)
    caveat: str = ""


def suggest_classification(definition) -> ClassificationSuggestion | None:
    """Propose a classification for one component definition, or nothing.

    `definition` is a `ComponentDefinition` — taken duck-typed so this module
    imports nothing. A sub-assembly gets no suggestion at all, because it is not
    a thing that is made or bought.

    Deliberately reads exactly two facts, and neither of them is the component's
    name: whether it is a sub-assembly, and whether the file carries geometry
    the application could measure.
    """
    if getattr(definition, "is_assembly", False):
        return None

    if getattr(definition, "geometry_status", None) == "measured":
        return ClassificationSuggestion(
            classification=MANUFACTURED,
            confidence="low",
            evidence=[GEOMETRY_EVIDENCE],
            caveat=SUGGESTION_CAVEAT,
        )

    return ClassificationSuggestion(
        classification=UNRESOLVED,
        confidence="low",
        evidence=[NO_GEOMETRY_EVIDENCE],
        caveat=NO_GEOMETRY_CAVEAT,
    )


# --------------------------------------------------------------------- the profile


class ComponentCostingProfile(BaseModel):
    """Every costing decision about one component definition, in one place.

    Both halves of the classification are kept. `system_classification` is what
    was proposed and is never overwritten; `user_classification` is what an
    engineer said and wins wherever it is present. `classification` is the
    resolved answer, computed rather than stored so the two can never disagree.
    """

    definition_id: str
    name: str = ""
    part_number: str | None = None
    is_assembly: bool = False
    quantity: int = Field(
        default=0,
        description=(
            "How many times this assembly uses the component: an occurrence count. It is not "
            "a production quantity, and the two are multiplied exactly once, in the roll-up."
        ),
    )

    # --- classification: the system's proposal, and the engineer's answer ---
    system_classification: str | None = None
    system_confidence: str | None = None
    system_evidence: list[str] = Field(default_factory=list)
    system_caveat: str = ""
    user_classification: str | None = Field(
        default=None,
        description="What the engineer said. Authoritative. Never written over the system's.",
    )
    classification_reason: str = Field(
        default="", description="Why the engineer classified it this way, when they said."
    )

    # --- material ---
    material: str | None = None
    material_source: str = MATERIAL_UNRESOLVED

    # --- a stated price, for a component that is bought rather than made ---
    unit_cost: float | None = Field(
        default=None,
        description=(
            "Cost of one, as the user states it. Never looked up, never derived from weight, "
            "and never defaulted to zero."
        ),
    )
    currency: str | None = Field(
        default=None,
        description="Required alongside a unit cost. Currencies are never converted.",
    )
    supplier: str | None = None
    supplier_part_number: str | None = None

    # --- exclusion ---
    exclusion_reason: str = Field(
        default="",
        description="Why this component is out of the manufacturing cost. Stated, not implied.",
    )

    notes: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def classification(self) -> str | None:
        """The classification actually used, resolved in one direction only:
        the engineer's answer, then the system's suggestion, then nothing."""
        if self.is_assembly:
            return None
        if self.user_classification:
            return self.user_classification
        if self.system_classification:
            return self.system_classification
        return None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def classification_source(self) -> str:
        if self.user_classification:
            return USER_SELECTED
        if self.system_classification and not self.is_assembly:
            return SYSTEM_SUGGESTED
        return UNCLASSIFIED

    @computed_field  # type: ignore[prop-decorator]
    @property
    def classification_changed(self) -> bool:
        """True only where a suggestion existed and the engineer disagreed."""
        return bool(
            self.system_classification
            and self.user_classification
            and self.system_classification != self.user_classification
        )

    @property
    def is_user_priced(self) -> bool:
        return self.classification in USER_PRICED


def build_profiles(assembly, *, decisions: dict[str, dict] | None = None):
    """Starting profiles for every definition in an assembly.

    Each carries the system's suggestion and the assembly's own facts — name,
    part number, quantity, and the material *if the CAD file states one*. A
    material the file does not state stays unresolved here; it is not filled in
    from a density, a colour or a name.

    `decisions` maps a definition id to whatever the user has already stated,
    and is applied on top. Only the user-owned fields can be set this way: a
    caller cannot overwrite `system_classification`, the quantity or the
    component's identity, so a round trip through the browser can never rewrite
    what the system proposed or how many of something the file contains.
    """
    decisions = decisions or {}
    profiles = []
    for definition in assembly.definitions:
        suggestion = suggest_classification(definition)
        material = getattr(definition, "material", None)
        profile = ComponentCostingProfile(
            definition_id=definition.definition_id,
            name=definition.name,
            part_number=definition.part_number,
            is_assembly=definition.is_assembly,
            quantity=definition.quantity,
            system_classification=suggestion.classification if suggestion else None,
            system_confidence=suggestion.confidence if suggestion else None,
            system_evidence=list(suggestion.evidence) if suggestion else [],
            system_caveat=suggestion.caveat if suggestion else "",
            material=material,
            material_source=MATERIAL_FROM_CAD if material else MATERIAL_UNRESOLVED,
        )
        profiles.append(apply_decision(profile, decisions.get(definition.definition_id)))
    return profiles


USER_OWNED_FIELDS: tuple[str, ...] = (
    "user_classification",
    "classification_reason",
    "material",
    "unit_cost",
    "currency",
    "supplier",
    "supplier_part_number",
    "exclusion_reason",
    "notes",
)
"""The only fields a request may set. Everything else on a profile is either
the system's proposal or a fact read out of the CAD file, and neither may be
rewritten by the thing that is meant to be correcting it."""


def apply_decision(profile: ComponentCostingProfile, decision: dict | None):
    """Overlay what the user stated onto a profile, ignoring anything else."""
    if not decision:
        return profile

    update = {
        field: decision[field]
        for field in USER_OWNED_FIELDS
        if field in decision and decision[field] is not None
    }
    if not update:
        return profile

    if update.get("user_classification") not in (None, *CLASSIFICATIONS):
        raise ValueError(
            f"Unknown classification '{update['user_classification']}' for component "
            f"'{profile.name or profile.definition_id}'. Known: {', '.join(CLASSIFICATIONS)}."
        )
    if "material" in update:
        update["material_source"] = MATERIAL_USER_SELECTED
    if update.get("user_classification") == EXCLUDED and not (
        update.get("exclusion_reason") or profile.exclusion_reason
    ):
        update["exclusion_reason"] = "user_selected"

    return profile.model_copy(update=update)


# --------------------------------------------------------------------- feedback


class ClassificationFeedback(BaseModel):
    """What the system proposed about a component, and what the engineer chose.

    The exact counterpart of `OperationFeedback`, and it exists for the same
    reason and with the same limits: a correction is only recoverable if it was
    recorded while it happened. **Nothing is trained on this.** No model, no
    fitting, no adaptation — a recorded correction never changes a later
    suggestion, and the suggestion rule stays the fixed two-branch test above.

    Frozen and written once, with the estimate. A later edit produces a new
    estimate carrying its own feedback rather than rewriting this one.
    """

    model_config = ConfigDict(frozen=True)

    definition_id: str
    component_name: str = ""
    quantity: int = 0

    system_classification: str | None = None
    system_confidence: str | None = None
    system_evidence: list[str] = Field(default_factory=list)
    user_classification: str | None = None
    final_classification: str | None = None
    classification_changed: bool = False
    reason: str = ""

    # Preserved because they are the user's own numbers, and re-entering them
    # is the cost of losing them.
    unit_cost: float | None = None
    currency: str | None = None
    unit_cost_source: str = COST_NONE
    supplier: str | None = None
    supplier_part_number: str | None = None
    material: str | None = None
    material_source: str = MATERIAL_UNRESOLVED

    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def classification_feedback(profiles) -> list[ClassificationFeedback]:
    """One record per component the system had an opinion about.

    Components with no suggestion — sub-assemblies — produce nothing, because
    "the system said nothing and the engineer said nothing" is not a
    correction and would only dilute the record.
    """
    records = []
    for profile in profiles:
        if not profile.system_classification:
            continue
        records.append(
            ClassificationFeedback(
                definition_id=profile.definition_id,
                component_name=profile.name,
                quantity=profile.quantity,
                system_classification=profile.system_classification,
                system_confidence=profile.system_confidence,
                system_evidence=list(profile.system_evidence),
                user_classification=profile.user_classification,
                final_classification=profile.classification,
                classification_changed=profile.classification_changed,
                reason=profile.classification_reason,
                unit_cost=profile.unit_cost,
                currency=profile.currency,
                unit_cost_source=(
                    COST_USER_ENTERED if profile.unit_cost is not None else COST_NONE
                ),
                supplier=profile.supplier,
                supplier_part_number=profile.supplier_part_number,
                material=profile.material,
                material_source=profile.material_source,
            )
        )
    return records
