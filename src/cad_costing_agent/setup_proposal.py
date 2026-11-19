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
  process with no tool approach (welding, bending, anything but milling,
  drilling and turning) is listed with its reason and left in whatever setup it
  is already in.
- **It never crosses components, or bodies.** A plan is one part's or one
  component's; this module sees one plan and one shape at a time. In a part of
  several bodies each body's features were checked against that body alone
  (`features.approach`), and two bodies never share a proposed setup: setup
  time is charged once per setup, and nothing says the bodies are clamped
  together.
- **A lathe and a mill never share a setup.** Turning operations are grouped
  into lathe chuckings by the end of the part that faces the tool — the same
  evidence, read for a spindle instead of a milling head — and milling and
  drilling into mill setups.

How a grouping is chosen
------------------------
Each operation is coverable from the directions along which *every* feature it
machines is reachable. Directions are then chosen greedily: the one covering
the most still-uncovered operations; ties go to the one where more of those
operations are approached head-on rather than by a tool's flank, then to a
fixed order (+Z, −Z, +X, −X, +Y, −Y, then any other direction by name). An
operation lands in the earliest chosen direction that reaches it head-on, else
the earliest that reaches it at all.

A greedy cover is not always the smallest: on a real 31-operation bracket it
chose five directions where four reach everything. So when there are at most
`EXACT_COVER_LIMIT` candidate directions, every smaller combination is then
tried, and if one covers every operation the smallest is used (ties to the one
reaching more operations head-on, then to the fixed order), its directions
ordered the greedy way. The proposal's `cover_method` says which: `fewest`
when the count is proven the smallest possible *for these candidate
directions*, `greedy` when there were too many to try and it is **not
guaranteed minimal**.

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
from collections.abc import Callable
from itertools import combinations

from pydantic import BaseModel, Field, field_validator

from cad_costing_agent.features.approach import (
    BODY_NOTE,
    FLANK,
    LATHE,
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
    OperationTimeEstimate,
    PlannedOperation,
    index_operation,
    _dominant_process,
    _setup_baseline_for,
)
from cad_costing_agent.operation_suggestions import SuggestionOrigin, UserAction

APPROACH_GROUPING = "approach_direction"
"""`ManufacturingSetup.system_grouping` for a setup created by applying this proposal."""

TOOL_APPROACH_PROCESSES = ("milling", "drilling")
"""Processes whose tool comes at the part along one direction per setup."""

LATHE_PROCESSES = ("turning",)
"""Processes cut with the part turning in a chuck: one chucking per end facing the tool."""

MACHINE = "3_axis_mill"
LATHE_MACHINE = "lathe"
INDEXER = "4_axis_indexer"
"""A mill with a rotary table that indexes (turns and clamps) between cuts, never cutting while turning."""
MILLING_MACHINES = (MACHINE, INDEXER)
ROTARY_AXES = {"Z": (0.0, 0.0, 1.0), "X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0)}
"""Rotary axes an indexer is proposed about, in the order ties go to."""
INDEXER_NOTE = (
    "Proposed for a 4-axis indexer: a rotary table about the {axis} axis turns the part between "
    "cuts, so every approach direction square to that axis is one setup, each operation cut at "
    "its own index position. A direction along the axis is still its own setup. Turning between "
    "positions happens on every part, so applying adds an index line timed per part (moves "
    "between positions x the time profile's index time, which the seeded profile does not "
    "state); it is never put in the setup, which is charged once per batch. A return to the "
    "loading position is not counted, near-identical directions are not merged into one "
    "position, cutting while the table turns (continuous 4-axis) is not modelled, and the "
    "rotary table's reach, clearance and the part's fixturing on it are not checked. Turning is "
    "proposed for a 2-axis lathe as on the 3-axis proposal."
)
MACHINE_NOTE = (
    "Proposed for a 3-axis mill: the tool axis is fixed, so every distinct approach direction is "
    "its own setup. A 4-axis machine with a rotary table could reach every direction "
    "perpendicular to its rotary axis in one setup, and a 5-axis machine most directions in one "
    "or two. Neither is modelled — this proposal does not reduce the count for them. Turning is "
    "proposed for a 2-axis lathe: one chucking per end of the part that has to face the tool, so "
    "facing both ends is two chuckings, the part turned round between them. A sub-spindle or "
    "bar feeder that does both ends in one cycle is not modelled, and a hole on the spindle axis "
    "that a lathe would drill from the tailstock in the same chucking is still proposed on the "
    "mill — move it by hand."
)
EXACT_COVER_LIMIT = 16
"""Up to this many candidate directions, the fewest setups are found by trying every combination."""

ALGORITHM_NOTE = (
    "Directions are chosen greedily — the one reaching the most remaining operations first, ties "
    "to the one approaching more of them head-on, then to the fixed order +Z, −Z, +X, −X, +Y, −Y. "
    f"With at most {EXACT_COVER_LIMIT} candidate directions every smaller combination is also "
    "tried, so the count is the fewest these directions allow (cover method 'fewest'); with more, "
    "the greedy choice stands and is not guaranteed to be the fewest setups possible (cover "
    "method 'greedy'). Either way only the candidate directions read from the B-Rep are "
    "considered, never an arbitrary angle."
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
    access: str = Field(
        description="'primary' (head-on), 'flank' (cut with the side of the tool) or 'lathe' (turned)."
    )
    evidence: str


class ProposedSetup(BaseModel):
    position: int = Field(ge=1)
    machine: str = Field(
        default=MACHINE,
        description="'3_axis_mill', '4_axis_indexer' for every direction square to its rotary axis, or 'lathe'.",
    )
    index_positions: list[str] = Field(
        default_factory=list,
        description="For an indexed setup: the approach directions its operations are cut from, one index position each.",
    )
    index_moves_per_part: int | None = Field(
        default=None,
        description=(
            "For an indexed setup of two or more positions: the moves between them, per part. "
            "Applying adds an index line timed for them, charged per part."
        ),
    )
    index_time: OperationTimeEstimate | None = Field(
        default=None,
        description=(
            "The index line's time from the plan's time profile: estimated, or needs input when "
            "the profile states no index time. Uncommitted until accepted."
        ),
    )
    solid_index: int | None = Field(
        default=None, description="The body this setup machines, for a part of several bodies."
    )
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
    rotary_axis: str | None = Field(
        default=None, description="For a 4-axis indexer: the axis the table turns about ('X', 'Y' or 'Z')."
    )
    rotary_axis_basis: str | None = Field(
        default=None, description="Why that axis: the engineer's, or which candidate placed the most operations."
    )
    algorithm_note: str = ALGORITHM_NOTE
    cover_method: str = Field(
        default="fewest",
        description=(
            "'fewest' when every combination of the candidate directions was tried, so no fewer "
            "setups reach every placed operation; 'greedy' when there were too many to try."
        ),
    )
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
    added_operation_ids: list[str] = Field(
        default_factory=list,
        description="Lines the apply added: an indexed setup's index line, timed per part and uncommitted.",
    )
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
    if process not in TOOL_APPROACH_PROCESSES and process not in LATHE_PROCESSES:
        return (
            False,
            NOT_A_TOOL_APPROACH,
            f"'{process or 'no process'}' is not cut by a tool approaching along one direction "
            "or by turning, so it is left where it is",
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


def turned_feature_ids(plan: ManufacturingPlan) -> frozenset[str]:
    """The features the plan's considered turning operations cut — the ones `approach_directions` reads for a lathe."""
    return frozenset(
        fid
        for operation in plan.operations
        if (operation.operation_type or "") in LATHE_PROCESSES and _considered(plan, operation)[0]
        for fid in operation.feature_ids
    )


def _key(machine: str, solid: int | None, direction: str) -> str:
    """The proposal's setup key: the direction, kept apart per machine and per body.

    A one-body part milled along +Z keys as '+Z', exactly the direction key.
    """
    return " ".join(
        [
            *(["lathe"] if machine == LATHE_MACHINE else []),
            *(["indexed"] if machine == INDEXER else []),
            *([f"body{solid}"] if solid is not None else []),
            direction,
        ]
    )


def _label(machine: str, solid: int | None, direction: str) -> str:
    label = direction_label(direction)
    if machine == INDEXER:
        label = f"4-axis, indexed about {direction}"
    if machine == LATHE_MACHINE:
        label = f"lathe, {label} end toward the tool"
    if solid is not None:
        label += f" — body {solid + 1}"
    return label


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
    machine: str = MACHINE,
    rotary_axis: str | None = None,
    index_time: Callable[..., OperationTimeEstimate] | None = None,
) -> SetupProposal:
    """Group the plan's operations by the directions their features are reachable from.

    `machine` is the 3-axis mill unless the engineer chose the 4-axis indexer,
    which needs a `rotary_axis` ('X', 'Y' or 'Z') and the approaches computed
    with that axis (`approach_directions(..., rotary_axis=...)`). `index_time`
    (`operation_time.estimate_index_time` bound to the plan's profile, handed in
    by `main.py`) times an indexed setup's moves per part.
    """
    if machine not in MILLING_MACHINES:
        raise ValueError(f"Unknown machine '{machine}'; expected one of {', '.join(MILLING_MACHINES)}.")
    if (machine == INDEXER) != (rotary_axis is not None):
        raise ValueError("A rotary axis is given for the 4-axis indexer, and only for it.")
    if rotary_axis is not None and rotary_axis not in ROTARY_AXES:
        raise ValueError(f"Unknown rotary axis '{rotary_axis}'; expected X, Y or Z.")
    axis_vector = ROTARY_AXES.get(rotary_axis or "")
    by_feature = {approach.feature_id: approach for approach in approaches}
    # setup key -> (machine, body, direction key)
    meta: dict[str, tuple[str, int | None, str]] = {}
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

        lathe = (operation.operation_type or "") in LATHE_PROCESSES
        kinds = (LATHE,) if lathe else (PRIMARY, FLANK)
        shared: dict[str, str] | None = None
        for approach in approaches_here:
            reachable = {}
            for candidate in approach.candidates:
                if not candidate.reachable or candidate.kind not in kinds:
                    continue
                on = LATHE_MACHINE if lathe else MACHINE
                direction = candidate.direction_key
                if (
                    not lathe
                    and axis_vector is not None
                    and abs(sum(a * b for a, b in zip(candidate.direction.as_tuple(), axis_vector))) < 1e-6
                ):
                    on, direction = INDEXER, rotary_axis
                key = _key(on, approach.solid_index, direction)
                if key not in meta:
                    meta[key] = (on, approach.solid_index, direction)
                    if on == INDEXER:
                        vectors[key] = Point3D.of(*axis_vector)
                reachable[key] = PRIMARY if reachable.get(key) == PRIMARY else candidate.kind
                vectors.setdefault(key, candidate.direction)
                origins.setdefault((operation.operation_id, key), candidate.origin)
            if shared is None:
                shared = reachable
            else:
                shared = {
                    key: (kind if shared[key] == kind else FLANK)
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

    def order(key: str) -> tuple:
        machine, solid, direction = meta[key]
        return (-1 if solid is None else solid, machine == LATHE_MACHINE, direction_order(direction))

    def greedy(allowed: set[str] | None) -> list[str]:
        """Deterministic greedy cover, optionally from a given set of directions only."""
        uncovered = set(coverable)
        picked: list[str] = []
        while uncovered:
            keys = sorted(
                {key for op in uncovered for key in coverable[op] if allowed is None or key in allowed},
                key=order,
            )
            best = max(
                keys,
                key=lambda key: (
                    sum(1 for op in uncovered if key in coverable[op]),
                    sum(1 for op in uncovered if coverable[op].get(key) in (PRIMARY, LATHE)),
                    -keys.index(key),
                ),
            )
            picked.append(best)
            uncovered -= {op for op in uncovered if best in coverable[op]}
        return picked

    chosen = greedy(None)
    every_key = sorted({key for options in coverable.values() for key in options}, key=order)
    cover_method = "fewest" if len(every_key) <= EXACT_COVER_LIMIT else "greedy"
    if cover_method == "fewest" and len(chosen) > 1:
        bit = {op: 1 << index for index, op in enumerate(sorted(coverable))}
        everything = sum(bit.values())
        reach = {key: sum(bit[op] for op in coverable if key in coverable[op]) for key in every_key}
        head_on = {
            key: sum(bit[op] for op in coverable if coverable[op].get(key) in (PRIMARY, LATHE))
            for key in every_key
        }
        for size in range(1, len(chosen)):
            best_combo: tuple[int, tuple[str, ...]] | None = None
            for combo in combinations(every_key, size):
                covered = 0
                for key in combo:
                    covered |= reach[key]
                if covered != everything:
                    continue
                facing = 0
                for key in combo:
                    facing |= head_on[key]
                score = bin(facing).count("1")
                if best_combo is None or score > best_combo[0]:
                    best_combo = (score, combo)
            if best_combo is not None:
                chosen = greedy(set(best_combo[1]))
                break

    assignment: dict[str, str] = {}
    for operation_id, options in coverable.items():
        head_on = [key for key in chosen if options.get(key) in (PRIMARY, LATHE)]
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
                        if (
                            c.direction_key == meta[key][2]
                            or (
                                meta[key][0] == INDEXER
                                and abs(sum(a * b for a, b in zip(c.direction.as_tuple(), axis_vector)))
                                < 1e-6
                            )
                        )
                        and c.reachable
                        and (c.kind == LATHE) == (meta[key][0] == LATHE_MACHINE)
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
        on, solid, direction = meta[key]
        positions: list[str] = []
        if on == INDEXER:
            for operation in members:
                for fid in operation.feature_ids:
                    used = next(
                        c.direction_key
                        for c in by_feature[fid].candidates
                        if c.reachable
                        and c.kind != LATHE
                        and abs(sum(a * b for a, b in zip(c.direction.as_tuple(), axis_vector))) < 1e-6
                    )
                    if used not in positions:
                        positions.append(used)
        moves = len(positions) - 1 if on == INDEXER and len(positions) > 1 else None
        setups.append(
            ProposedSetup(
                position=len(setups) + 1,
                machine=on,
                index_positions=positions,
                index_moves_per_part=moves,
                solid_index=solid,
                direction=vectors[key],
                direction_key=direction,
                direction_label=_label(on, solid, direction),
                operation_ids=[o.operation_id for o in members],
                feature_ids=[fid for o in members for fid in operations[o.operation_id].feature_ids],
                operations=placed,
                marker_origin=origins.get((first, key)),
            )
        )

    if not considered_count:
        reason = (
            "No operation in this plan is milled, drilled or turned from a recognized feature, so "
            "there is nothing to group."
        )
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

    if index_time is not None:
        # Timed at the proposal's own confidence, never above it.
        setups = [
            s.model_copy(
                update={"index_time": index_time(s.index_moves_per_part, suggestion_confidence=confidence)}
            )
            if s.index_moves_per_part is not None
            else s
            for s in setups
        ]

    limitations = list(LIMITATIONS)
    if any(approach.solid_index is not None for approach in approaches):
        limitations.append(BODY_NOTE)
    return SetupProposal(
        component_id=component_id,
        machine=machine,
        machine_note=INDEXER_NOTE.format(axis=rotary_axis) if machine == INDEXER else MACHINE_NOTE,
        rotary_axis=rotary_axis,
        limitations=limitations,
        confidence=confidence,
        confidence_reason=reason,
        considered_operation_count=considered_count,
        cover_method=cover_method,
        setups=setups,
        unplaced=unplaced,
        features=approaches,
    )


def choose_rotary_axis(proposals: dict[str, SetupProposal]) -> SetupProposal:
    """Of indexer proposals about each axis, the one placing the most operations.

    Ties go to fewer setups, then to the order Z, X, Y. The chosen proposal
    says which axes were tried and what each placed, so the choice is
    checkable rather than asserted.
    """
    order = list(ROTARY_AXES)
    def placed(p: SetupProposal) -> int:
        return sum(len(s.operation_ids) for s in p.setups)

    best = min(proposals, key=lambda a: (-placed(proposals[a]), len(proposals[a].setups), order.index(a)))
    tried = "; ".join(
        f"{a}: {placed(proposals[a])} placed in {len(proposals[a].setups)} setup(s)"
        for a in order
        if a in proposals
    )
    return proposals[best].model_copy(
        update={
            "rotary_axis_basis": (
                f"Chosen by the system: the axis whose indexer proposal placed the most operations "
                f"(ties to fewer setups, then Z, X, Y). Tried — {tried}."
            )
        }
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
    added: list[str] = []
    for proposed in proposal.setups:
        members = [plan.get(operation_id) for operation_id in proposed.operation_ids]
        setup_id = _next_setup_id(updated, prefix)
        setup = ManufacturingSetup(
            setup_id=setup_id,
            sequence_position=len(updated.setups) + 1,
            name=(
                f"Lathe chucking, {proposed.direction_label.removeprefix('lathe, ')}"
                if proposed.machine == LATHE_MACHINE
                else f"Indexer {proposed.direction_label.removeprefix('4-axis, ')}"
                if proposed.machine == INDEXER
                else f"Approach {proposed.direction_label}"
            ),
            description=(
                f"Proposed from tool approach directions ({proposed.machine}) and applied by the "
                "engineer."
            ),
            process_context=_dominant_process(members),
            origin=SuggestionOrigin.SYSTEM,
            system_operation_ids=list(proposed.operation_ids),
            system_grouping=APPROACH_GROUPING,
            system_grouping_reason=(
                (
                    "Every operation here turns a feature reached from the same end of the part, "
                    f"{proposed.direction_label.removeprefix('lathe, ')}, and a lathe chucking "
                    "presents one end to the tool. Proposed from the B-Rep and applied by the "
                    "engineer."
                )
                if proposed.machine == LATHE_MACHINE
                else (
                    "Every operation here machines a feature that straight rays reach along a "
                    f"direction square to the rotary axis ({len(proposed.index_positions)} index "
                    "position(s)), and a 4-axis indexer turns the part between them in one "
                    "clamping. Proposed from the B-Rep and applied by the engineer."
                )
                if proposed.machine == INDEXER
                else (
                    f"Every operation here machines a feature that straight rays reach along "
                    f"{proposed.direction_label}, and a 3-axis mill holds one approach direction "
                    "per setup. Proposed from the B-Rep and applied by the engineer."
                )
            ),
            system_grouping_confidence=proposal.confidence,
            system_grouping_assumptions=[
                *(
                    f"{placed.operation_id} ({', '.join(placed.feature_ids)}), "
                    f"{ {FLANK: 'cut by the flank', LATHE: 'turned'}.get(placed.access, 'head-on')}: "
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
        if proposed.index_time is not None and proposed.index_moves_per_part is not None:
            line = index_operation(
                operation_id=f"{setup_id}-INDEX",
                setup_id=setup_id,
                index_moves=proposed.index_moves_per_part,
                positions=len(proposed.index_positions),
                estimate=proposed.index_time,
                confidence=proposal.confidence,
            )
            updated = updated.with_added(line)
            added.append(line.operation_id)

    # Within each setup, operations keep the order they had in the plan.
    original = {o.operation_id: index for index, o in enumerate(plan.ordered)}
    order = [
        o.operation_id
        for setup in updated.ordered_setups
        for o in sorted(
            updated.operations_in(setup.setup_id, active_only=False),
            # An added index line has no earlier place: it goes last in its setup.
            key=lambda o: original.get(o.operation_id, len(original)),
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
        added_operation_ids=added,
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
    "INDEXER",
    "ROTARY_AXES",
    "choose_rotary_axis",
    "propose_setups",
    "turned_feature_ids",
    "unavailable",
]
