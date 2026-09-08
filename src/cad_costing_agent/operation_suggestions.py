"""Recognized features → suggested manufacturing operations.

    CAD → features → **suggestions** → user reviews/edits → existing costing

This layer proposes; it never decides, and it never prices. It sits between
feature recognition and the costing pipeline and is deliberately a dead end
until a human passes something through it:

- **Nothing here computes a cost.** This module does not import `costing`,
  `operation_costing`, `part_cost` or `cad_part_cost`, and no costing module
  imports it. A test asserts both directions.
- **Nothing here invents a time or a rate.** A suggestion carries an operation
  *identity* and its link back to the feature that caused it. Setup and cycle
  time come from the user; machine and labour rates come from the user's rate
  set. Every suggestion therefore reports `costable=False` and says what is
  missing. `to_operation_requirement` is the only bridge into costing and it
  raises rather than defaulting a missing time to anything.
- **Nothing here is learned.** No model, no training, no randomness. Each
  suggestion names the rule that produced it and the evidence that rule stood
  on, and the same features always produce the same suggestions in the same
  order.

Why suggestions are separate from recognition
---------------------------------------------
Recognition answers a geometric question — *is this a through hole?* — which
the CAD file can settle. Turning that into *drill it* is a manufacturing
decision, which the CAD file cannot: it depends on the shop, the batch size,
the fixturing and the material. Keeping the two apart means a wrong suggestion
is visible and correctable without casting doubt on the geometry, and it keeps
a modelling opinion out of a measurement routine.

Confidence, and why a suggestion is never more confident than its feature
------------------------------------------------------------------------
A suggestion's confidence is the **weakest** of three things:

1. the recognized feature's own confidence — a hole whose ends could not be
   classified cannot support a confident operation;
2. a per-rule ceiling — "this planar face needs facing" is a weaker claim than
   "this through hole needs drilling", however cleanly the face was recognized;
3. a per-source ceiling — a feature fitted to polygons is weaker evidence than
   one read from exact surfaces, so `faceted` caps every suggestion at
   `medium`.

The result can only ever be lower than the feature's confidence, never higher.

What is deliberately not suggested
----------------------------------
Where the recognizers do not supply enough evidence, this module emits a
`DeclinedSuggestion` explaining the gap instead of a guess. That covers holes
whose end conditions are ambiguous or enclosed, blends whose geometry was only
partially confirmed, and planar faces too small to be plausibly a machined
datum. Declining loudly is the point: an unexplained absence and an invented
operation are both worse than a stated refusal.
"""

from __future__ import annotations

import time
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from cad_costing_agent.features import describe_feature
from cad_costing_agent.features.model import (
    Confidence,
    Feature,
    FeatureRecognitionResult,
    FeatureType,
)
from cad_costing_agent.rate_sets import OperationRequirement

# ------------------------------------------------------------------ vocabulary

DRILLING = "drilling"
MILLING = "milling"
TURNING = "turning"

# Process names are lowercase because a rate set keys its rates that way and
# resolves them case-insensitively. Using the same spelling here means a
# suggestion can be looked up in a rate set without translation.
KNOWN_OPERATION_TYPES = (DRILLING, MILLING, TURNING)

NOT_COSTED_REASON = (
    "Suggested operation — not yet costed because processing time has not been estimated."
)

# What a suggestion is missing before the existing costing pipeline could price
# it. Both come from the user: this module will not default them.
REQUIRED_FOR_COSTING = ("setup_time_minutes", "cycle_time_minutes")

# A planar face smaller than this share of the largest planar face on the same
# solid is not proposed for facing. Every model has many small incidental flats
# — blend reliefs, boss tops, the sides of a chamfered edge — and proposing an
# operation for each would bury the faces that actually matter. The threshold is
# relative rather than absolute so it holds for a 10 mm insert and a 2 m frame
# alike, and the share that decided each case is recorded in the evidence.
MIN_PLANE_AREA_SHARE = 0.10


class UserAction(str, Enum):
    """What the user did with a suggestion.

    Recorded so a later step can build training data from corrections. The
    system's own suggestion is never overwritten when the user changes it — the
    edit goes to `user_operation_type` and the original stays in
    `operation_type`, because the pair *(suggested, chosen)* is the signal and
    keeping only the second discards it.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"
    ADDED = "added"


class SuggestionOrigin(str, Enum):
    """Who proposed the operation. `user` means there was no suggestion behind it."""

    SYSTEM = "system"
    USER = "user"


_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}

# A fitted faceted feature is weaker evidence than an analytic B-Rep one, so it
# cannot support a `high` suggestion however well the fit went.
_SOURCE_CEILING = {"brep": Confidence.HIGH, "faceted": Confidence.MEDIUM}


def _weakest(*levels: Confidence) -> Confidence:
    return min(levels, key=lambda level: _CONFIDENCE_RANK[level])


# ---------------------------------------------------------------------- models


class OperationSuggestion(BaseModel):
    """One suggested operation, traceable to the one feature that caused it.

    Frozen: a suggestion is a record of what the system proposed from a given
    set of features. The user's response is carried in the `user_*` fields of a
    *copy*, so the original proposal survives the correction.
    """

    model_config = ConfigDict(frozen=True)

    suggestion_id: str = Field(description="Stable within one run, e.g. 'SUG-001'.")

    # --- the feature this came from. The whole point: every suggestion answers
    # --- "why did you suggest this?" by pointing at a recognized feature.
    feature_id: str = Field(description="The recognized feature that caused this, e.g. 'H-001'.")
    component_id: str | None = Field(
        default=None,
        description=(
            "The assembly component whose feature caused this, propagated from the feature. "
            "Null for a single part. This is what keeps component -> feature -> operation "
            "traceable through decomposition."
        ),
    )
    feature_type: str
    feature_subtype: str | None = None
    feature_label: str = Field(description="Readable description, e.g. 'hole (through) Ø10 mm'.")
    feature_confidence: Confidence
    feature_detection_method: str = Field(description="The recognizer rule that found the feature.")
    solid_index: int

    # --- the suggestion itself
    operation_type: str = Field(
        description=(
            "The process the system suggests, lowercase to match rate-set keys. This is the "
            "*system's* proposal and is never overwritten by a user edit."
        )
    )
    operation_name: str = Field(description="Readable default name, e.g. 'Drill Ø10 mm through hole'.")
    confidence: Confidence = Field(
        description="Weakest of the feature's confidence, the rule ceiling and the source ceiling."
    )
    source_type: str = Field(description="'brep' or 'faceted', propagated from the feature.")
    rule_id: str = Field(description="Which suggestion rule fired, e.g. 'through_hole_to_drilling'.")
    reason: str = Field(description="One line saying why, in manufacturing terms.")
    evidence: list[str] = Field(
        default_factory=list,
        description="The specific facts this suggestion stands on, including where in the CAD.",
    )
    alternatives: list[str] = Field(
        default_factory=list,
        description=(
            "Other plausible processes for this feature, offered because the geometry alone "
            "does not settle the choice. Not applied unless the user picks one."
        ),
    )
    dimensions: dict[str, float] = Field(
        default_factory=dict, description="Copied from the feature, for display only."
    )

    # --- viewer linkage: what to highlight when this suggestion is selected
    source_faces: list[int] = Field(default_factory=list)
    source_facets: list[int] = Field(default_factory=list)

    # --- review state, for the UI now and the learning loop later
    editable: bool = True
    origin: SuggestionOrigin = SuggestionOrigin.SYSTEM
    user_action: UserAction = UserAction.PENDING
    user_operation_type: str | None = Field(
        default=None,
        description="What the user changed the process to. The suggestion itself is unchanged.",
    )

    # --- costability. Always false here: a suggestion is not a priced operation.
    costable: bool = Field(
        default=False,
        description="Always false. A suggestion carries no time, so it cannot be costed.",
    )
    missing_for_costing: list[str] = Field(default_factory=lambda: list(REQUIRED_FOR_COSTING))
    not_costable_reason: str = NOT_COSTED_REASON
    priced_by_rate_set: bool | None = Field(
        default=None,
        description=(
            "Whether the selected rate set prices this process. None when no rate set was "
            "supplied to compare against. Rates are never invented either way."
        ),
    )

    @property
    def effective_operation_type(self) -> str:
        """The process that would actually be costed: the user's edit if any, else the suggestion."""
        return self.user_operation_type or self.operation_type


class DeclinedSuggestion(BaseModel):
    """A recognized feature the rules deliberately did not turn into an operation.

    Reported rather than silently dropped, so "no suggestion" is a visible,
    explained decision instead of a gap the reader has to notice.
    """

    model_config = ConfigDict(frozen=True)

    feature_id: str
    component_id: str | None = Field(
        default=None,
        description="The assembly component this feature belongs to, or null for a part.",
    )
    feature_type: str
    feature_subtype: str | None = None
    feature_label: str
    feature_confidence: Confidence
    source_type: str
    rule_id: str
    reason: str


class OperationSuggestionResult(BaseModel):
    """Everything `/cad/operations/suggest` returns for one uploaded file."""

    filename: str
    source_format: str
    representation: str = Field(description="'brep' or 'faceted', from recognition.")
    units: str = "mm"

    features: list[Feature] = Field(description="The recognized features, unchanged.")
    suggestions: list[OperationSuggestion]
    declined: list[DeclinedSuggestion]

    feature_count: int = 0
    suggestion_counts: dict[str, int] = Field(
        default_factory=dict, description="Suggestions per operation type."
    )
    summary: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list, description="Which suggestion rules fired.")

    rate_set_id: str | None = None
    rate_set_version: str | None = None
    priced_processes: list[str] = Field(
        default_factory=list, description="Process types the compared rate set prices."
    )

    costable: bool = Field(
        default=False, description="Always false: no suggestion in this result is a priced operation."
    )
    costing_note: str = (
        "No suggestion here is costed. Suggestions carry an operation identity and its link to "
        "a CAD feature; processing times come from you and rates come from your rate set."
    )
    elapsed_ms: float = 0.0


# ----------------------------------------------------------------------- rules


@dataclass(frozen=True)
class _Proposal:
    """A rule's positive outcome."""

    operation_type: str
    rule_id: str
    reason: str
    ceiling: Confidence
    alternatives: tuple[str, ...] = ()
    extra_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Decline:
    """A rule's refusal, with the gap that caused it."""

    rule_id: str
    reason: str


@dataclass(frozen=True)
class _Context:
    """Per-model facts a rule needs that a single feature does not carry."""

    largest_plane_area_by_solid: dict[int, float]


# --- holes -------------------------------------------------------------------

# Hole subtypes the analytic recognizer produces are 'through', 'blind',
# 'counterbore', 'ambiguous', 'enclosed', and any of the first two suffixed
# '_countersunk'. The faceted recognizer produces 'through', 'blind' or
# 'ambiguous'. Both are handled here, and anything unrecognized declines.
def _hole_rule(feature: Feature, context: _Context) -> _Proposal | _Decline:
    subtype = (feature.subtype or "").lower()
    countersunk = subtype.endswith("_countersunk")
    base = subtype[: -len("_countersunk")] if countersunk else subtype

    if base == "through":
        reason = "cylindrical through-hole detected, so it can be drilled from one side"
    elif base == "blind":
        reason = "cylindrical hole with one closed end detected, so it can be drilled to depth"
    elif base == "counterbore":
        return _Proposal(
            operation_type=DRILLING,
            rule_id="counterbored_hole_to_drilling",
            reason=(
                "coaxial wide and narrow bores joined by an annular floor, so the pilot is "
                "drilled and the counterbore opened out"
            ),
            ceiling=Confidence.HIGH,
            # The wide step is as often milled or bored as drilled; the geometry
            # does not settle which, so the alternative is offered rather than
            # a second operation being invented.
            alternatives=(MILLING,),
            extra_evidence=(
                "the counterbore step itself may be milled or bored rather than drilled — the "
                "geometry does not decide which",
            ),
        )
    elif base == "ambiguous":
        return _Decline(
            rule_id="ambiguous_hole_declined",
            reason=(
                "the recognizer could not classify this bore's end conditions, so whether it "
                "is drilled through, drilled to depth, or enclosed and unreachable is not "
                "established by the geometry. No operation is suggested."
            ),
        )
    elif base == "enclosed":
        return _Decline(
            rule_id="enclosed_hole_declined",
            reason=(
                "both ends of this bore are closed, so no tool can reach it as modelled. It is "
                "usually an internal void or a modelling artefact rather than a machined hole, "
                "so no operation is suggested."
            ),
        )
    else:
        return _Decline(
            rule_id="unknown_hole_subtype_declined",
            reason=(
                f"hole subtype '{feature.subtype}' has no suggestion rule, so no operation is "
                "suggested rather than one being guessed."
            ),
        )

    extra: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    if countersunk:
        extra = (
            "a coaxial cone at the open end makes this hole countersunk; countersinking is a "
            "separate cut and is not suggested as its own operation here",
        )
    return _Proposal(
        operation_type=DRILLING,
        rule_id=f"{base}_hole_to_drilling",
        reason=reason,
        ceiling=Confidence.HIGH,
        alternatives=alternatives,
        extra_evidence=extra,
    )


# --- cylinders ---------------------------------------------------------------


def _cylinder_rule(feature: Feature, context: _Context) -> _Proposal | _Decline:
    subtype = (feature.subtype or "").lower()

    if subtype == "shaft":
        # An external cylinder is the classic turned form, but a shaft feature on
        # a prismatic part is just as often milled as a boss. The geometry cannot
        # tell a turned bar from a milled boss, so turning leads and milling is
        # offered rather than the choice being made silently.
        return _Proposal(
            operation_type=TURNING,
            rule_id="external_cylinder_to_turning",
            reason=(
                "external cylindrical surface detected, which is the form turning produces"
            ),
            ceiling=Confidence.HIGH,
            alternatives=(MILLING,),
            extra_evidence=(
                "whether this is turned on a lathe or milled as a boss depends on the part's "
                "overall form and the shop's machines, which the CAD geometry does not state",
            ),
        )

    if subtype == "bore":
        # A complete internal cylinder the hole recognizer did not claim: its
        # ends were never classified, so this is weaker ground than a hole.
        return _Proposal(
            operation_type=DRILLING,
            rule_id="internal_cylinder_to_drilling",
            reason=(
                "internal cylindrical surface detected, but its end conditions were not "
                "classified, so drilling is suggested only as the most likely process"
            ),
            ceiling=Confidence.MEDIUM,
            alternatives=(MILLING, TURNING),
            extra_evidence=(
                "no hole rule claimed this surface, so it carries no through/blind "
                "classification; boring or milling may suit it better",
            ),
        )

    return _Decline(
        rule_id="unknown_cylinder_subtype_declined",
        reason=(
            f"cylinder subtype '{feature.subtype}' has no suggestion rule, so no operation is "
            "suggested."
        ),
    )


# --- pockets and slots -------------------------------------------------------


def _pocket_rule(feature: Feature, context: _Context) -> _Proposal | _Decline:
    kind = feature.feature_type.value
    return _Proposal(
        operation_type=MILLING,
        rule_id=f"{kind}_to_milling",
        reason=(
            f"a planar floor enclosed by rising walls makes this a {kind}, which is cut by "
            "milling"
        ),
        ceiling=Confidence.HIGH,
    )


# --- planes ------------------------------------------------------------------


def _plane_rule(feature: Feature, context: _Context) -> _Proposal | _Decline:
    area = feature.dimensions.get("area_mm2")
    if area is None or area <= 0:
        return _Decline(
            rule_id="plane_without_area_declined",
            reason=(
                "no area was determined for this planar face, so whether it is a significant "
                "machined surface cannot be judged. No operation is suggested."
            ),
        )

    largest = context.largest_plane_area_by_solid.get(feature.solid_index, 0.0)
    share = area / largest if largest > 0 else 1.0
    if share < MIN_PLANE_AREA_SHARE:
        return _Decline(
            rule_id="minor_plane_declined",
            reason=(
                f"this planar face is {share:.1%} of the largest planar face on its solid, below "
                f"the {MIN_PLANE_AREA_SHARE:.0%} at which a flat is treated as a significant "
                "machined surface. It is more likely an incidental flat than a face to be "
                "milled, so no operation is suggested."
            ),
        )

    # Deliberately capped at medium however cleanly the face was recognized:
    # geometry establishes that a flat surface exists, not that any material has
    # to come off it. A face may already be the stock surface.
    return _Proposal(
        operation_type=MILLING,
        rule_id="significant_plane_to_milling",
        reason=(
            "a significant planar face was detected, which is faced or milled if material has "
            "to be removed from it"
        ),
        ceiling=Confidence.MEDIUM,
        extra_evidence=(
            f"this face is {share:.1%} of the largest planar face on its solid, at or above the "
            f"{MIN_PLANE_AREA_SHARE:.0%} significance threshold",
            "whether this face needs material removed is a process decision, not a geometric "
            "fact — the CAD cannot say whether it is already the stock surface",
        ),
    )


# --- blends ------------------------------------------------------------------


def _blend_rule(feature: Feature, context: _Context) -> _Proposal | _Decline:
    kind = feature.feature_type.value
    # A blend is only proposed when its own recognition was fully confirmed. A
    # `medium` fillet is one whose opposite tangent neighbour was never found,
    # which is exactly the case where the "blend" may be an ordinary curved
    # wall — not something to propose a finishing pass for.
    if feature.confidence is not Confidence.HIGH:
        return _Decline(
            rule_id=f"unconfirmed_{kind}_declined",
            reason=(
                f"this {kind} was recognized at confidence {feature.confidence.value}, so the "
                "blend itself is not fully confirmed. A finishing pass is not suggested on "
                "partial evidence."
            ),
        )

    return _Proposal(
        operation_type=MILLING,
        rule_id=f"{kind}_to_finishing_milling",
        reason=(
            f"a fully confirmed {kind} was detected, which is produced or finished by milling"
        ),
        # Capped at medium: a blend is very often formed by the tool radius of an
        # adjacent milling cut rather than needing an operation of its own.
        ceiling=Confidence.MEDIUM,
        extra_evidence=(
            f"this {kind} may already be produced by the cutter radius of an adjacent milling "
            "operation, in which case it needs no separate pass",
        ),
    )


_RULES = {
    FeatureType.HOLE: _hole_rule,
    FeatureType.CYLINDER: _cylinder_rule,
    FeatureType.POCKET: _pocket_rule,
    FeatureType.SLOT: _pocket_rule,
    FeatureType.PLANE: _plane_rule,
    FeatureType.FILLET: _blend_rule,
    FeatureType.CHAMFER: _blend_rule,
}


# ---------------------------------------------------------------- the engine


def _dimension_evidence(feature: Feature) -> str | None:
    """The measured dimensions, in the order they read best."""
    order = [
        ("diameter_mm", "Ø", "mm"),
        ("depth_mm", "depth", "mm"),
        ("length_mm", "length", "mm"),
        ("width_mm", "width", "mm"),
        ("radius_mm", "R", "mm"),
        ("area_mm2", "area", "mm²"),
        ("floor_area_mm2", "floor area", "mm²"),
        ("angle_deg", "angle", "°"),
        ("counterbore_diameter_mm", "counterbore Ø", "mm"),
        ("counterbore_depth_mm", "counterbore depth", "mm"),
        ("countersink_diameter_mm", "countersink Ø", "mm"),
        ("countersink_angle_deg", "countersink angle", "°"),
    ]
    parts = [
        f"{label}{feature.dimensions[key]:g} {unit}"
        if label in ("Ø", "R")
        else f"{label} {feature.dimensions[key]:g} {unit}"
        for key, label, unit in order
        if key in feature.dimensions
    ]
    return ", ".join(parts) if parts else None


def _location_evidence(feature: Feature) -> str | None:
    """Where in the CAD this came from — what a viewer would highlight."""
    if feature.source_faces:
        shown = ", ".join(str(index) for index in feature.source_faces[:12])
        if len(feature.source_faces) > 12:
            shown += f", … ({len(feature.source_faces)} faces in total)"
        return f"CAD face(s) {shown}"
    if feature.source_facets:
        return f"{len(feature.source_facets)} polygon(s) of the faceted body"
    return None


def _operation_name(feature: Feature, operation_type: str) -> str:
    """A readable default name the user can overwrite."""
    verb = {DRILLING: "Drill", MILLING: "Mill", TURNING: "Turn"}.get(
        operation_type, operation_type.capitalize()
    )
    return f"{verb} {describe_feature(feature)}"


def _source_sentence(source_type: str) -> str:
    if source_type == "faceted":
        return (
            "this feature was reconstructed by fitting to polygons rather than read from exact "
            "surfaces, so the suggestion is capped at medium confidence"
        )
    return "this feature was read from the file's exact B-Rep surfaces"


def _largest_plane_area_by_solid(features: Iterable[Feature]) -> dict[int, float]:
    largest: dict[int, float] = {}
    for feature in features:
        if feature.feature_type is not FeatureType.PLANE:
            continue
        area = feature.dimensions.get("area_mm2")
        if area is None:
            continue
        if area > largest.get(feature.solid_index, 0.0):
            largest[feature.solid_index] = float(area)
    return largest


def suggest_operations(
    recognition: FeatureRecognitionResult,
    *,
    priced_processes: Collection[str] | None = None,
    rate_set_id: str | None = None,
    rate_set_version: str | None = None,
) -> OperationSuggestionResult:
    """Turn recognized features into suggested operations. Deterministic.

    `priced_processes` is only used to *report* whether the user's rate set
    already prices a suggested process. It never changes what is suggested and
    no rate is ever read or invented from it.
    """
    started = time.perf_counter()

    priced = (
        None
        if priced_processes is None
        else {process.strip().lower() for process in priced_processes}
    )

    context = _Context(largest_plane_area_by_solid=_largest_plane_area_by_solid(recognition.features))

    suggestions: list[OperationSuggestion] = []
    declined: list[DeclinedSuggestion] = []
    rules_fired: list[str] = []

    # Features arrive already sorted deterministically by the recognizer
    # (most manufacturing-relevant type first, then solid, then lowest face),
    # so iterating in order gives stable suggestion ids without re-sorting.
    for feature in recognition.features:
        rule = _RULES.get(feature.feature_type)
        label = describe_feature(feature)

        if rule is None:
            declined.append(
                DeclinedSuggestion(
                    feature_id=feature.feature_id,
                    component_id=feature.component_id,
                    feature_type=feature.feature_type.value,
                    feature_subtype=feature.subtype,
                    feature_label=label,
                    feature_confidence=feature.confidence,
                    source_type=feature.source_type,
                    rule_id="no_rule_for_feature_type_declined",
                    reason=(
                        f"no suggestion rule covers feature type '{feature.feature_type.value}', "
                        "so no operation is suggested."
                    ),
                )
            )
            continue

        outcome = rule(feature, context)

        if isinstance(outcome, _Decline):
            declined.append(
                DeclinedSuggestion(
                    feature_id=feature.feature_id,
                    component_id=feature.component_id,
                    feature_type=feature.feature_type.value,
                    feature_subtype=feature.subtype,
                    feature_label=label,
                    feature_confidence=feature.confidence,
                    source_type=feature.source_type,
                    rule_id=outcome.rule_id,
                    reason=outcome.reason,
                )
            )
            continue

        source_ceiling = _SOURCE_CEILING.get(feature.source_type, Confidence.MEDIUM)
        confidence = _weakest(feature.confidence, outcome.ceiling, source_ceiling)

        evidence = [
            item
            for item in (
                _dimension_evidence(feature),
                _location_evidence(feature),
                f"recognized by rule '{feature.detection_method}' at confidence "
                f"{feature.confidence.value}",
                _source_sentence(feature.source_type),
                *outcome.extra_evidence,
            )
            if item
        ]

        is_priced = None if priced is None else outcome.operation_type in priced
        not_costable = NOT_COSTED_REASON
        if is_priced is False:
            not_costable += (
                f" The selected rate set also prices no '{outcome.operation_type}' process, so a "
                "rate would have to be added to it before this could be costed."
            )

        suggestions.append(
            OperationSuggestion(
                suggestion_id=f"SUG-{len(suggestions) + 1:03d}",
                feature_id=feature.feature_id,
                component_id=feature.component_id,
                feature_type=feature.feature_type.value,
                feature_subtype=feature.subtype,
                feature_label=label,
                feature_confidence=feature.confidence,
                feature_detection_method=feature.detection_method,
                solid_index=feature.solid_index,
                operation_type=outcome.operation_type,
                operation_name=_operation_name(feature, outcome.operation_type),
                confidence=confidence,
                source_type=feature.source_type,
                rule_id=outcome.rule_id,
                reason=outcome.reason,
                evidence=evidence,
                alternatives=list(outcome.alternatives),
                dimensions=dict(feature.dimensions),
                source_faces=list(feature.source_faces),
                source_facets=list(feature.source_facets),
                priced_by_rate_set=is_priced,
            )
        )
        if outcome.rule_id not in rules_fired:
            rules_fired.append(outcome.rule_id)

    counts: dict[str, int] = {}
    for suggestion in suggestions:
        counts[suggestion.operation_type] = counts.get(suggestion.operation_type, 0) + 1

    warnings = _warnings_for(recognition, suggestions, declined, priced)

    return OperationSuggestionResult(
        filename=recognition.filename,
        source_format=recognition.source_format,
        representation=recognition.representation,
        units=recognition.units,
        features=list(recognition.features),
        suggestions=suggestions,
        declined=declined,
        feature_count=len(recognition.features),
        suggestion_counts=counts,
        summary=_summarize(suggestions),
        warnings=warnings,
        rules=rules_fired,
        rate_set_id=rate_set_id,
        rate_set_version=rate_set_version,
        priced_processes=sorted(priced) if priced is not None else [],
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
    )


def _summarize(suggestions: list[OperationSuggestion]) -> list[str]:
    """Group identical suggestions into "6 × drilling — through hole Ø10 mm" lines."""
    grouped: dict[tuple[str, str, str], int] = {}
    order: list[tuple[str, str, str]] = []
    for suggestion in suggestions:
        key = (suggestion.operation_type, suggestion.feature_label, suggestion.confidence.value)
        if key not in grouped:
            order.append(key)
        grouped[key] = grouped.get(key, 0) + 1
    return [
        f"{grouped[key]} × {key[0]} — from {key[1]} — confidence {key[2]}" for key in order
    ]


def _warnings_for(
    recognition: FeatureRecognitionResult,
    suggestions: list[OperationSuggestion],
    declined: list[DeclinedSuggestion],
    priced: set[str] | None,
) -> list[str]:
    """Say plainly what these suggestions are not."""
    warnings = [
        "These are suggestions, not decisions. Nothing here is costed and nothing is applied to "
        "an estimate until you accept it and enter the processing times yourself.",
        "No setup or cycle time is suggested. This step does not estimate machining time, and a "
        "time invented here would silently become money.",
    ]

    if recognition.representation == "faceted":
        warnings.append(
            "This is a faceted export, so every feature behind these suggestions was fitted to "
            "polygons rather than read from exact surfaces. Suggestions from it are capped at "
            "medium confidence."
        )

    if declined:
        warnings.append(
            f"{len(declined)} recognized feature(s) produced no suggestion because the geometry "
            "does not support one. Each is listed with the reason rather than being dropped."
        )

    if priced is not None:
        unpriced = sorted(
            {
                suggestion.operation_type
                for suggestion in suggestions
                if suggestion.priced_by_rate_set is False
            }
        )
        if unpriced:
            warnings.append(
                "The selected rate set prices no rate for: "
                + ", ".join(unpriced)
                + ". Add these processes to the rate set before costing operations that use them."
            )

    # Recognition's own caveats matter here too — a model whose pocket rule
    # failed will be missing pocket suggestions for a reason worth repeating.
    warnings.extend(recognition.warnings)
    return warnings


# ------------------------------------------------------- the bridge to costing


def to_operation_requirement(
    suggestion: OperationSuggestion,
    *,
    setup_time_minutes: float | None,
    cycle_time_minutes: float | None,
    quantity: int = 1,
    operation_id: str | None = None,
    operation_name: str | None = None,
) -> OperationRequirement:
    """Convert an accepted suggestion into a costable `OperationRequirement`.

    The **only** path from a suggestion into the costing pipeline, and it is a
    gate rather than a conversion: a suggestion carries no processing time, so
    this raises `ValueError` unless the caller supplies one. Defaulting a
    missing time — to zero, to a guess, to a per-process constant — would turn
    a suggestion into money without anyone deciding to, which is the specific
    failure this function exists to prevent.

    A rejected suggestion is refused too: costing something the user removed
    would defeat the review step.

    Rates are not involved. They stay with the rate set, resolved by
    `process_type` exactly as a hand-entered operation's are.
    """
    if suggestion.user_action is UserAction.REJECTED:
        raise ValueError(
            f"Suggestion {suggestion.suggestion_id} was rejected by the user, so it must not be "
            "costed."
        )

    if cycle_time_minutes is None or cycle_time_minutes <= 0:
        raise ValueError(
            f"Suggestion {suggestion.suggestion_id} ({suggestion.effective_operation_type}) has "
            "no cycle time. This step does not estimate machining time, so a cycle time must be "
            "entered before this operation can be costed. Nothing is assumed."
        )

    if setup_time_minutes is None or setup_time_minutes < 0:
        raise ValueError(
            f"Suggestion {suggestion.suggestion_id} ({suggestion.effective_operation_type}) has "
            "no setup time. Enter one — zero is allowed, but it has to be stated rather than "
            "assumed."
        )

    return OperationRequirement(
        operation_id=operation_id or suggestion.suggestion_id,
        operation_name=operation_name or suggestion.operation_name,
        process_type=suggestion.effective_operation_type,
        setup_time_minutes=setup_time_minutes,
        cycle_time_minutes=cycle_time_minutes,
        quantity=quantity,
    )


def record_user_decision(
    suggestion: OperationSuggestion,
    action: UserAction,
    *,
    operation_type: str | None = None,
) -> OperationSuggestion:
    """Return a copy carrying the user's decision, with the original intact.

    The system's `operation_type` is never overwritten. An edit is recorded in
    `user_operation_type` alongside it, so the pair *(suggested, chosen)* is
    preserved — that pair is the entire value of the correction to a later
    learning step, and keeping only the user's answer would throw it away.
    """
    if action is UserAction.EDITED:
        if not operation_type or not operation_type.strip():
            raise ValueError("An edited suggestion needs the operation type the user chose.")
        chosen = operation_type.strip().lower()
        if chosen == suggestion.operation_type:
            # Not an edit: the user picked what was suggested.
            return suggestion.model_copy(
                update={"user_action": UserAction.ACCEPTED, "user_operation_type": None}
            )
        return suggestion.model_copy(
            update={"user_action": UserAction.EDITED, "user_operation_type": chosen}
        )

    if operation_type is not None:
        raise ValueError(
            f"An operation type may only be supplied with '{UserAction.EDITED.value}'."
        )

    return suggestion.model_copy(update={"user_action": action, "user_operation_type": None})


__all__ = [
    "DRILLING",
    "MILLING",
    "TURNING",
    "KNOWN_OPERATION_TYPES",
    "MIN_PLANE_AREA_SHARE",
    "NOT_COSTED_REASON",
    "REQUIRED_FOR_COSTING",
    "DeclinedSuggestion",
    "OperationSuggestion",
    "OperationSuggestionResult",
    "SuggestionOrigin",
    "UserAction",
    "record_user_decision",
    "suggest_operations",
    "to_operation_requirement",
]
