"""Setups proposed from tool approach directions — offered, and applied only by a person.

    plan + approach directions (features/approach.py) → **a proposed grouping** → apply

A plan starts with one setup holding everything, assumed at low confidence,
because nothing used to give real evidence of fixturing. The direction a tool
must come from to reach each feature *is* evidence: on a 3-axis mill the tool
axis is fixed, so features that can only be reached from different directions
cannot be cut in one clamping. This module turns the approach evidence into a
proposed grouping of the plan's operations and, when a person asks, applies it.

What it does not do
-------------------
- **It never changes a plan on its own.** `build_plan` is untouched and still
  produces one `system_assumed` setup. `propose_setups` returns a proposal
  beside the plan; only `apply_setup_proposal`, called because an engineer
  pressed *Apply*, changes anything.
- **It never overwrites the system's grouping.** Applying writes `user_setup_id`
  through `ManufacturingPlan.with_operation_in_setup` — the same regrouping an
  engineer's hand move uses — and `system_setup_id` stays where `build_plan`
  put it.
- **It invents no time.** A proposed setup's estimated time is derived the way
  `build_plan` derives the default setup's — the largest per-process setup
  baseline among its operations, from the time profile those operations were
  estimated under — and it is *uncommitted* until someone accepts it.
- **It never claims more than medium confidence**, and claims low whenever an
  operation could not be placed. A clear ray is necessary for access, not
  sufficient (`features.approach.LIMITATIONS`).
- **It never places an operation it has no evidence for.** An unreachable
  feature, a feature with no approach rule, a line with no feature, or a
  process with no tool approach (turning, welding) is listed with its reason
  and left in whatever setup it is already in.
- **It never crosses components.** A plan is one part's or one component's;
  this module sees one plan and one shape at a time.

How a grouping is chosen
------------------------
Each operation is coverable from the directions along which *every* feature it
machines is reachable. Directions are then chosen greedily: the one covering
the most still-uncovered operations; ties go to the one where more of those
operations are approached head-on rather than by a tool's flank, then to a
fixed order (+Z, −Z, +X, −X, +Y, −Y, then any other direction by name). An
operation lands in the earliest chosen direction that reaches it head-on, else
the earliest that reaches it at all. A greedy cover is deterministic and
usually small; it is **not guaranteed minimal**, and the proposal says so.

The machine is a 3-axis mill: one setup per distinct direction. A 4-axis
machine with a rotary table could hold every direction perpendicular to its
rotary axis in one setup, and a 5-axis machine most directions in one or two;
neither is modelled here, and the proposal says so rather than pretending.

Imports `manufacturing_plan`, `operation_suggestions` (for the origin and
action enums a setup records) and `features` only. No costing module imports
this one, it imports no costing arithmetic, and only `main.py` imports it.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from cad_costing_agent.features.approach import (
    FLANK,
    LIMITATIONS,
    NO_APPROACH,
    PRIMARY,
    FeatureApproach,
    direction_label,
    direction_order,
)
from cad_costing_agent.features.model import Confidence, Feature, Point3D
from cad_costing_agent.manufacturing_plan import (
    SETUP_TIME_DERIVATION,
    ManufacturingPlan,
    ManufacturingSetup,
    PlannedOperation,
    _dominant_process,
    _setup_baseline_for,
)
from cad_costing_agent.operation_suggestions import SuggestionOrigin, UserAction

APPROACH_GROUPING = "approach_direction"
"""`ManufacturingSetup.system_grouping` for a setup created by applying this proposal."""

TOOL_APPROACH_PROCESSES = ("milling", "drilling")
"""Processes whose tool comes at the part along one direction per setup."""

MACHINE = "3_axis_mill"
MACHINE_NOTE = (
    "Proposed for a 3-axis mill: the tool axis is fixed, so every distinct approach direction is "
    "its own setup. A 4-axis machine with a rotary table could reach every direction "
    "perpendicular to its rotary axis in one setup, and a 5-axis machine most directions in one "
    "or two. Neither is modelled — this proposal does not reduce the count for them."
)
ALGORITHM_NOTE = (
    "Directions are chosen greedily — the one reaching the most remaining operations first, ties "
    "to the one approaching more of them head-on, then to the fixed order +Z, −Z, +X, −X, +Y, −Y. "
    "Deterministic, and usually small, but not guaranteed to be the fewest setups possible."
)
PROPOSAL_NOTE = (
    "A proposal. Nothing in the plan changes until you apply it, and applying it moves operations "
    "into new setups the way moving them by hand does — the system's original grouping is kept "
    "beside it. The new setups' times are estimates, uncommitted until you accept them."
)

# Why an operation was not placed in any proposed setup.
REMOVED = "removed"
NOT_IN_ROUTE = "not_in_route"
NOT_A_TOOL_APPROACH = "not_a_tool_approach_process"
NO_FEATURE_LINK = "no_feature_link"
FEATURE_NOT_FOUND = "feature_not_found"
UNREACHABLE = "unreachable"

_SETUP_NUMBER = re.compile(r"SETUP-\d{3,}$")


class PlacedOperation(BaseModel):
    operation_id: str
    operation_name: str
    feature_ids: list[str]
    access: str = Field(description="'primary' (head-on) or 'flank' (side milling).")
    evidence: str


class ProposedSetup(BaseModel):
    position: int = Field(ge=1)
    direction: Point3D
    direction_key: str
    direction_label: str
    operation_ids: list[str]
    feature_ids: list[str]
    operations: list[PlacedOperation]
    marker_origin: Point3D | None = Field(
        default=None,
        description="Where one of this setup's rays starts — a pointer the viewer can draw the direction at.",
    )


class UnplacedOperation(BaseModel):
    operation_id: str
    operation_name: str
    feature_ids: list[str]
    status: str
    reason: str


class SetupProposal(BaseModel):
    """Proposed setups for one plan, with the evidence behind each. Stores nothing."""

    kind: str = "proposal"
    available: bool = True
    unavailable_reason: str | None = None
    component_id: str | None = None
    machine: str = MACHINE
    machine_note: str = MACHINE_NOTE
    algorithm_note: str = ALGORITHM_NOTE
    note: str = PROPOSAL_NOTE
    confidence: Confidence = Confidence.LOW
    confidence_reason: str = ""
    considered_operation_count: int = 0
    setups: list[ProposedSetup] = Field(default_factory=list)
    unplaced: list[UnplacedOperation] = Field(default_factory=list)
    features: list[FeatureApproach] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=lambda: list(LIMITATIONS))

    @field_validator("confidence")
    @classmethod
    def _never_high(cls, value: Confidence) -> Confidence:
        # A clear straight ray is necessary for access, not sufficient; nothing
        # here can justify more than medium.
        if value is Confidence.HIGH:
            raise ValueError("A setup proposal is never high confidence.")
        return value


class AppliedProposal(BaseModel):
    plan: ManufacturingPlan
    proposal: SetupProposal
    created_setup_ids: list[str]
    removed_setup_ids: list[str] = Field(
        default_factory=list,
        description="Setups the apply emptied, removed as the Remove button would remove them.",
    )
    moved_operation_ids: list[str]
    left_in_place: list[str] = Field(
        default_factory=list, description="Operations the proposal did not place; their setup is unchanged."
    )
    note: str = (
        "Each moved operation still records the setup the system first put it in, with your move "
        "recorded beside it. The new setups' times are estimates — accept them or enter your own "
        "before they are costed."
    )


# ---------------------------------------------------------------- what is considered


def _considered(plan: ManufacturingPlan, operation: PlannedOperation) -> tuple[bool, str, str]:
    """(considered, status, reason) for one operation."""
    if operation.user_action is UserAction.REJECTED:
        return False, REMOVED, "removed from the plan, so it is not grouped"
    if not plan.in_route(operation) or plan.is_as_formed(operation):
        return False, NOT_IN_ROUTE, "not costed on the chosen route, so it is not grouped"
    process = operation.operation_type or ""
    if process not in TOOL_APPROACH_PROCESSES:
        return (
            False,
            NOT_A_TOOL_APPROACH,
            (
                "turning has no milling approach: the part turns about the spindle, so it is "
                "left where it is"
                if process == "turning"
                else f"'{process or 'no process'}' is not cut by a tool approaching along one "
                "direction, so it is left where it is"
            ),
        )
    if not operation.feature_ids:
        return False, NO_FEATURE_LINK, "linked to no recognized feature, so no direction can be read for it"
    return True, "", ""


def features_to_analyse(plan: ManufacturingPlan, features: list[Feature]) -> list[Feature]:
    """The recognized features the plan's considered operations machine, in recognition order."""
    wanted: set[str] = set()
    for operation in plan.operations:
        considered, _, _ = _considered(plan, operation)
        if considered:
            wanted.update(operation.feature_ids)
    return [feature for feature in features if feature.feature_id in wanted]


# ---------------------------------------------------------------- the proposal


def unavailable(reason: str, *, component_id: str | None = None) -> SetupProposal:
    return SetupProposal(
        available=False,
        unavailable_reason=reason,
        component_id=component_id,
        confidence=Confidence.LOW,
        confidence_reason="No proposal: " + reason,
    )


def propose_setups(
    plan: ManufacturingPlan,
    approaches: list[FeatureApproach],
    *,
    component_id: str | None = None,
) -> SetupProposal:
    """Group the plan's operations by the directions their features are reachable from."""
    by_feature = {approach.feature_id: approach for approach in approaches}
    unplaced: list[UnplacedOperation] = []
    # operation id -> {direction key: kind}
    coverable: dict[str, dict[str, str]] = {}
    vectors: dict[str, Point3D] = {}
    origins: dict[tuple[str, str], Point3D | None] = {}
    considered_count = 0
    weak = False

    for operation in plan.ordered:
        considered, status, reason = _considered(plan, operation)
        if not considered:
            if status != REMOVED:
                unplaced.append(_unplaced(operation, status, reason))
            continue
        considered_count += 1

        missing = [fid for fid in operation.feature_ids if fid not in by_feature]
        if missing:
            weak = True
            unplaced.append(
                _unplaced(
                    operation,
                    FEATURE_NOT_FOUND,
                    f"feature(s) {', '.join(missing)} are not in this file's recognition, so no "
                    "direction can be read for them",
                )
            )
            continue

        approaches_here = [by_feature[fid] for fid in operation.feature_ids]
        silent = [a for a in approaches_here if a.status == NO_APPROACH]
        if silent:
            weak = True
            unplaced.append(
                _unplaced(
                    operation,
                    NO_APPROACH,
                    "; ".join(f"{a.feature_id}: {a.reason}" for a in silent),
                )
            )
            continue

        shared: dict[str, str] | None = None
        for approach in approaches_here:
            reachable = {
                c.direction_key: c.kind for c in approach.candidates if c.reachable
            }
            for candidate in approach.candidates:
                if candidate.reachable:
                    vectors.setdefault(candidate.direction_key, candidate.direction)
                    origins.setdefault((operation.operation_id, candidate.direction_key), candidate.origin)
            if shared is None:
                shared = reachable
            else:
                shared = {
                    key: (PRIMARY if shared[key] == PRIMARY and kind == PRIMARY else FLANK)
                    for key, kind in reachable.items()
                    if key in shared
                }
        if not shared:
            weak = True
            unplaced.append(
                _unplaced(
                    operation,
                    UNREACHABLE,
                    "; ".join(f"{a.feature_id}: {a.reason}" for a in approaches_here),
                )
            )
            continue
        coverable[operation.operation_id] = shared

    # Greedy cover, deterministic tie-breaks.
    uncovered = set(coverable)
    chosen: list[str] = []
    while uncovered:
        keys = sorted({key for op in uncovered for key in coverable[op]}, key=direction_order)
        best = max(
            keys,
            key=lambda key: (
                sum(1 for op in uncovered if key in coverable[op]),
                sum(1 for op in uncovered if coverable[op].get(key) == PRIMARY),
                -keys.index(key),
            ),
        )
        chosen.append(best)
        uncovered -= {op for op in uncovered if best in coverable[op]}

    assignment: dict[str, str] = {}
    for operation_id, options in coverable.items():
        head_on = [key for key in chosen if options.get(key) == PRIMARY]
        assignment[operation_id] = head_on[0] if head_on else next(k for k in chosen if k in options)

    operations = {o.operation_id: o for o in plan.operations}
    setups: list[ProposedSetup] = []
    for key in chosen:
        members = [o for o in plan.ordered if assignment.get(o.operation_id) == key]
        if not members:
            continue
        placed = []
        for operation in members:
            kind = coverable[operation.operation_id][key]
            evidence = "; ".join(
                next(
                    (
                        f"{fid}: {c.reason} ({c.basis})"
                        for c in by_feature[fid].candidates
                        if c.direction_key == key and c.reachable
                    ),
                    fid,
                )
                for fid in operation.feature_ids
            )
            placed.append(
                PlacedOperation(
                    operation_id=operation.operation_id,
                    operation_name=operation.operation_name,
                    feature_ids=list(operation.feature_ids),
                    access=kind,
                    evidence=evidence,
                )
            )
        first = members[0].operation_id
        setups.append(
            ProposedSetup(
                position=len(setups) + 1,
                direction=vectors[key],
                direction_key=key,
                direction_label=direction_label(key),
                operation_ids=[o.operation_id for o in members],
                feature_ids=[fid for o in members for fid in operations[o.operation_id].feature_ids],
                operations=placed,
                marker_origin=origins.get((first, key)),
            )
        )

    if not considered_count:
        reason = "No operation in this plan is milled or drilled from a recognized feature, so there is nothing to group."
        confidence = Confidence.LOW
    elif weak:
        confidence = Confidence.LOW
        reason = (
            f"Low: {sum(1 for u in unplaced if u.status in (UNREACHABLE, NO_APPROACH, FEATURE_NOT_FOUND))} "
            "operation(s) could not be placed — an unreachable feature, or one with no approach "
            "rule — so the grouping is incomplete."
        )
    else:
        confidence = Confidence.MEDIUM
        reason = (
            "Medium at most: every grouped feature was reached by straight rays along its setup's "
            "direction, but a ray is not a tool — holder, reach, undercuts and the fixture are not "
            "checked."
        )

    return SetupProposal(
        component_id=component_id,
        confidence=confidence,
        confidence_reason=reason,
        considered_operation_count=considered_count,
        setups=setups,
        unplaced=unplaced,
        features=approaches,
    )


def _unplaced(operation: PlannedOperation, status: str, reason: str) -> UnplacedOperation:
    return UnplacedOperation(
        operation_id=operation.operation_id,
        operation_name=operation.operation_name,
        feature_ids=list(operation.feature_ids),
        status=status,
        reason=reason,
    )


# ---------------------------------------------------------------- applying it


def _setup_id_prefix(plan: ManufacturingPlan) -> str:
    """The namespace `build_plan(id_prefix=...)` gave this plan's setups, if any."""
    for setup in plan.ordered_setups:
        match = _SETUP_NUMBER.search(setup.setup_id)
        if match:
            return setup.setup_id[: match.start()]
    return ""


def _next_setup_id(plan: ManufacturingPlan, prefix: str) -> str:
    used = {s.setup_id for s in plan.setups}
    index = len(used) + 1
    while f"{prefix}SETUP-{index:03d}" in used:
        index += 1
    return f"{prefix}SETUP-{index:03d}"


def apply_setup_proposal(plan: ManufacturingPlan, proposal: SetupProposal) -> AppliedProposal:
    """Create one setup per proposed direction and move its operations in.

    Uses the plan's own regrouping: `with_added_setup`, then
    `with_operation_in_setup` per operation, which writes `user_setup_id` and
    never touches `system_setup_id`. A setup the moves leave with no operation
    at all is removed, as the Remove button would; one still holding anything —
    an unplaced line, or a removed one kept as a record — stays.
    """
    if not proposal.available:
        raise ValueError(proposal.unavailable_reason or "There is no proposal to apply.")
    if not proposal.setups:
        raise ValueError("The proposal places no operation in any setup, so there is nothing to apply.")
    known = {o.operation_id for o in plan.operations}
    for setup in proposal.setups:
        for operation_id in setup.operation_ids:
            if operation_id not in known:
                raise ValueError(f"The proposal names operation '{operation_id}', which is not in this plan.")

    occupied_before = {
        s.setup_id for s in plan.setups if plan.operations_in(s.setup_id, active_only=False)
    }
    prefix = _setup_id_prefix(plan) if plan.setups else ""
    updated = plan
    created: list[str] = []
    moved: list[str] = []
    for proposed in proposal.setups:
        members = [plan.get(operation_id) for operation_id in proposed.operation_ids]
        setup_id = _next_setup_id(updated, prefix)
        setup = ManufacturingSetup(
            setup_id=setup_id,
            sequence_position=len(updated.setups) + 1,
            name=f"Approach {proposed.direction_label}",
            description=(
                f"Proposed from tool approach directions ({proposal.machine}) and applied by the "
                "engineer."
            ),
            process_context=_dominant_process(members),
            origin=SuggestionOrigin.SYSTEM,
            system_operation_ids=list(proposed.operation_ids),
            system_grouping=APPROACH_GROUPING,
            system_grouping_reason=(
                f"Every operation here machines a feature that straight rays reach along "
                f"{proposed.direction_label}, and a 3-axis mill holds one approach direction per "
                "setup. Proposed from the B-Rep and applied by the engineer."
            ),
            system_grouping_confidence=proposal.confidence,
            system_grouping_assumptions=[
                *(
                    f"{placed.operation_id} ({', '.join(placed.feature_ids)}), "
                    f"{'side milled by the flank' if placed.access == FLANK else 'head-on'}: "
                    f"{placed.evidence}"
                    for placed in proposed.operations
                ),
                *proposal.limitations,
                proposal.algorithm_note,
                SETUP_TIME_DERIVATION,
            ],
            system_setup_time_minutes=_setup_baseline_for(members),
            system_setup_time_source="largest_process_baseline",
            # The engineer applied it: accepted as a grouping, not as a time.
            user_action=UserAction.ACCEPTED,
        )
        updated = updated.with_added_setup(setup)
        created.append(setup_id)
        for operation_id in proposed.operation_ids:
            updated = updated.with_operation_in_setup(operation_id, setup_id)
            moved.append(operation_id)

    # Within each setup, operations keep the order they had in the plan.
    original = {o.operation_id: index for index, o in enumerate(plan.ordered)}
    order = [
        o.operation_id
        for setup in updated.ordered_setups
        for o in sorted(
            updated.operations_in(setup.setup_id, active_only=False),
            key=lambda o: original[o.operation_id],
        )
    ]
    # A line whose setup is unknown keeps its place after the rest rather than vanishing.
    order += [o.operation_id for o in plan.ordered if o.operation_id not in set(order)]
    updated = updated.reordered(order).renumbered()

    removed: list[str] = []
    for setup in list(updated.ordered_setups):
        if setup.setup_id in occupied_before and not updated.operations_in(setup.setup_id, active_only=False):
            updated = updated.without_setup(setup.setup_id)
            removed.append(setup.setup_id)

    placed_ids = set(moved)
    return AppliedProposal(
        plan=updated,
        proposal=proposal,
        created_setup_ids=created,
        removed_setup_ids=removed,
        moved_operation_ids=moved,
        left_in_place=[o.operation_id for o in plan.ordered if o.operation_id not in placed_ids],
    )


__all__ = [
    "APPROACH_GROUPING",
    "AppliedProposal",
    "MACHINE",
    "ProposedSetup",
    "SetupProposal",
    "UnplacedOperation",
    "apply_setup_proposal",
    "features_to_analyse",
    "propose_setups",
    "unavailable",
]
