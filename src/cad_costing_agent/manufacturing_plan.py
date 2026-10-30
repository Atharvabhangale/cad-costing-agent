"""The editable manufacturing plan: what the engineer actually decided.

    suggestions + time estimates → **plan** → engineer edits → sequence → costing

A `ManufacturingPlan` is an ordered list of `PlannedOperation`, and each one
carries two parallel sets of values that never mix:

    system_suggested_*   what the system proposed. Frozen at plan creation.
    user_*               what the engineer chose instead.

**The system's proposal is never overwritten.** That is the single invariant
this module exists to hold. An edit writes to the `user_*` side and leaves the
prediction intact, so the pair *(predicted, chosen)* survives into
`operation_feedback` — and that pair is the entire point, because a later
learning step can only learn from corrections it can still see.

What is costed
--------------
`final_setup_time` / `final_cycle_time` resolve in one direction only:

1. the engineer typed a value          → use it
2. the engineer accepted the estimate  → use the system's value
3. otherwise                           → **None**

There is no fourth branch. A generated estimate is *not* committed merely by
existing: `system_time_accepted` has to be set by a person. And an operation
with no resolved cycle time is not costed at all — never zero, never a process
average, never the previous value. `to_operation_requirements` simply leaves it
out and `uncosted_operations` names it, so the omission is visible rather than
silent.

Changing the process invalidates the estimate
---------------------------------------------
A system time estimated for milling is not a turning time. When the engineer
changes an operation's process, `system_time_applies` goes false and the
operation needs an entered time, even though the original estimate is still
recorded. Costing a milling time as a turning operation would be exactly the
kind of quiet substitution the rest of this module prevents.

Sequence
--------
`sequence_position` is an explicit, persisted property of the plan and nothing
more. **Order does not affect any number**: there is no sequence-dependent
machining time, no setup consolidation, no routing optimisation. Operations are
costed identically whatever order they are in, and reordering is recorded
because a manufacturing plan without an order is not a plan — not because the
arithmetic reads it.

No precedence rules are enforced either. The system does not claim that drilling
must precede milling; engineers sequence the way their shop actually works, and
a universal rule invented here would be wrong somewhere.

This module imports no costing arithmetic. It produces `OperationRequirement`
objects, which the existing unmodified costing consumes exactly as it does for a
hand-entered operation — rates still come from the rate set.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cad_costing_agent.features.model import Confidence
from cad_costing_agent.operation_suggestions import (
    OperationSuggestion,
    SuggestionOrigin,
    UserAction,
)
from cad_costing_agent.operation_time import (
    DrillingOption,
    OperationTimeEstimate,
    TimeAssumptions,
    TimeStatus,
    WeldSpec,
    estimate_for_suggestion,
    estimate_operation_time,
)
from cad_costing_agent.rate_sets import OperationRequirement


class TimeSource(str, Enum):
    """Where an operation's committed time came from, if anywhere."""

    SYSTEM = "system"
    USER = "user"
    NEEDS_INPUT = "needs_input"


DEFAULT_SETUP_ID = "SETUP-001"

SYSTEM_GROUPING_REASON = (
    "Every operation was placed in one setup, because nothing in the CAD proves a second "
    "fixturing is needed. This is the conservative default: it charges setup once rather than "
    "once per operation, which is the error it exists to avoid."
)

SYSTEM_GROUPING_ASSUMPTIONS = (
    "the part can be held in a single fixture in a single orientation for these operations",
    "no tool cannot reach a feature from this orientation",
    "geometry alone cannot prove either of the above — an axis pointing a different way is a "
    "hint, not evidence, so the grouping is offered rather than asserted",
)

SETUP_TIME_DERIVATION = (
    "The setup's estimated time is the **largest** per-process setup baseline among its "
    "operations, not their sum: one fixturing event takes as long as its most demanding process "
    "needs, and summing them is exactly the mistake this model exists to prevent."
)

NOT_COSTED_WITHOUT_SETUP_TIME = (
    "This setup has no committed setup time, so no setup cost is charged for it. Accept the "
    "estimate or enter one. Nothing is assumed."
)

NOT_COSTED_WITHOUT_TIME = (
    "This operation has no cycle time, so it is not costed. Enter one, or accept the system's "
    "estimate. Nothing is assumed."
)


class SetupTimeSummary(BaseModel):
    """One setup's contribution of minutes, setup and cycle kept apart."""

    setup_id: str
    name: str
    sequence_position: int
    setup_time_minutes: float | None = Field(
        default=None, description="Committed setup time, or None when nobody has set one."
    )
    charged_setup_time_minutes: float = Field(
        default=0.0,
        description="What actually reaches costing: the committed time, or zero. Charged once.",
    )
    setup_time_source: TimeSource = TimeSource.NEEDS_INPUT
    operation_count: int = 0
    costable_operation_count: int = 0
    cycle_time_minutes: float = Field(
        default=0.0, description="Sum of cycle × quantity over this setup's costable operations."
    )


class PlanTimeSummary(BaseModel):
    """Minutes, split the way a manufacturing plan actually spends them.

    Exists so nothing has to infer the split from a cost. "Setup time: 10 min
    across 1 setup; cycle time: 7.2 min; total 17.2 min" is a sentence this
    model can produce directly.
    """

    quantity: int = 1
    setup_count: int = 0
    charged_setup_count: int = Field(
        default=0, description="Setups that actually contributed a setup cost."
    )
    total_setup_time_minutes: float = 0.0
    total_cycle_time_minutes: float = 0.0
    total_time_minutes: float = 0.0
    setups: list[SetupTimeSummary] = Field(default_factory=list)
    uncosted_operation_ids: list[str] = Field(default_factory=list)
    setups_needing_time: list[str] = Field(default_factory=list)
    out_of_route_operation_ids: list[str] = Field(
        default_factory=list,
        description="Kept in the plan but not costed, because the chosen route does not include their process.",
    )
    as_formed_operation_ids: list[str] = Field(
        default_factory=list,
        description="Kept but not costed: left as cast or forged on a near-net-shape route.",
    )


class ManufacturingSetup(BaseModel):
    """One fixturing context: the part clamped one way, for one or more operations.

    **A setup is not an operation.** Setup time is charged once for the setup;
    cycle time is charged for every operation in it. Charging a setup per
    operation — six drilled holes carrying six setups — is the specific costing
    error this model exists to remove, and it is removed by representing the
    setup explicitly rather than by dividing anything.

    What the system can and cannot know
    -----------------------------------
    Geometry does not prove fixturing. A hole whose axis points elsewhere *might*
    need a second setup, or the part might sit on a tombstone and never move. So
    the system groups conservatively, marks the grouping `system_assumed` at low
    confidence, states the assumption, and leaves the engineer with final
    authority. It never claims two operations share a setup because they share a
    process type — that is not evidence of anything.

    As everywhere else in the plan, the system's decision is never overwritten:
    an engineer's regrouping is recorded beside it, so the pair
    *(grouped, regrouped)* survives for a later learning step.
    """

    model_config = ConfigDict(frozen=True)

    setup_id: str = Field(description="Stable within one plan, e.g. 'SETUP-001'.")
    sequence_position: int = Field(ge=1, description="1-based order of the setups themselves.")
    name: str = Field(default="Setup", description="What the engineer calls this fixturing.")
    description: str = ""
    process_context: str | None = Field(
        default=None,
        description="The dominant process of its operations, if they agree. Descriptive only.",
    )

    # --- what the system proposed. Written once, never overwritten. ---
    origin: SuggestionOrigin = SuggestionOrigin.SYSTEM
    system_operation_ids: list[str] = Field(
        default_factory=list,
        description="Which operations the system put here, kept whatever the engineer does next.",
    )
    system_grouping: str = Field(
        default="system_assumed",
        description="'system_assumed' for an inferred grouping, 'user_defined' for one added by hand.",
    )
    system_grouping_reason: str = ""
    system_grouping_confidence: Confidence = Field(
        default=Confidence.LOW,
        description=(
            "Low by default and deliberately so: inferred fixture compatibility is not an "
            "engineering fact, and labelling it one would be the wrong kind of confident."
        ),
    )
    system_grouping_assumptions: list[str] = Field(default_factory=list)
    system_setup_time_minutes: float | None = None
    system_setup_time_source: str | None = Field(
        default=None, description="How the estimate was derived, e.g. 'largest_process_baseline'."
    )

    # --- the engineer's decisions ---
    user_action: UserAction = UserAction.PENDING
    user_setup_time_minutes: float | None = Field(default=None, ge=0)
    system_setup_time_accepted: bool = Field(
        default=False,
        description="An estimated setup time is a proposal until a person accepts or replaces it.",
    )

    # ---------------------------------------------------------------- derived

    @property
    def setup_time_minutes(self) -> float | None:
        """The committed setup time, or None. Never a substituted default."""
        if self.user_setup_time_minutes is not None:
            return self.user_setup_time_minutes
        if self.system_setup_time_accepted and self.system_setup_time_minutes is not None:
            return self.system_setup_time_minutes
        return None

    @property
    def setup_time_source(self) -> TimeSource:
        if self.user_setup_time_minutes is not None:
            return TimeSource.USER
        if self.system_setup_time_accepted and self.system_setup_time_minutes is not None:
            return TimeSource.SYSTEM
        return TimeSource.NEEDS_INPUT

    @property
    def charged_setup_time(self) -> float:
        """What actually reaches costing: the committed time, or zero.

        Zero rather than a guess. A setup nobody has timed costs nothing and is
        listed as needing input, which is visible; inventing a number would not
        be.
        """
        committed = self.setup_time_minutes
        return committed if committed is not None else 0.0

    # ------------------------------------------------------------ transitions

    def renamed(self, name: str, description: str | None = None) -> "ManufacturingSetup":
        if not (name or "").strip():
            raise ValueError("A setup needs a name.")
        updates: dict = {"name": name.strip(), "user_action": UserAction.EDITED}
        if description is not None:
            updates["description"] = description
        return self.model_copy(update=updates)

    def accepted(self, *, use_estimated_time: bool = False) -> "ManufacturingSetup":
        return self.model_copy(
            update={
                "user_action": UserAction.ACCEPTED,
                "system_setup_time_accepted": (
                    self.system_setup_time_accepted
                    or (use_estimated_time and self.system_setup_time_minutes is not None)
                ),
            }
        )

    def with_setup_time(self, minutes: float) -> "ManufacturingSetup":
        """Record an engineer-entered setup time beside the system's, never over it."""
        if minutes is None or minutes < 0:
            raise ValueError("Setup time cannot be negative.")
        changed = minutes != self.system_setup_time_minutes
        return self.model_copy(
            update={
                "user_setup_time_minutes": float(minutes),
                "user_action": UserAction.EDITED if changed else UserAction.ACCEPTED,
            }
        )


def default_setup(
    operation_ids: list[str],
    *,
    setup_id: str = DEFAULT_SETUP_ID,
    sequence_position: int = 1,
    name: str = "Setup 1",
) -> ManufacturingSetup:
    """The conservative default: one setup holding everything.

    Chosen because the opposite default — a setup per operation — is the costing
    error this step exists to fix, and because nothing in the CAD proves a second
    fixturing is needed. It is marked `system_assumed` at low confidence with its
    assumptions spelled out, and the engineer can split it at will.
    """
    return ManufacturingSetup(
        setup_id=setup_id,
        sequence_position=sequence_position,
        name=name,
        origin=SuggestionOrigin.SYSTEM,
        system_operation_ids=list(operation_ids),
        system_grouping="system_assumed",
        system_grouping_reason=SYSTEM_GROUPING_REASON,
        system_grouping_confidence=Confidence.LOW,
        system_grouping_assumptions=list(SYSTEM_GROUPING_ASSUMPTIONS),
    )


class PlannedOperation(BaseModel):
    """One line of the manufacturing plan.

    Frozen: every edit produces a new object, so an operation cannot be mutated
    out from under the record of what was originally proposed.
    """

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(description="Stable within one plan, e.g. 'OP-001'.")
    sequence_position: int = Field(
        ge=1, description="1-based position **within its setup**. The setups have their own order."
    )
    # Setup membership, kept as an explicit relationship rather than inferred
    # from a name or a process type. The system's placement is never overwritten:
    # moving an operation writes `user_setup_id` beside it, so a later learning
    # step can see "the system grouped H1–H4 together and the engineer moved H4".
    system_setup_id: str | None = None
    user_setup_id: str | None = None
    feature_ids: list[str] = Field(
        default_factory=list,
        description="The recognized features behind this operation. Empty for a manual line.",
    )
    feature_label: str = ""
    # Carried so the operation can be re-estimated on its own. A drilling time
    # needs the subtype to know whether the drill breaks through, and a milling
    # time needs the type to tell a blend (followed along its length) from a
    # face (swept over its area) — dimensions alone do not say.
    feature_type: str | None = None
    feature_subtype: str | None = None
    operation_name: str

    origin: SuggestionOrigin = SuggestionOrigin.SYSTEM
    user_action: UserAction = UserAction.PENDING

    # --- the system's proposal. Written once, at plan creation, never again. ---
    system_suggested_operation: str | None = None
    system_suggestion_confidence: Confidence | None = None
    system_suggestion_rule: str | None = None
    # Carried for Step 29's analytics, which needs to answer "which rule fired,
    # on what kind of geometry, from a feature recognized how confidently?"
    # without re-running recognition. `source_type` is also derivable from
    # whether `source_faces` or `source_facets` is populated — and the analytics
    # layer still derives it that way for plans stored before this field
    # existed — but a fact worth reporting is worth recording rather than
    # inferring every time.
    system_source_type: str | None = Field(
        default=None, description="'brep' or 'faceted', propagated from the suggestion."
    )
    system_feature_confidence: Confidence | None = Field(
        default=None, description="How completely the geometry supported the feature itself."
    )
    system_suggested_setup_time: float | None = None
    system_suggested_cycle_time: float | None = None
    system_time_method: str | None = None
    system_time_confidence: Confidence | None = None
    # Which published assumptions produced the prediction. Recorded per
    # operation so a historical line can answer "why 2.37 minutes?" without
    # anything else, and so a later learning step knows which parameter set a
    # correction was a correction *of*.
    system_time_profile_id: str | None = None
    system_time_profile_version: str | None = None
    system_time_profile_name: str | None = None
    system_time_parameters: dict[str, float] = Field(
        default_factory=dict,
        description="The parameter values that actually entered this operation's calculation.",
    )
    system_time_operation_type: str | None = Field(
        default=None,
        description=(
            "The process a system time was estimated for on a line no rule proposed — a weld the "
            "engineer added and described. None: the time is for `system_suggested_operation`."
        ),
    )
    system_time_status: TimeStatus = TimeStatus.NEEDS_USER_INPUT
    system_time_reason: str = ""
    system_time_evidence: list[str] = Field(default_factory=list)
    system_time_assumptions: list[str] = Field(default_factory=list)
    system_time_missing: list[str] = Field(default_factory=list)

    # --- the engineer's decisions ---
    user_operation_type: str | None = None
    user_setup_time: float | None = Field(default=None, ge=0)
    user_cycle_time: float | None = Field(default=None, gt=0)
    system_time_accepted: bool = Field(
        default=False,
        description=(
            "Set only by an explicit 'use the estimated time'. Generating an estimate does not "
            "commit it."
        ),
    )

    # --- viewer linkage, carried through from the suggestion ---
    source_faces: list[int] = Field(default_factory=list)
    source_facets: list[int] = Field(default_factory=list)
    dimensions: dict[str, float] = Field(default_factory=dict)

    # --- drilling: how a hole is opened up, smallest drill first ---
    system_drilling_steps: list[float] = Field(
        default_factory=list,
        description="The drill sequence the drilling rule suggested. Never overwritten.",
    )
    system_drilling_options: list[DrillingOption] = Field(
        default_factory=list,
        description="Every sequence the system offered for this hole, each with its own time.",
    )
    user_drilling_steps: list[float] | None = Field(
        default=None,
        description="The sequence the engineer chose instead, smallest first. None: the suggestion.",
    )
    user_machined: bool | None = Field(
        default=None,
        description=(
            "On a cast or forged route: True — machine this even though its feature type is "
            "left as formed by default; False — leave it as formed. None follows the default."
        ),
    )

    # --- welding: the weld as the engineer described it, never read from CAD ---
    weld: WeldSpec | None = Field(
        default=None,
        description="Welding lines only. A system weld time is estimated from these figures.",
    )

    # ---------------------------------------------------------------- derived

    @property
    def operation_type(self) -> str | None:
        """The process that would actually be costed."""
        return self.user_operation_type or self.system_suggested_operation

    @property
    def setup_id(self) -> str | None:
        """The setup this operation is actually in."""
        return self.user_setup_id or self.system_setup_id

    @property
    def setup_was_changed(self) -> bool:
        """True once the engineer has moved it out of where the system put it."""
        return (
            self.user_setup_id is not None
            and self.system_setup_id is not None
            and self.user_setup_id != self.system_setup_id
        )

    def moved_to_setup(self, setup_id: str) -> "PlannedOperation":
        """Record a regrouping without touching where the system put it."""
        if not (setup_id or "").strip():
            raise ValueError("An operation must belong to a setup.")
        target = setup_id.strip()
        if target == self.system_setup_id:
            # Back to where it started: not a correction any more.
            return self.model_copy(update={"user_setup_id": None})
        return self.model_copy(
            update={
                "user_setup_id": target,
                "user_action": (
                    self.user_action
                    if self.user_action in (UserAction.ADDED, UserAction.REJECTED)
                    else UserAction.EDITED
                ),
            }
        )

    @property
    def estimated_for(self) -> str | None:
        """The process the system's time is for: the suggested one, or — on a line
        no rule proposed — the one recorded when its time was estimated."""
        return self.system_suggested_operation or self.system_time_operation_type

    @property
    def system_time_applies(self) -> bool:
        """Whether the system's estimate is still for the process now selected.

        False once the engineer changes the process: an estimate made for
        milling says nothing about turning.
        """
        if self.system_time_status is not TimeStatus.ESTIMATED:
            return False
        if self.estimated_for is None:
            return False
        if self.operation_type != self.estimated_for:
            return False
        # A drilling sequence the system never timed has no system time.
        return self.system_cycle_for_choice is not None

    @property
    def drilling_steps(self) -> list[float]:
        """The drills actually planned for this hole: the engineer's choice, else the system's."""
        if self.user_drilling_steps is not None:
            return list(self.user_drilling_steps)
        return list(self.system_drilling_steps)

    @property
    def system_cycle_for_choice(self) -> float | None:
        """The system's cycle time for the drilling sequence now chosen.

        The suggested sequence's time is `system_suggested_cycle_time`, never
        overwritten. Another sequence the system offered carries its own
        system-computed time in `system_drilling_options`; a sequence it did not
        offer has none, and needs an entered time like any other change.
        """
        if self.user_drilling_steps is None or self.user_drilling_steps == self.system_drilling_steps:
            return self.system_suggested_cycle_time
        for option in self.system_drilling_options:
            if option.steps == self.user_drilling_steps:
                return option.cycle_time_minutes
        return None

    @property
    def time_source(self) -> TimeSource:
        if self.user_cycle_time is not None:
            return TimeSource.USER
        if self.system_time_accepted and self.system_time_applies:
            return TimeSource.SYSTEM
        return TimeSource.NEEDS_INPUT

    @property
    def final_cycle_time(self) -> float | None:
        if self.user_cycle_time is not None:
            return self.user_cycle_time
        if self.system_time_accepted and self.system_time_applies:
            return self.system_cycle_for_choice
        return None

    @property
    def final_setup_time(self) -> float | None:
        """Setup follows the same resolution, but zero is a legitimate answer.

        A typed 0 is a decision; a missing setup alongside a committed cycle
        time is not, so it resolves to 0 only when the cycle time itself is
        committed.
        """
        if self.user_setup_time is not None:
            return self.user_setup_time
        if self.system_time_accepted and self.system_time_applies:
            return self.system_suggested_setup_time
        return None

    @property
    def costable(self) -> bool:
        return (
            self.user_action is not UserAction.REJECTED
            and bool(self.operation_type)
            and self.final_cycle_time is not None
            and self.final_cycle_time > 0
        )

    @property
    def not_costable_reason(self) -> str | None:
        if self.costable:
            return None
        if self.user_action is UserAction.REJECTED:
            return "Removed from the plan by the engineer, so it is not costed."
        if not self.operation_type:
            return "This operation has no process, so it cannot be costed."
        if self.system_time_status is TimeStatus.ESTIMATED and not self.system_time_applies:
            return (
                f"The estimated time was for {self.estimated_for}, but this "
                f"operation is now {self.operation_type}. Enter a time for the process you chose."
            )
        return NOT_COSTED_WITHOUT_TIME

    # ------------------------------------------------------------ transitions
    # Each returns a copy. The system_* fields are never among the updates.

    def accepted(self, *, use_estimated_time: bool = False) -> "PlannedOperation":
        return self.model_copy(
            update={
                "user_action": UserAction.ACCEPTED,
                "system_time_accepted": (
                    self.system_time_accepted or (use_estimated_time and self.system_time_applies)
                ),
            }
        )

    def rejected(self) -> "PlannedOperation":
        """Removed from the plan, but kept as a record of what was proposed."""
        return self.model_copy(update={"user_action": UserAction.REJECTED})

    def with_operation_type(self, operation_type: str) -> "PlannedOperation":
        """Record a process change without touching what the system suggested."""
        chosen = (operation_type or "").strip().lower()
        if not chosen:
            raise ValueError("An operation type is required.")
        if chosen == self.system_suggested_operation:
            # Not a correction: the engineer picked what was proposed.
            return self.model_copy(
                update={"user_operation_type": None, "user_action": UserAction.ACCEPTED}
            )
        return self.model_copy(
            update={
                "user_operation_type": chosen,
                "user_action": UserAction.EDITED,
                # The estimate was for the old process; it must not carry over.
                "system_time_accepted": False,
            }
        )

    def with_times(
        self, *, setup_time_minutes: float | None = None, cycle_time_minutes: float | None = None
    ) -> "PlannedOperation":
        """Record engineer-entered times beside the system's, never over them."""
        updates: dict = {}
        if setup_time_minutes is not None:
            if setup_time_minutes < 0:
                raise ValueError("Setup time cannot be negative.")
            updates["user_setup_time"] = float(setup_time_minutes)
        if cycle_time_minutes is not None:
            if cycle_time_minutes <= 0:
                raise ValueError("Cycle time must be greater than zero.")
            updates["user_cycle_time"] = float(cycle_time_minutes)
        if not updates:
            return self
        if self.user_action in (UserAction.PENDING, UserAction.ACCEPTED):
            updates["user_action"] = (
                UserAction.EDITED if self._time_differs(updates) else UserAction.ACCEPTED
            )
        return self.model_copy(update=updates)

    def _time_differs(self, updates: dict) -> bool:
        setup = updates.get("user_setup_time", self.user_setup_time)
        cycle = updates.get("user_cycle_time", self.user_cycle_time)
        return (
            cycle is not None and cycle != self.system_suggested_cycle_time
        ) or (setup is not None and setup != self.system_suggested_setup_time)

    def with_estimate(self, estimate: OperationTimeEstimate) -> "PlannedOperation":
        """Attach a system time estimate. Only ever called at plan creation."""
        return self.model_copy(
            update={
                "system_suggested_setup_time": estimate.setup_time_minutes,
                "system_suggested_cycle_time": estimate.cycle_time_minutes,
                "system_time_method": estimate.method,
                "system_time_confidence": estimate.confidence,
                "system_time_status": estimate.time_status,
                "system_time_profile_id": estimate.time_profile_id,
                "system_time_profile_version": estimate.time_profile_version,
                "system_time_profile_name": estimate.time_profile_name,
                "system_time_parameters": dict(estimate.parameters),
                "system_time_reason": estimate.reason,
                "system_time_evidence": list(estimate.evidence),
                "system_time_assumptions": list(estimate.assumptions),
                "system_time_missing": list(estimate.missing),
                "system_drilling_steps": list(estimate.drilling_steps),
                "system_drilling_options": list(estimate.drilling_options),
            }
        )

    def with_weld(self, weld: WeldSpec, estimate: OperationTimeEstimate) -> "PlannedOperation":
        """Record the weld the engineer described and the system's time for it.

        The time depends on the engineer's own figures, so describing the weld
        again re-estimates it — and clears any earlier acceptance, because what
        was accepted was the time for a weld that is no longer the one described.
        """
        return self.with_estimate(estimate).model_copy(
            update={
                "weld": weld,
                "system_time_operation_type": "welding",
                "system_time_accepted": False,
            }
        )

    def with_drilling_steps(self, steps: list[float]) -> "PlannedOperation":
        """Record the engineer's drilling sequence beside the system's.

        Choosing the suggested sequence again is not a correction, so it is
        recorded as no choice at all — the same rule a process edit follows.
        """
        chosen = [round(float(step), 3) for step in steps]
        if not chosen or any(later <= earlier for earlier, later in zip(chosen, chosen[1:])):
            raise ValueError("A drilling sequence must list increasing diameters, smallest first.")
        # A different sequence is different work, so an earlier acceptance of the
        # estimate does not carry over — the same rule as a change of process.
        if chosen == self.system_drilling_steps:
            update: dict = {"user_drilling_steps": None, "system_time_accepted": False}
            if self.user_operation_type is None and self.user_action is UserAction.EDITED:
                update["user_action"] = UserAction.ACCEPTED
            return self.model_copy(update=update)
        update = {"user_drilling_steps": chosen, "system_time_accepted": False}
        if self.user_action in (UserAction.PENDING, UserAction.ACCEPTED):
            update["user_action"] = UserAction.EDITED
        return self.model_copy(update=update)


class ManufacturingPlan(BaseModel):
    """Setups, each holding ordered operations.

        ManufacturingPlan
         └── setups
              ├── Setup 1 ── Facing, Drill H1, Drill H2, Drill H3
              └── Setup 2 ── Drill H4

    Two levels of order, both explicit and both persisted: the setups run in
    their own sequence, and the operations run in theirs *within* a setup.
    Neither changes a number today — there is no sequence-dependent time and no
    routing optimisation — but setup *membership* does, because setup time is
    charged once per setup.
    """

    model_config = ConfigDict(frozen=True)

    setups: list[ManufacturingSetup] = Field(default_factory=list)
    operations: list[PlannedOperation] = Field(default_factory=list)
    units: str = "mm"
    assumptions_are_illustrative: bool = True
    source_filename: str = ""
    time_profile_id: str | None = None
    time_profile_version: str | None = None
    time_profile_name: str | None = None
    route_processes: list[str] | None = Field(
        default=None,
        description=(
            "The process types the engineer's chosen manufacturing route includes. None means "
            "no route has been decided and every operation counts, which is how every plan "
            "behaved before routes existed. An operation whose process is not listed is kept "
            "in the plan, unchanged, but is not costed — changing the route back, or the "
            "operation's process, brings it back."
        ),
    )
    as_formed_feature_types: list[str] | None = Field(
        default=None,
        description=(
            "On a decided route that casts or forges a blank and then machines it: the feature "
            "types the blank leaves as formed by default (its blends). None on any other route, "
            "and then nothing is left as formed. An operation's own `user_machined` overrides "
            "the default either way."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _ensure_every_operation_has_a_setup(cls, data):
        """Accept a plan that predates setups, and give it one.

        Plans stored before this step, and any hand-built without setups, are
        read as a single system-assumed setup holding everything — which is the
        same conservative default a fresh plan gets, and is what those plans
        already meant.
        """
        if not isinstance(data, dict):
            return data
        operations = data.get("operations") or []
        if not operations or data.get("setups"):
            return data

        ids = [
            operation.get("operation_id") if isinstance(operation, dict) else operation.operation_id
            for operation in operations
        ]
        return {
            **data,
            "setups": [default_setup(ids).model_dump(mode="python")],
            "operations": [
                {**operation, "system_setup_id": operation.get("system_setup_id") or DEFAULT_SETUP_ID}
                if isinstance(operation, dict)
                else operation.model_copy(
                    update={"system_setup_id": operation.system_setup_id or DEFAULT_SETUP_ID}
                )
                for operation in operations
            ],
        }

    # ---------------------------------------------------------------- reading

    @property
    def ordered_setups(self) -> list[ManufacturingSetup]:
        return sorted(self.setups, key=lambda s: (s.sequence_position, s.setup_id))

    def setup(self, setup_id: str) -> ManufacturingSetup:
        for candidate in self.setups:
            if candidate.setup_id == setup_id:
                return candidate
        raise ValueError(f"Unknown setup '{setup_id}' in this plan.")

    def operations_in(self, setup_id: str, *, active_only: bool = True) -> list[PlannedOperation]:
        """The operations of one setup, in their own order."""
        members = [o for o in self.operations if o.setup_id == setup_id]
        if active_only:
            members = [o for o in members if o.user_action is not UserAction.REJECTED]
        return sorted(members, key=lambda o: (o.sequence_position, o.operation_id))

    @property
    def ordered(self) -> list[PlannedOperation]:
        """Every operation, setups in order and operations in order within them.

        Falls back to the operation's own position for anything whose setup is
        unknown, so a malformed plan still reads rather than raising.
        """
        position = {setup.setup_id: setup.sequence_position for setup in self.setups}
        return sorted(
            self.operations,
            key=lambda o: (
                position.get(o.setup_id, 10**6),
                o.sequence_position,
                o.operation_id,
            ),
        )

    @property
    def active(self) -> list[PlannedOperation]:
        """Everything the engineer has not removed."""
        return [o for o in self.ordered if o.user_action is not UserAction.REJECTED]

    def get(self, operation_id: str) -> PlannedOperation:
        for operation in self.operations:
            if operation.operation_id == operation_id:
                return operation
        raise ValueError(f"Unknown operation '{operation_id}' in this plan.")

    def in_route(self, operation: PlannedOperation) -> bool:
        """Whether the chosen route includes this operation's process. Always true with no route."""
        if self.route_processes is None:
            return True
        return (operation.operation_type or "") in self.route_processes

    def out_of_route_operations(self) -> list[PlannedOperation]:
        """Active operations the chosen route leaves out. Kept, not costed, not deleted."""
        return [o for o in self.active if not self.in_route(o)]

    def is_as_formed(self, operation: PlannedOperation) -> bool:
        """Whether a cast or forged blank leaves this unmachined. Never on any other route."""
        if self.as_formed_feature_types is None:
            return False
        if operation.user_machined is not None:
            return not operation.user_machined
        return (operation.feature_type or "") in self.as_formed_feature_types

    def as_formed_operations(self) -> list[PlannedOperation]:
        """In-route operations left as cast or forged. Kept, not costed, not deleted."""
        return [o for o in self.active if self.in_route(o) and self.is_as_formed(o)]

    def counts_toward_cost(self, operation: PlannedOperation) -> bool:
        """The one test of whether an operation reaches the cost.

        Active, in the chosen route, machined rather than left as formed, and
        carrying a committed time. Costing, the time summary and the feedback
        record all read this, so none of them can disagree about what was costed.
        """
        return (
            operation.user_action is not UserAction.REJECTED
            and operation.costable
            and self.in_route(operation)
            and not self.is_as_formed(operation)
        )

    def uncosted_operations(self) -> list[PlannedOperation]:
        """Active, in-route, machined operations that carry no committed time, so are left out.

        An operation outside the route, or left as formed, is not "missing a
        time" — it is excluded on purpose — so it is reported by
        `out_of_route_operations` or `as_formed_operations` instead.
        """
        return [
            o for o in self.active if self.in_route(o) and not self.is_as_formed(o) and not o.costable
        ]

    def to_operation_requirements(self, quantity: int) -> list[OperationRequirement]:
        """The costable operations, as the existing unmodified costing wants them.

        **Setup time is charged once per setup, not once per operation.** The
        existing `OperationRequirement` has no setup concept — it carries a
        `setup_time_minutes` per operation and the unchanged
        `calculate_operation_cost` charges it once per requirement — so the
        bridge attaches each setup's whole time to the *first costable operation
        in that setup* and zero to the rest.

        That is arithmetic, and arithmetic is exactly where this could go wrong,
        so note what it is not: it is not a division, not an average, and not an
        inference. The setup is a real object in this plan with real membership
        and its own time; this method only chooses where to hang that time so
        the existing formulas total it correctly. `time_summary()` reports the
        setup and cycle totals separately, which is how the distinction stays
        visible rather than being buried here.

        Operations without a committed cycle time are omitted, not defaulted;
        `uncosted_operations` names them.
        """
        requirements: list[OperationRequirement] = []
        charged: set[str] = set()

        for operation in self.ordered:
            if not self.counts_toward_cost(operation):
                continue

            setup_time = 0.0
            setup_id = operation.setup_id
            if setup_id is not None and setup_id not in charged:
                try:
                    setup_time = self.setup(setup_id).charged_setup_time
                except ValueError:
                    setup_time = 0.0
                charged.add(setup_id)

            requirements.append(
                OperationRequirement(
                    operation_id=operation.operation_id,
                    operation_name=operation.operation_name or operation.operation_id,
                    process_type=operation.operation_type,
                    setup_time_minutes=setup_time,
                    cycle_time_minutes=operation.final_cycle_time,
                    quantity=quantity,
                )
            )
        return requirements

    def time_summary(self, quantity: int = 1) -> "PlanTimeSummary":
        """Setup and cycle time, kept apart so the distinction is legible.

        This is what makes "10 min of setup across 1 setup, plus 7.2 min of
        cycle" sayable. Nothing here is money: it reports minutes, and the
        existing costing turns them into a cost at the rate set's rates.
        """
        per_setup: list[SetupTimeSummary] = []
        for setup in self.ordered_setups:
            members = [
                o for o in self.operations_in(setup.setup_id) if self.counts_toward_cost(o)
            ]
            cycle = sum((o.final_cycle_time or 0.0) * quantity for o in members)
            per_setup.append(
                SetupTimeSummary(
                    setup_id=setup.setup_id,
                    name=setup.name,
                    sequence_position=setup.sequence_position,
                    setup_time_minutes=setup.setup_time_minutes,
                    charged_setup_time_minutes=setup.charged_setup_time if members else 0.0,
                    setup_time_source=setup.setup_time_source,
                    operation_count=len(self.operations_in(setup.setup_id)),
                    costable_operation_count=len(members),
                    cycle_time_minutes=round(cycle, 6),
                )
            )

        total_setup = sum(entry.charged_setup_time_minutes for entry in per_setup)
        total_cycle = sum(entry.cycle_time_minutes for entry in per_setup)
        return PlanTimeSummary(
            quantity=quantity,
            setup_count=len(per_setup),
            charged_setup_count=sum(
                1 for entry in per_setup if entry.costable_operation_count and entry.charged_setup_time_minutes
            ),
            total_setup_time_minutes=round(total_setup, 6),
            total_cycle_time_minutes=round(total_cycle, 6),
            total_time_minutes=round(total_setup + total_cycle, 6),
            setups=per_setup,
            uncosted_operation_ids=[o.operation_id for o in self.uncosted_operations()],
            setups_needing_time=[
                entry.setup_id
                for entry in per_setup
                if entry.costable_operation_count and entry.setup_time_minutes is None
            ],
            out_of_route_operation_ids=[o.operation_id for o in self.out_of_route_operations()],
            as_formed_operation_ids=[o.operation_id for o in self.as_formed_operations()],
        )

    # ---------------------------------------------------------------- editing

    # ------------------------------------------------------------ setup editing

    def replacing_setup(self, setup: ManufacturingSetup) -> "ManufacturingPlan":
        return self.model_copy(
            update={
                "setups": [
                    setup if s.setup_id == setup.setup_id else s for s in self.setups
                ]
            }
        )

    def next_setup_id(self) -> str:
        used = {s.setup_id for s in self.setups}
        index = len(used) + 1
        while f"SETUP-{index:03d}" in used:
            index += 1
        return f"SETUP-{index:03d}"

    def with_setup(
        self, *, name: str = "", setup_id: str | None = None, setup_time_minutes: float | None = None
    ) -> "ManufacturingSetup":
        """Build a new, empty, engineer-created setup for this plan.

        Its `system_*` fields stay empty: the system proposed nothing here, and
        recording a grouping it never made would put a false claim into the
        feedback data.
        """
        resolved_id = setup_id or self.next_setup_id()
        setup = ManufacturingSetup(
            setup_id=resolved_id,
            sequence_position=len(self.setups) + 1,
            name=name.strip() or f"Setup {len(self.setups) + 1}",
            origin=SuggestionOrigin.USER,
            system_grouping="user_defined",
            system_grouping_reason="Added by the engineer; the system proposed no such setup.",
            system_grouping_confidence=Confidence.LOW,
            user_action=UserAction.ADDED,
        )
        if setup_time_minutes is not None:
            setup = setup.with_setup_time(setup_time_minutes).model_copy(
                update={"user_action": UserAction.ADDED}
            )
        return setup

    def with_added_setup(self, setup: ManufacturingSetup) -> "ManufacturingPlan":
        if any(s.setup_id == setup.setup_id for s in self.setups):
            raise ValueError(f"Setup '{setup.setup_id}' already exists in this plan.")
        return self.model_copy(update={"setups": [*self.setups, setup]})._renumber_setups()

    def without_setup(self, setup_id: str) -> "ManufacturingPlan":
        """Remove a setup. Refused while it still holds operations.

        Deleting a setup with work in it would either orphan those operations or
        silently move them somewhere the engineer did not choose. Both are worse
        than making them move it first.
        """
        setup = self.setup(setup_id)
        members = self.operations_in(setup_id, active_only=False)
        if members:
            raise ValueError(
                f"Setup '{setup.name}' still holds {len(members)} operation(s). Move them to "
                "another setup before removing it."
            )
        if len(self.setups) <= 1:
            raise ValueError("A plan needs at least one setup.")
        return self.model_copy(
            update={"setups": [s for s in self.setups if s.setup_id != setup_id]}
        )._renumber_setups()

    def _renumber_setups(self, order: list[ManufacturingSetup] | None = None) -> "ManufacturingPlan":
        sequence = order if order is not None else self.ordered_setups
        return self.model_copy(
            update={
                "setups": [
                    setup.model_copy(update={"sequence_position": index})
                    for index, setup in enumerate(sequence, start=1)
                ]
            }
        )

    def moved_setup(self, setup_id: str, offset: int) -> "ManufacturingPlan":
        """Move a whole setup earlier (-1) or later (+1) in the routing."""
        order = self.ordered_setups
        index = next((i for i, s in enumerate(order) if s.setup_id == setup_id), None)
        if index is None:
            raise ValueError(f"Unknown setup '{setup_id}' in this plan.")
        target = index + offset
        if target < 0 or target >= len(order):
            return self
        order.insert(target, order.pop(index))
        return self._renumber_setups(order)

    def with_operation_in_setup(self, operation_id: str, setup_id: str) -> "ManufacturingPlan":
        """Move one operation into another setup, then renumber both.

        The operation lands at the end of its new setup, which is the only
        position that needs no guess about intent.
        """
        self.setup(setup_id)  # raises if unknown
        operation = self.get(operation_id)
        moved = operation.moved_to_setup(setup_id)
        return self.replacing(moved)._renumber_operations_in_setups()

    def _renumber_operations_in_setups(self) -> "ManufacturingPlan":
        """Give every setup's operations contiguous positions from 1."""
        updated: list[PlannedOperation] = []
        seen: set[str] = set()
        for setup in self.ordered_setups:
            for index, operation in enumerate(
                self.operations_in(setup.setup_id, active_only=False), start=1
            ):
                updated.append(operation.model_copy(update={"sequence_position": index}))
                seen.add(operation.operation_id)
        # Anything whose setup is unknown keeps its position rather than vanishing.
        updated.extend(o for o in self.operations if o.operation_id not in seen)
        return self.model_copy(update={"operations": updated})

    def replacing(self, operation: PlannedOperation) -> "ManufacturingPlan":
        return self.model_copy(
            update={
                "operations": [
                    operation if o.operation_id == operation.operation_id else o
                    for o in self.operations
                ]
            }
        )

    def _positioned(self, operations: list[PlannedOperation]) -> "ManufacturingPlan":
        """Number the given list 1..n **within each setup**, in the order given.

        Takes the order from the list rather than re-deriving it, because every
        caller here has just built the order it wants. Sorting again would read
        the stale `sequence_position` values and undo the change. Counters are
        per setup because an operation's position is its position in its own
        setup, not in the plan as a whole.
        """
        counters: dict[str | None, int] = {}
        updated: list[PlannedOperation] = []
        for operation in operations:
            counters[operation.setup_id] = counters.get(operation.setup_id, 0) + 1
            updated.append(
                operation.model_copy(update={"sequence_position": counters[operation.setup_id]})
            )
        return self.model_copy(update={"operations": updated})

    def renumbered(self) -> "ManufacturingPlan":
        """Close gaps left by removals, keeping the current order in each setup."""
        return self._positioned(self.ordered)

    def moved(self, operation_id: str, offset: int) -> "ManufacturingPlan":
        """Move one operation up (-1) or down (+1) **within its own setup**.

        Reordering never crosses a setup boundary: moving work to another
        fixturing is a different decision with a different cost, so it has its
        own call, `with_operation_in_setup`.
        """
        operation = self.get(operation_id)
        within = self.operations_in(operation.setup_id, active_only=False)
        index = next((i for i, o in enumerate(within) if o.operation_id == operation_id), None)
        if index is None:
            raise ValueError(f"Unknown operation '{operation_id}' in this plan.")
        target = index + offset
        if target < 0 or target >= len(within):
            return self  # already at an end of its setup; not an error
        within.insert(target, within.pop(index))

        # Rebuild the global order with this setup's new internal order in place.
        rebuilt: list[PlannedOperation] = []
        for setup in self.ordered_setups:
            if setup.setup_id == operation.setup_id:
                rebuilt.extend(within)
            else:
                rebuilt.extend(self.operations_in(setup.setup_id, active_only=False))
        seen = {o.operation_id for o in rebuilt}
        rebuilt.extend(o for o in self.operations if o.operation_id not in seen)
        return self._positioned(rebuilt)

    def reordered(self, operation_ids: list[str]) -> "ManufacturingPlan":
        """Set the whole sequence at once. Every operation must be listed exactly once."""
        known = {o.operation_id for o in self.operations}
        if sorted(operation_ids) != sorted(known):
            raise ValueError(
                "A reordering must list every operation in the plan exactly once. "
                f"Expected {len(known)} ids, got {len(operation_ids)}."
            )
        position = {operation_id: index for index, operation_id in enumerate(operation_ids, start=1)}
        return self.model_copy(
            update={
                "operations": [
                    o.model_copy(update={"sequence_position": position[o.operation_id]})
                    for o in self.operations
                ]
            }
        )

    def with_added(self, operation: PlannedOperation) -> "ManufacturingPlan":
        return self._positioned([*self.ordered, operation])

    def without(self, operation_id: str) -> "ManufacturingPlan":
        """Delete a line outright.

        For a *suggested* operation prefer `rejected()`, which keeps the record
        of what was proposed. Deleting is for lines the engineer added.
        """
        remaining = [o for o in self.ordered if o.operation_id != operation_id]
        if len(remaining) == len(self.operations):
            raise ValueError(f"Unknown operation '{operation_id}' in this plan.")
        return self._positioned(remaining)


# --------------------------------------------------------------- construction


def build_plan(
    suggestions: list[OperationSuggestion],
    *,
    units: str = "mm",
    assumptions: TimeAssumptions | None = None,
    profile=None,
    source_filename: str = "",
    id_prefix: str = "",
) -> ManufacturingPlan:
    """Turn Step 19 suggestions into a plan, estimating a time for each.

    Estimating is not committing: every operation starts `pending` with
    `system_time_accepted` false, so nothing here can reach a cost until a
    person accepts it.

    Sequence starts as the order recognition produced — holes before planes,
    which is deterministic but carries no manufacturing claim. It is a starting
    point to be reordered, not a routing decision.

    `id_prefix` namespaces the operation and setup ids this plan generates. It
    exists for Step 28: an assembly's components each get their own plan, and
    two of them would otherwise both call their first operation `OP-001`, so an
    operation could not be traced back to the component that caused it. It is a
    prefix rather than a post-hoc relabelling because rewriting ids afterwards
    would have to keep `system_setup_id`, `user_setup_id` and the setup's own
    membership list in step with each other — exactly the kind of bookkeeping
    that goes wrong silently. Passing it in means the plan is *born* consistent
    and the existing validator checks it like any other.
    """
    # A profile supplies the parameters *and* the identity recorded against each
    # prediction, so it is passed instead of its assumptions rather than
    # alongside them — the estimator treats explicit assumptions as an override
    # that clears the attribution, which is right, but only if we do not
    # accidentally pass the profile's own values as an "override" of itself.
    explicit = assumptions is not None
    stamped = None if explicit else profile
    resolved = assumptions if explicit else (getattr(profile, "assumptions", None) or TimeAssumptions())
    operations: list[PlannedOperation] = []

    for index, suggestion in enumerate(suggestions, start=1):
        operation = PlannedOperation(
            operation_id=f"{id_prefix}OP-{index:03d}",
            sequence_position=index,
            feature_ids=[suggestion.feature_id] if suggestion.feature_id else [],
            feature_label=suggestion.feature_label,
            feature_type=suggestion.feature_type,
            feature_subtype=suggestion.feature_subtype,
            operation_name=suggestion.operation_name,
            origin=SuggestionOrigin.SYSTEM,
            user_action=UserAction.PENDING,
            system_suggested_operation=suggestion.operation_type,
            system_suggestion_confidence=suggestion.confidence,
            system_suggestion_rule=suggestion.rule_id,
            system_source_type=getattr(suggestion, "source_type", None),
            system_feature_confidence=getattr(suggestion, "feature_confidence", None),
            source_faces=list(suggestion.source_faces),
            source_facets=list(suggestion.source_facets),
            dimensions=dict(suggestion.dimensions),
        )
        operations.append(
            operation.with_estimate(
                estimate_for_suggestion(
                    suggestion,
                    units=units,
                    assumptions=assumptions if explicit else None,
                    profile=stamped,
                )
            )
        )

    # Every operation starts in one setup. The setup's estimated time is migrated
    # out of the operations rather than left on each of them: the operations keep
    # their per-process baseline as a record, but only the setup's figure is ever
    # charged, and it is charged once.
    setup_id = f"{id_prefix}{DEFAULT_SETUP_ID}"
    operations = [o.model_copy(update={"system_setup_id": setup_id}) for o in operations]
    setup = default_setup([o.operation_id for o in operations], setup_id=setup_id).model_copy(
        update={
            "system_setup_time_minutes": _setup_baseline_for(operations),
            "system_setup_time_source": "largest_process_baseline",
            "process_context": _dominant_process(operations),
            "system_grouping_assumptions": [
                *SYSTEM_GROUPING_ASSUMPTIONS,
                SETUP_TIME_DERIVATION,
            ],
        }
    )

    return ManufacturingPlan(
        setups=[setup],
        operations=operations,
        units=units,
        assumptions_are_illustrative=resolved.is_illustrative,
        source_filename=source_filename,
        time_profile_id=getattr(stamped, "time_profile_id", None),
        time_profile_version=getattr(stamped, "version", None),
        time_profile_name=getattr(stamped, "name", None),
    )


def _setup_baseline_for(operations: list[PlannedOperation]) -> float | None:
    """The largest per-process setup baseline among a setup's operations.

    The **largest**, never the sum. One fixturing event takes as long as its most
    demanding process needs; adding six drilling setups together is precisely the
    error this whole step exists to remove. Nor is it an average, which would be
    a number no process actually has.
    """
    baselines = [
        o.system_suggested_setup_time
        for o in operations
        if o.system_suggested_setup_time is not None
    ]
    return max(baselines) if baselines else None


def _dominant_process(operations: list[PlannedOperation]) -> str | None:
    """The process every operation shares, or None when they differ.

    Descriptive only. It is never used to decide that operations *can* share a
    setup — a shared process type is not evidence of shared fixturing.
    """
    processes = {o.operation_type for o in operations if o.operation_type}
    return next(iter(processes)) if len(processes) == 1 else None


def manual_operation(
    *,
    operation_id: str,
    sequence_position: int,
    operation_type: str,
    operation_name: str,
    setup_time_minutes: float | None = None,
    cycle_time_minutes: float | None = None,
    feature_ids: list[str] | None = None,
    setup_id: str = DEFAULT_SETUP_ID,
) -> PlannedOperation:
    """An operation the engineer added, which no rule proposed.

    Its `system_suggested_*` fields stay empty — there was no prediction, and
    inventing one would put a system claim into the feedback data that the
    system never made.
    """
    chosen = (operation_type or "").strip().lower()
    if not chosen:
        raise ValueError("A manually added operation needs an operation type.")
    operation = PlannedOperation(
        operation_id=operation_id,
        sequence_position=sequence_position,
        system_setup_id=setup_id,
        feature_ids=feature_ids or [],
        operation_name=operation_name or operation_id,
        origin=SuggestionOrigin.USER,
        user_action=UserAction.ADDED,
        user_operation_type=chosen,
        system_time_status=TimeStatus.NEEDS_USER_INPUT,
        system_time_reason="Added by the engineer; the system proposed nothing for this line.",
    )
    if setup_time_minutes is not None or cycle_time_minutes is not None:
        operation = operation.with_times(
            setup_time_minutes=setup_time_minutes, cycle_time_minutes=cycle_time_minutes
        ).model_copy(update={"user_action": UserAction.ADDED})
    return operation


def estimate_missing_times(
    plan: ManufacturingPlan,
    *,
    assumptions: TimeAssumptions | None = None,
    profile=None,
) -> ManufacturingPlan:
    """Fill in estimates for operations that have none, without disturbing any.

    Only touches operations whose `system_time_status` is `needs_user_input`,
    and never an operation whose process the engineer changed — an estimate for
    the process they rejected would be noise, and one written into the system
    fields after the fact would corrupt the prediction record.
    """
    updated: list[PlannedOperation] = []
    for operation in plan.operations:
        needs = operation.system_time_status is not TimeStatus.ESTIMATED
        untouched_process = operation.user_operation_type is None
        if needs and untouched_process and operation.operation_type:
            estimate = estimate_operation_time(
                operation_type=operation.operation_type,
                feature_type=operation.feature_type,
                feature_subtype=operation.feature_subtype,
                dimensions=operation.dimensions,
                units=plan.units,
                suggestion_confidence=operation.system_suggestion_confidence,
                assumptions=assumptions,
                profile=None if assumptions is not None else profile,
            )
            updated.append(operation.with_estimate(estimate))
        else:
            updated.append(operation)
    return plan.model_copy(update={"operations": updated})


__all__ = [
    "DEFAULT_SETUP_ID",
    "NOT_COSTED_WITHOUT_SETUP_TIME",
    "NOT_COSTED_WITHOUT_TIME",
    "SETUP_TIME_DERIVATION",
    "SYSTEM_GROUPING_ASSUMPTIONS",
    "SYSTEM_GROUPING_REASON",
    "ManufacturingSetup",
    "PlanTimeSummary",
    "SetupTimeSummary",
    "default_setup",
    "ManufacturingPlan",
    "PlannedOperation",
    "TimeSource",
    "build_plan",
    "estimate_missing_times",
    "manual_operation",
]
