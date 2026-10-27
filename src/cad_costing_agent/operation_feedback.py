"""What the system predicted, and what the engineer actually chose.

One `OperationFeedback` record per planned operation, capturing the difference
between the two — nothing else. It is a *projection* of a `ManufacturingPlan`,
not a second source of truth, so it cannot drift from the plan it came from.

    System:    Milling — 20 min
    Engineer:  Turning — 32 min
    Record:    suggested_operation=milling, final_operation=turning,
               suggested_cycle_time=20, final_cycle_time=32,
               operation_changed=True, cycle_time_changed=True

Why this exists now, with no model behind it
--------------------------------------------
A learning step is only possible if the corrections were recorded while they
happened. Predictions that were silently overwritten are gone, and no amount of
later modelling brings them back. So Step 20 records the pairs and stops there:

- **Nothing is trained.** No model, no fitting, no weights.
- **Nothing adapts.** A recorded correction never changes a later prediction.
  The estimator is a fixed set of formulas and stays that way.
- **Nothing is aggregated.** These are per-operation facts, not statistics.

The only question this data has to be able to answer is the one a future
training pipeline will ask: *what did the system predict, and what did the
engineer actually choose?*

Rejected operations are recorded too. "The system suggested this and the
engineer removed it" is a correction like any other, and dropping those rows
would leave a dataset that only ever saw its own successes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from cad_costing_agent.features.model import Confidence
from cad_costing_agent.manufacturing_plan import ManufacturingPlan, TimeSource
from cad_costing_agent.operation_suggestions import SuggestionOrigin, UserAction
from cad_costing_agent.operation_time import TimeStatus


class OperationFeedback(BaseModel):
    """One prediction/decision pair, ready to become a training row later.

    Frozen, and written once when an estimate is created. A later edit produces
    a **new** estimate with its own feedback rather than rewriting this one, so
    the history of what was believed when is preserved.
    """

    model_config = ConfigDict(frozen=True)

    estimate_id: str | None = Field(
        default=None, description="Filled in once the estimate this belongs to has an id."
    )
    # Which component of the assembly this operation belongs to. Null for a
    # standalone part, which has no component identity to record. Added in
    # Step 28: without it, "the system said milling and the engineer said
    # turning" is uninterpretable across an assembly, because the same
    # correction means different things on a housing and on a shaft.
    component_id: str | None = None
    component_name: str | None = None
    operation_id: str
    feature_ids: list[str] = Field(default_factory=list)
    feature_id: str | None = Field(
        default=None, description="The first feature, for convenience; `feature_ids` is complete."
    )
    sequence_position: int

    # --- what the system said ---
    suggested_operation: str | None = None
    suggested_setup_time: float | None = None
    suggested_cycle_time: float | None = None
    suggestion_confidence: Confidence | None = None
    time_confidence: Confidence | None = None
    # Which rule fired, on what kind of geometry, from a feature recognized how
    # confidently. Recorded so the analytics layer can measure a rule's hit rate
    # and compare B-Rep against faceted evidence without re-running recognition
    # — and so a historical correction stays interpretable when the rules change.
    rule_id: str | None = None
    source_type: str | None = Field(
        default=None, description="'brep' or 'faceted', as the suggestion reported it."
    )
    feature_confidence: Confidence | None = None
    time_method: str | None = None
    time_status: TimeStatus = TimeStatus.NEEDS_USER_INPUT
    # Which published assumptions produced the prediction. Without this a
    # correction is uninterpretable: "the system said 20 min and the engineer
    # said 32" means nothing unless you know which feeds and speeds the 20 came
    # from. A later training step would otherwise be fitting to a moving target.
    time_profile_id: str | None = None
    time_profile_version: str | None = None
    time_profile_name: str | None = None
    time_parameters: dict[str, float] = Field(
        default_factory=dict,
        description="The exact parameter values behind the prediction, for a later comparison.",
    )

    # --- how the system grouped it into a fixturing, and where it ended up ---
    #
    # "The system put H1-H4 in one setup and the engineer moved H4 out" is a
    # correction like any other, and one a later step would very much want: it
    # is the shape of every real fixturing decision the CAD cannot prove.
    system_setup_id: str | None = None
    final_setup_id: str | None = None
    setup_changed: bool = False
    system_setup_grouping: str | None = Field(
        default=None, description="'system_assumed' or 'user_defined' for the setup it ended in."
    )
    system_setup_time_minutes: float | None = Field(
        default=None, description="What the system estimated for the setup this operation is in."
    )
    final_setup_time_minutes: float | None = Field(
        default=None, description="The setup's committed time, charged once for the whole setup."
    )
    setup_time_corrected: bool = Field(
        default=False,
        description=(
            "The *setup's* time was changed. Distinct from `setup_time_changed`, which is the "
            "operation's own per-process baseline and no longer reaches a cost."
        ),
    )
    setup_operation_count: int = Field(
        default=0, description="How many operations shared the setup this one ended up in."
    )

    # --- what the engineer chose ---
    final_operation: str | None = None
    final_setup_time: float | None = None
    final_cycle_time: float | None = None
    time_source: TimeSource = TimeSource.NEEDS_INPUT

    # --- the difference, precomputed so a reader cannot get it wrong ---
    operation_changed: bool = False
    setup_time_changed: bool = False
    cycle_time_changed: bool = False

    user_action: UserAction = UserAction.PENDING
    origin: SuggestionOrigin = SuggestionOrigin.SYSTEM
    costed: bool = Field(
        default=False, description="Whether this operation actually contributed to the cost."
    )
    timestamp: datetime


def _source_type_of(operation) -> str | None:
    """Where the evidence came from, recorded if it was and derived if it was not.

    Plans built before `system_source_type` existed still carry the viewer
    linkage, and that answers the same question: a B-Rep recognizer reports
    `source_faces` and no facets, a faceted one reports `source_facets` and no
    faces. The derivation is only a fallback, and a test proves it agrees with
    the recorded value on both a real B-Rep and a real faceted model.
    """
    recorded = getattr(operation, "system_source_type", None)
    if recorded:
        return recorded
    if operation.source_facets:
        return "faceted"
    if operation.source_faces:
        return "brep"
    return None


def _changed(suggested: float | None, final: float | None) -> bool:
    """A change needs both a prediction and a decision to compare.

    An operation the system never predicted a time for was not *corrected* — it
    was simply entered. Counting that as a correction would make the dataset
    look like the estimator was wrong far more often than it was.
    """
    if suggested is None or final is None:
        return False
    return suggested != final


def _setup_or_none(plan: ManufacturingPlan, setup_id: str | None):
    if setup_id is None:
        return None
    try:
        return plan.setup(setup_id)
    except ValueError:
        return None


def _grouping_of(plan: ManufacturingPlan, setup_id: str | None) -> str | None:
    setup = _setup_or_none(plan, setup_id)
    return setup.system_grouping if setup else None


def _system_setup_time(plan: ManufacturingPlan, setup_id: str | None) -> float | None:
    setup = _setup_or_none(plan, setup_id)
    return setup.system_setup_time_minutes if setup else None


def _final_setup_time(plan: ManufacturingPlan, setup_id: str | None) -> float | None:
    setup = _setup_or_none(plan, setup_id)
    return setup.setup_time_minutes if setup else None


def _setup_time_corrected(plan: ManufacturingPlan, setup_id: str | None) -> bool:
    setup = _setup_or_none(plan, setup_id)
    if setup is None:
        return False
    return _changed(setup.system_setup_time_minutes, setup.setup_time_minutes)


def feedback_from_plan(
    plan: ManufacturingPlan,
    *,
    estimate_id: str | None = None,
    at: datetime | None = None,
    component_id: str | None = None,
    component_name: str | None = None,
) -> list[OperationFeedback]:
    """Project a plan into one feedback record per operation, in sequence order.

    `at` is injectable so a caller — or a test — can pin the timestamp. It
    defaults to now in UTC, which is the only value in the record that varies
    between otherwise identical runs.

    `component_id` names the assembly component whose plan this is, and is left
    null for a standalone part. It is carried rather than derived from the
    operation ids, because the id prefix is a convenience and the component's
    identity is a fact.
    """
    moment = at or datetime.now(timezone.utc)

    return [
        OperationFeedback(
            estimate_id=estimate_id,
            component_id=component_id,
            component_name=component_name,
            operation_id=operation.operation_id,
            feature_ids=list(operation.feature_ids),
            feature_id=operation.feature_ids[0] if operation.feature_ids else None,
            sequence_position=operation.sequence_position,
            suggested_operation=operation.system_suggested_operation,
            suggested_setup_time=operation.system_suggested_setup_time,
            suggested_cycle_time=operation.system_suggested_cycle_time,
            suggestion_confidence=operation.system_suggestion_confidence,
            time_confidence=operation.system_time_confidence,
            rule_id=operation.system_suggestion_rule,
            source_type=_source_type_of(operation),
            feature_confidence=getattr(operation, "system_feature_confidence", None),
            time_method=operation.system_time_method,
            time_status=operation.system_time_status,
            time_profile_id=operation.system_time_profile_id,
            time_profile_version=operation.system_time_profile_version,
            time_profile_name=operation.system_time_profile_name,
            time_parameters=dict(operation.system_time_parameters),
            system_setup_id=operation.system_setup_id,
            final_setup_id=operation.setup_id,
            setup_changed=operation.setup_was_changed,
            system_setup_grouping=_grouping_of(plan, operation.setup_id),
            system_setup_time_minutes=_system_setup_time(plan, operation.setup_id),
            final_setup_time_minutes=_final_setup_time(plan, operation.setup_id),
            setup_time_corrected=_setup_time_corrected(plan, operation.setup_id),
            setup_operation_count=len(plan.operations_in(operation.setup_id))
            if operation.setup_id
            else 0,
            final_operation=operation.operation_type,
            final_setup_time=operation.final_setup_time,
            final_cycle_time=operation.final_cycle_time,
            time_source=operation.time_source,
            operation_changed=(
                operation.system_suggested_operation is not None
                and operation.operation_type != operation.system_suggested_operation
            ),
            setup_time_changed=_changed(
                operation.system_suggested_setup_time, operation.final_setup_time
            ),
            cycle_time_changed=_changed(
                operation.system_suggested_cycle_time, operation.final_cycle_time
            ),
            user_action=operation.user_action,
            origin=operation.origin,
            # What the plan actually costed — not merely whether the operation
            # carries a time: one outside the chosen route, or left as cast or
            # forged, has a time and was still not costed.
            costed=plan.counts_toward_cost(operation),
            timestamp=moment,
        )
        for operation in plan.ordered
    ]


__all__ = ["OperationFeedback", "feedback_from_plan"]
