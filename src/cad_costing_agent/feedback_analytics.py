"""What the system predicted, what the engineer chose, and where they differ.

Why this exists now
-------------------
Four steps have been recording corrections: the operation and time pairs from
Step 20, the setup regroupings from Step 21, the make/buy pairs from Step 26,
and since Step 28 every one of those again with the component that produced it.
None of it has ever been read back. A dataset nobody looks at is indistinguishable
from no dataset, and the point of recording predictions beside decisions was
always to find out **where the system is wrong before teaching it to improve**.

So this module reads the corrections and reports them. That is all it does.

Nothing here learns
-------------------
This is the constraint the whole module is built to make structurally obvious:

- No model is fitted, no weights exist, no rule is changed, no confidence is
  recalibrated, no time profile is adjusted, and no future estimate is affected.
- It imports **nothing** from the rest of the package. It reads plain
  dictionaries — the snapshots as they were written — and returns plain models.
  It cannot reach a rule, a profile, a rate set or a repository, so it cannot
  modify one, and a structural test asserts the empty import set in both
  directions.
- It contains no `save`, `add`, `commit`, `update` or `delete`, asserted by a
  test reading this source.

`LEARNING_DISCLAIMER` says so in every response, because a page full of
correction rates is exactly where a reader starts to assume something is
adapting.

Snapshots are read as they were written
---------------------------------------
Every figure comes out of an estimate's own persisted snapshot, by explicit
`.get()` on plain dicts rather than by validating into today's models. That is
deliberate: coercing an old record into a current model would fill absent fields
with defaults that read exactly like recorded data. A field a record does not
have is reported **unknown** and counted in the readiness section, never
defaulted into a statistic.

Consequently, changing today's rates or time profiles cannot alter a single
number here — the same property the cost comparison and the quotation rest on.

Honesty about small samples
---------------------------
Correction rates over four observations are noise wearing a percentage sign. So
every metric that needs a sample carries `sample_count`, `minimum_required` and
`is_actionable`, and a directional bias is reported only above the threshold;
below it the finding is the sentence "not enough evidence for a reliable
pattern". The threshold is configurable, and the summary states which one it was
judged against.

Medians, not means, carry the headline figures: one engineer typing 90 minutes
where the system said 0.6 would otherwise define a shop's entire bias. Outliers
are reported **separately** rather than trimmed silently, because a genuine
order-of-magnitude miss is the most interesting record in the set and must not
be deleted to make the average look calm.

Absent is not the same as missing
---------------------------------
Step 29's readiness report counted every empty field as a gap, and measuring the
real database in Step 30 showed two of its three headline gaps were not gaps at
all. All 449 records "missing" a component identity came from single-part
estimates, where a component identity does not exist; every one of the 264
assembly records already had it. And 302 of the 308 "missing" paired cycle times
were operations nobody ever committed a time for — which Step 29 itself requires
to stay unpaired.

So each evidence field is now classified rather than merely counted:

    known           the value was recorded
    not_applicable  the value legitimately does not exist for this record —
                    a standalone part has no component, an unfinished operation
                    has no paired time
    unrecorded      the value could have existed and was not captured, and
                    something persisted could still recover it
    unrecoverable   the value was never captured and nothing stored can recover
                    it; the CAD file is not kept, so a feature confidence that
                    was not written down is gone for good

`percent_known` divides by the records where the value *could* exist, never by
all of them — the same lesson as the acceptance denominator below. An
`unrecoverable` count is reported as exactly that: a permanent limitation, not a
backlog. **Nothing here converts an unknown into a guess to improve a number.**

An acceptance rate needs the right denominator
----------------------------------------------
An operation nobody ever committed a time for was not accepted and was not
corrected — it was never finished. Measured on this project's own history, 189
of 354 milling suggestions were in that state, mostly because the rate set
prices no rate for the process the system proposed. Counting them in the
denominator made a rule with **one** correction read as 46% acceptance, which
says the opposite of what happened.

So every acceptance rate is measured over the operations somebody actually
decided about — accepted, changed or rejected — and the unfinished ones are
reported separately as `not_finished`. The numerator, the denominator and that
count all travel with the rate, so a reader can see exactly which population it
describes.

Relative error needs a floor. A system estimate of 0.0008 minutes corrected to
0.5 is a 62,400% error and means nothing; records whose predicted time is below
`RELATIVE_ERROR_FLOOR` are excluded from relative-error statistics only, counted
as `near_zero_excluded`, and still included in the absolute and signed figures.
"""

from __future__ import annotations

import statistics
from datetime import datetime

from pydantic import BaseModel, Field

# --------------------------------------------------------------------- vocabulary
#
# Defined locally as plain strings rather than imported. This module has no
# package imports at all — that is what makes "analytics cannot change anything"
# a structural fact rather than a promise — and these are the analytics layer's
# own vocabulary in any case.

OPERATION_CHANGED = "operation_changed"
SETUP_CHANGED = "setup_changed"
SETUP_TIME_CHANGED = "setup_time_changed"
CYCLE_TIME_CHANGED = "cycle_time_changed"
CLASSIFICATION_CHANGED = "classification_changed"
ACCEPTED = "accepted"
REJECTED = "rejected"
REMOVED = "removed"
ADDED = "added"

CORRECTION_TYPES: tuple[str, ...] = (
    OPERATION_CHANGED,
    SETUP_CHANGED,
    SETUP_TIME_CHANGED,
    CYCLE_TIME_CHANGED,
    CLASSIFICATION_CHANGED,
    ACCEPTED,
    REJECTED,
    REMOVED,
    ADDED,
)

UNKNOWN = "unknown"
"""What a field a record does not carry is reported as. Never a default that
would read like recorded data."""

# --------------------------------------------------------------- evidence states

EVIDENCE_KNOWN = "known"
EVIDENCE_NOT_APPLICABLE = "not_applicable"
"""The value legitimately does not exist for this record. A standalone part has
no component identity; an operation nobody committed a time for has no paired
observation. Counting these as gaps overstates what is missing."""

EVIDENCE_UNRECORDED = "unrecorded"
"""It could have existed and was not captured, and something persisted could
still recover it."""

EVIDENCE_UNRECOVERABLE = "unrecoverable"
"""Never captured, and nothing stored can recover it. The CAD file is not kept,
so a feature confidence that was not written down is gone permanently. Reported
as a limitation, never as a backlog."""

EVIDENCE_STATES: tuple[str, ...] = (
    EVIDENCE_KNOWN,
    EVIDENCE_NOT_APPLICABLE,
    EVIDENCE_UNRECORDED,
    EVIDENCE_UNRECOVERABLE,
)

# --------------------------------------------------------------- time pairing

PAIRED = "paired"
"""Both a prediction and a committed engineer time — accepted or corrected."""

UNFINISHED = "unfinished"
"""A prediction nobody committed a time for. Correctly unpaired: it is not a
missing observation, it is an operation that was never finished."""

NOT_PREDICTED = "not_predicted"
"""No prediction to pair with. Usually a hand-added operation."""

PAIRING_STATES: tuple[str, ...] = (PAIRED, UNFINISHED, NOT_PREDICTED)

RELATIVE_ERROR_FLOOR = 0.05
"""Minutes below which a predicted time is too small to divide by. A 0.0008
minute prediction corrected to 0.5 is a 62,400% error and says nothing about the
estimator; such records are excluded from relative error, counted, and still
included in the absolute and signed statistics."""

OUTLIER_RATIO = 5.0
"""A correction is reported as an outlier when the final time is more than this
many times the prediction, or less than its reciprocal. Reported separately, not
removed: an order-of-magnitude miss is the most interesting record in the set."""

LEARNING_DISCLAIMER = (
    "No model has been trained. Feedback is currently used for analysis only: nothing here "
    "changes a suggestion rule, a time assumption, a confidence value, a rate set or any "
    "future estimate. These are empirical correction and acceptance rates measured against "
    "what engineers actually chose — not evidence that the system's confidence is calibrated, "
    "and not a claim about which answer was right."
)

SMALL_SAMPLE_TEXT = "{count} observation(s) — not enough evidence for a reliable pattern."

# --------------------------------------------------------------------- models


class SampleGuard(BaseModel):
    """How much evidence a figure rests on, attached to the figure itself.

    Present on every metric that needs a sample so a reader never has to go
    looking for the denominator. `is_actionable` is a floor, not a significance
    test: this project makes no distributional claims.
    """

    sample_count: int = 0
    minimum_required: int = 0
    is_actionable: bool = False
    note: str = ""


class FeedbackRecord(BaseModel):
    """One normalized correction, read from one estimate's own snapshot.

    A flat record on purpose: this is the shape a later learning step would
    consume, and the point of Step 29 is to find out whether it is rich enough
    before anything is fitted to it. Every value is the one that was recorded.
    """

    estimate_id: str | None = None
    estimate_created_at: datetime | None = None
    source: str = ""

    component_id: str | None = None
    component_name: str | None = None
    feature_id: str | None = None
    feature_ids: list[str] = Field(default_factory=list)
    feature_type: str | None = None
    operation_id: str = ""

    # --- what the system said, and what the engineer chose ---
    system_operation: str | None = None
    user_operation: str | None = None
    system_setup_id: str | None = None
    user_setup_id: str | None = None
    system_setup_time: float | None = None
    user_setup_time: float | None = None
    system_cycle_time: float | None = None
    user_cycle_time: float | None = None

    # --- provenance of the prediction ---
    rule_id: str | None = None
    source_type: str = UNKNOWN
    source_type_derived: bool = Field(
        default=False,
        description=(
            "True when the source type came from the plan in the same snapshot rather than "
            "from the feedback row itself — either stated there or inferred from the viewer "
            "linkage, because the record predates the recorded field. Flagged rather than "
            "hidden, so a reader can tell a recorded fact from a reconstructed one."
        ),
    )
    feature_confidence: str = UNKNOWN
    operation_confidence: str = UNKNOWN
    time_confidence: str = UNKNOWN
    time_method: str | None = None
    time_status: str | None = None
    time_profile_id: str | None = None
    time_profile_version: str | None = None
    time_profile_name: str | None = None
    rate_set_id: str | None = None
    rate_set_version: str | None = None
    material: str | None = None
    setup_grouping: str | None = None
    setup_operation_count: int = 0

    user_action: str = UNKNOWN
    origin: str = UNKNOWN
    costed: bool = False
    timestamp: datetime | None = None

    # --- whether each evidence field could exist for this record at all ---
    #
    # Determined from the shape of the snapshot it was read out of, not from a
    # name or a guess. A record extracted from an assembly's per-component
    # feedback map can have a component identity; one from a part estimate's
    # flat feedback list cannot, because a standalone part has no components.
    component_identity_applicable: bool = Field(
        default=False,
        description=(
            "True only for a record read from an assembly estimate's per-component feedback. "
            "For a single-part estimate the absence of a component identity is correct, not a "
            "gap."
        ),
    )

    corrections: list[str] = Field(
        default_factory=list,
        description=(
            "Every correction type this record contains. One record can carry several — an "
            "engineer who changes a process usually changes its time too — and each is "
            "counted once, against its own denominator, never twice for one edit."
        ),
    )

    @property
    def was_corrected(self) -> bool:
        return any(item not in (ACCEPTED,) for item in self.corrections)

    @property
    def time_pairing(self) -> str:
        """Whether this record is a usable time observation, and why not if it is not.

        Derived, never stored: both halves are already persisted separately and
        the pairing is a reading of them. An accepted prediction is paired —
        the engineer committed the system's own figure, which is a decision —
        and a corrected time is paired. A prediction nobody committed is
        `unfinished`, which Step 29 requires to stay out of every time
        statistic.
        """
        if self.system_cycle_time is None:
            return NOT_PREDICTED
        if self.user_cycle_time is None:
            return UNFINISHED
        return PAIRED

    @property
    def cycle_error(self) -> float | None:
        """Final minus predicted. Positive means the system under-estimated."""
        if self.system_cycle_time is None or self.user_cycle_time is None:
            return None
        return self.user_cycle_time - self.system_cycle_time


class ClassificationRecord(BaseModel):
    """One make/buy pair, read from an assembly estimate's own snapshot."""

    estimate_id: str | None = None
    estimate_created_at: datetime | None = None
    definition_id: str = ""
    component_name: str = ""
    quantity: int = 0
    system_classification: str | None = None
    user_classification: str | None = None
    final_classification: str | None = None
    changed: bool = False
    reason: str = ""
    system_confidence: str = UNKNOWN
    material: str | None = None
    timestamp: datetime | None = None


class CountedRate(BaseModel):
    """A rate with its numerator, denominator and sample guard together.

    A percentage on its own is the easiest way to mislead in this whole
    application, so the two counts it came from travel with it.
    """

    label: str = ""
    numerator: int = 0
    denominator: int = 0
    rate: float | None = Field(
        default=None, description="Null rather than zero when the denominator is zero."
    )
    excluded: int = Field(
        default=0,
        description=(
            "Records deliberately left out of the denominator, and why is stated in `basis`. "
            "For an acceptance rate these are the operations nobody ever finished: counting "
            "them would make an unreviewed suggestion look like a rejected one."
        ),
    )
    basis: str = Field(
        default="", description="What population the denominator describes, in words."
    )
    guard: SampleGuard = Field(default_factory=SampleGuard)


class TimeErrorStats(BaseModel):
    """Prediction error, reported the several ways it can honestly be reported."""

    samples: int = 0
    system_median: float | None = None
    user_median: float | None = None
    system_mean: float | None = None
    user_mean: float | None = None
    median_ratio: float | None = Field(
        default=None,
        description="user median / system median. Above 1 means the system under-estimates.",
    )
    median_absolute_error: float | None = None
    mean_absolute_error: float | None = None
    median_signed_error: float | None = Field(
        default=None, description="Positive means the system under-estimates."
    )
    median_relative_error: float | None = Field(
        default=None,
        description="Median of |final - predicted| / predicted, over records above the floor.",
    )
    near_zero_excluded: int = Field(
        default=0,
        description=(
            f"Records whose prediction was below {RELATIVE_ERROR_FLOOR} min and so were left "
            "out of the relative-error figures only. Still counted everywhere else."
        ),
    )
    outlier_count: int = 0
    guard: SampleGuard = Field(default_factory=SampleGuard)


class BiasFinding(BaseModel):
    """A directional pattern, or the honest refusal to claim one."""

    group: str
    direction: str = Field(
        default="insufficient_data",
        description="'under_estimates', 'over_estimates', 'no_clear_bias' or 'insufficient_data'.",
    )
    statement: str = ""
    stats: TimeErrorStats = Field(default_factory=TimeErrorStats)


class ConfusionCell(BaseModel):
    """One system → engineer pair, with enough identity to drill into it."""

    system_operation: str
    user_operation: str
    count: int = 0
    changed: bool = False
    example_operation_ids: list[str] = Field(
        default_factory=list, description="A few records behind the count, for a drill-down."
    )


class RulePerformance(BaseModel):
    """How often one suggestion rule's proposal survived review.

    `acceptance` is measured over the proposals somebody decided about.
    `not_finished` is the rest — proposals that never got a committed time,
    usually because the rate set prices no rate for the process — and they are
    reported apart because counting them would make a rule with one correction
    look like a rule engineers reject half the time.
    """

    rule_id: str
    suggestions: int = 0
    accepted: int = 0
    changed: int = 0
    rejected: int = 0
    not_finished: int = 0
    acceptance: CountedRate = Field(default_factory=CountedRate)
    became: dict[str, int] = Field(
        default_factory=dict, description="What the engineer chose instead, when they changed it."
    )


class SetupAnalytics(BaseModel):
    """What happened to the system's fixturing assumption.

    Deliberately phrased as correction *frequency*. The system's grouping being
    changed is not evidence it was wrong — it is evidence a person had
    information the CAD does not carry, which is the whole premise of Step 21's
    low-confidence default.
    """

    reviewed_operations: int = 0
    grouping_accepted: CountedRate = Field(default_factory=CountedRate)
    moved_between_setups: CountedRate = Field(default_factory=CountedRate)
    setup_time_changed: CountedRate = Field(default_factory=CountedRate)
    estimates_with_one_system_setup: int = 0
    estimates_split_into_more: int = 0
    split_rate: CountedRate = Field(default_factory=CountedRate)
    note: str = ""


class GroupedRate(BaseModel):
    """An acceptance rate for one slice of the data, with its own guard."""

    group: str
    reviewed: int = 0
    accepted: int = 0
    corrected: int = 0
    not_finished: int = Field(
        default=0, description="Neither accepted nor corrected — never committed a time."
    )
    acceptance: CountedRate = Field(default_factory=CountedRate)
    time: TimeErrorStats = Field(default_factory=TimeErrorStats)


class ClassificationPair(BaseModel):
    """A make/buy correction, counted."""

    system_classification: str
    user_classification: str
    count: int = 0
    examples: list[str] = Field(default_factory=list, description="Component names, for a drill-down.")


class ClassificationAnalytics(BaseModel):
    reviewed: int = 0
    changed: CountedRate = Field(default_factory=CountedRate)
    pairs: list[ClassificationPair] = Field(default_factory=list)
    note: str = ""


class EvidenceField(BaseModel):
    """How completely one evidence field is captured, and why it is not.

    The four counts are exhaustive and sum to `total`. `percent_known` divides
    by the records where the value *could* exist — `total` minus
    `not_applicable` — because dividing by all of them is how Step 29's report
    came to describe 449 single-part records as a component-identity gap.
    """

    field: str
    label: str
    total: int = 0
    known: int = 0
    not_applicable: int = 0
    unrecorded: int = 0
    unrecoverable: int = 0
    applicable: int = Field(
        default=0, description="total - not_applicable. The denominator of `percent_known`."
    )
    percent_known: float | None = Field(
        default=None,
        description="known / applicable, as a fraction. Null when nothing is applicable.",
    )
    basis: str = Field(default="", description="What the denominator describes, in words.")
    source_of_truth: str = Field(
        default="", description="Where the value legitimately comes from."
    )
    recoverable: bool = Field(
        default=False,
        description=(
            "Whether anything persisted could still fill the gaps. False means the remaining "
            "unknowns are permanent — reported as a limitation, not a backlog."
        ),
    )
    guard: SampleGuard = Field(default_factory=SampleGuard)
    note: str = ""


class DatasetReadiness(BaseModel):
    """Whether the recorded corrections would make a usable training set.

    Reporting only. The counts say what a later step would have to work with,
    and the gaps say what is missing before it could.
    """

    total_records: int = 0
    with_system_prediction_and_user_outcome: int = 0
    with_usable_time_correction: int = 0
    with_usable_operation_correction: int = 0
    with_component_identity: int = 0
    with_feature_identity: int = 0
    with_rule_id: int = 0
    with_known_source_type: int = 0
    with_known_feature_confidence: int = 0
    with_time_profile_version: int = 0

    potential_training_rows: int = Field(
        default=0,
        description=(
            "Records carrying a system prediction, a user outcome and a feature identity — the "
            "minimum a supervised row would need. Not a claim that a model should be fitted."
        ),
    )
    insufficient_rows: int = 0
    missing_fields: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "How many records lack each field where it could have existed. Records the field "
            "does not apply to are excluded — a standalone part is not missing a component "
            "identity. The `evidence` list below carries the full breakdown."
        ),
    )
    evidence: list[EvidenceField] = Field(
        default_factory=list,
        description=(
            "Per-field completeness: known, not applicable, unrecorded, unrecoverable, and "
            "whether the remaining gap can ever be closed."
        ),
    )
    permanently_unknown: int = Field(
        default=0,
        description=(
            "Field values that were never captured and that nothing stored can recover. A "
            "limitation of the historical data, not work outstanding."
        ),
    )
    safe_backfill_available: int = Field(
        default=0,
        description=(
            "Values a deterministic recovery from persisted data could still fill. Reported so "
            "the difference between 'not captured' and 'not recoverable' is visible."
        ),
    )
    note: str = ""


class FeedbackFilters(BaseModel):
    """What slice of the history a report covers. Echoed back on every response."""

    component: str | None = None
    operation_type: str | None = None
    feature_type: str | None = None
    material: str | None = None
    source_type: str | None = None
    confidence: str | None = None
    time_profile_version: str | None = None
    rate_set_version: str | None = None
    rule_id: str | None = None
    corrected_only: bool = False


class FeedbackWindow(BaseModel):
    """How much history was read, and whether that was all of it."""

    estimates_read: int = 0
    estimates_available: int = 0
    truncated: bool = False
    note: str = ""


class FeedbackSummary(BaseModel):
    """The headline figures, every one carrying its own sample guard."""

    window: FeedbackWindow = Field(default_factory=FeedbackWindow)
    filters: FeedbackFilters = Field(default_factory=FeedbackFilters)
    minimum_sample: int = 0

    reviewed_operations: int = 0
    not_finished: int = Field(
        default=0,
        description=(
            "Operations nobody committed a time for. Neither accepted nor corrected — never "
            "finished — and excluded from the acceptance denominator rather than counted as "
            "rejections."
        ),
    )
    correction_counts: dict[str, int] = Field(default_factory=dict)

    acceptance: CountedRate = Field(default_factory=CountedRate)
    operation_correction: CountedRate = Field(default_factory=CountedRate)
    time_correction: CountedRate = Field(default_factory=CountedRate)
    setup_correction: CountedRate = Field(default_factory=CountedRate)
    classification_correction: CountedRate = Field(default_factory=CountedRate)

    cycle_time: TimeErrorStats = Field(default_factory=TimeErrorStats)
    readiness: DatasetReadiness = Field(default_factory=DatasetReadiness)
    disclaimer: str = LEARNING_DISCLAIMER


class OperationAnalytics(BaseModel):
    """The confusion matrix and per-rule performance."""

    window: FeedbackWindow = Field(default_factory=FeedbackWindow)
    filters: FeedbackFilters = Field(default_factory=FeedbackFilters)
    minimum_sample: int = 0
    reviewed: int = 0
    accuracy: CountedRate = Field(default_factory=CountedRate)
    confusion: list[ConfusionCell] = Field(default_factory=list)
    rules: list[RulePerformance] = Field(default_factory=list)
    disclaimer: str = LEARNING_DISCLAIMER


class TimeOutlier(BaseModel):
    """One correction extreme enough to be reported on its own."""

    estimate_id: str | None = None
    operation_id: str = ""
    component_name: str | None = None
    feature_id: str | None = None
    operation: str | None = None
    system_cycle_time: float | None = None
    user_cycle_time: float | None = None
    ratio: float | None = None


class TimeAnalytics(BaseModel):
    """Calibration per process, with outliers kept out of the headline."""

    window: FeedbackWindow = Field(default_factory=FeedbackWindow)
    filters: FeedbackFilters = Field(default_factory=FeedbackFilters)
    minimum_sample: int = 0
    overall: TimeErrorStats = Field(default_factory=TimeErrorStats)
    by_operation: list[BiasFinding] = Field(default_factory=list)
    by_time_profile: list[GroupedRate] = Field(default_factory=list)
    outliers: list[TimeOutlier] = Field(default_factory=list)
    outlier_note: str = ""
    disclaimer: str = LEARNING_DISCLAIMER


class ConfidenceAnalytics(BaseModel):
    """Empirical acceptance by confidence and by evidence type."""

    window: FeedbackWindow = Field(default_factory=FeedbackWindow)
    filters: FeedbackFilters = Field(default_factory=FeedbackFilters)
    minimum_sample: int = 0
    by_operation_confidence: list[GroupedRate] = Field(default_factory=list)
    by_feature_confidence: list[GroupedRate] = Field(default_factory=list)
    by_time_confidence: list[GroupedRate] = Field(default_factory=list)
    by_source_type: list[GroupedRate] = Field(default_factory=list)
    calibration_note: str = ""
    disclaimer: str = LEARNING_DISCLAIMER


class RecordPage(BaseModel):
    """One page of normalized records, for drilling into a statistic."""

    window: FeedbackWindow = Field(default_factory=FeedbackWindow)
    filters: FeedbackFilters = Field(default_factory=FeedbackFilters)
    total: int = 0
    limit: int = 50
    offset: int = 0
    items: list[FeedbackRecord] = Field(default_factory=list)
    disclaimer: str = LEARNING_DISCLAIMER


SETUP_NOTE = (
    "These are correction frequencies, not error rates. The system deliberately assumes one "
    "setup at low confidence because nothing in the CAD proves a second fixturing is needed, so "
    "an engineer splitting it is a person supplying information the geometry does not carry — "
    "not the system being wrong."
)

CLASSIFICATION_NOTE = (
    "No CAD format states make/buy, so the system's suggestion is a low-confidence reading of "
    "whether a component carries measurable geometry. A change here records what the engineer "
    "chose; it is not evidence about what the component truly is beyond that choice."
)

CALIBRATION_NOTE = (
    "These are empirical acceptance rates by confidence band, not evidence that the confidence "
    "values are calibrated. Confidence in this application is a three-way classification of "
    "which rule branches fired, never a probability, so a higher acceptance rate at 'high' is a "
    "measured association and nothing more."
)

READINESS_NOTE = (
    "An evidence-quality report, not a training system. These counts say what a later learning "
    "step would have to work with and what it would be missing; more evidence existing is not "
    "the same as a model being ready, and no model has been trained or is being prepared. A "
    "field a record cannot have — a component identity on a standalone part, a paired time on "
    "an operation nobody finished — is reported as not applicable rather than as a gap, and an "
    "unknown is never converted into a guess to improve a number."
)

OUTLIER_NOTE = (
    f"A correction where the final time is more than {OUTLIER_RATIO:g}x the prediction, or less "
    f"than 1/{OUTLIER_RATIO:g} of it. Listed separately and still included in every count above: "
    "the medians are robust to them by construction, and deleting an order-of-magnitude miss "
    "would remove the most informative record in the set."
)


# --------------------------------------------------------------------- reading


def _guard(count: int, minimum: int) -> SampleGuard:
    actionable = count >= minimum
    return SampleGuard(
        sample_count=count,
        minimum_required=minimum,
        is_actionable=actionable,
        note="" if actionable else SMALL_SAMPLE_TEXT.format(count=count),
    )


def _rate(
    label: str,
    numerator: int,
    denominator: int,
    minimum: int,
    *,
    excluded: int = 0,
    basis: str = "",
) -> CountedRate:
    return CountedRate(
        label=label,
        numerator=numerator,
        denominator=denominator,
        rate=(numerator / denominator) if denominator else None,
        excluded=excluded,
        basis=basis,
        guard=_guard(denominator, minimum),
    )


RESOLVED_BASIS = (
    "the operations somebody actually decided about — accepted, changed or rejected. Operations "
    "nobody committed a time for were never finished and are excluded rather than counted as "
    "rejections."
)


def _resolved(records: list[FeedbackRecord]) -> tuple[int, int, int]:
    """Accepted, resolved and never-finished counts for a set of records.

    "Resolved" is the population an acceptance rate is about. An operation with
    no committed time and no edit was not reviewed at all — on this project's own
    history that is over a quarter of every suggestion, usually because the rate
    set prices no rate for the process proposed — and putting it in the
    denominator turns an unreviewed suggestion into a rejected one.
    """
    accepted = sum(1 for record in records if ACCEPTED in record.corrections)
    resolved = sum(1 for record in records if record.corrections)
    return accepted, resolved, len(records) - resolved


def _acceptance(label: str, records: list[FeedbackRecord], minimum: int) -> CountedRate:
    accepted, resolved, unfinished = _resolved(records)
    return _rate(
        label, accepted, resolved, minimum, excluded=unfinished, basis=RESOLVED_BASIS
    )


def _text(value) -> str:
    """A recorded enum value as a plain string, or `unknown`.

    Snapshots hold enum *values* because they were serialized with
    `mode="json"`, but a record written by an older path may hold the name or
    nothing at all. Anything unusable becomes `unknown` rather than a guess.
    """
    if value is None:
        return UNKNOWN
    if isinstance(value, str):
        return value or UNKNOWN
    return str(getattr(value, "value", value))


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _corrections_of(row: dict) -> list[str]:
    """Every correction this record carries, each counted once.

    An engineer who changes a process almost always changes its time too, so the
    types are a *set* on one record rather than a total: counting "two
    corrections" for one edit would inflate every rate in the report. The
    booleans are the ones the feedback record precomputed when it was written,
    so what is counted here is what was recorded then rather than a re-derivation
    that could disagree with it.
    """
    found: list[str] = []
    action = _text(row.get("user_action"))

    if row.get("operation_changed"):
        found.append(OPERATION_CHANGED)
    if row.get("setup_changed"):
        found.append(SETUP_CHANGED)
    if row.get("setup_time_corrected") or row.get("setup_time_changed"):
        found.append(SETUP_TIME_CHANGED)
    if row.get("cycle_time_changed"):
        found.append(CYCLE_TIME_CHANGED)

    if action == "rejected":
        found.append(REJECTED)
        found.append(REMOVED)
    elif action == "added" or _text(row.get("origin")) == "user":
        found.append(ADDED)
    elif not found and action in ("accepted", "edited", "pending"):
        # Nothing was changed about it. Only an operation that actually reached
        # the cost counts as accepted: one still needing a time was neither
        # accepted nor corrected, it was simply never finished.
        if row.get("costed"):
            found.append(ACCEPTED)
    return found


def _source_type_of(row: dict) -> tuple[str, bool]:
    """The evidence type, and whether it had to be derived.

    Records written before `source_type` existed still carry the viewer linkage,
    which answers the same question: a B-Rep recognizer reports faces, a faceted
    one reports facets. The derivation is flagged so a reader can tell a recorded
    fact from a reconstructed one.
    """
    recorded = row.get("source_type")
    if recorded:
        return str(recorded), False
    if row.get("source_facets"):
        return "faceted", True
    if row.get("source_faces"):
        return "brep", True
    return UNKNOWN, False


def normalize_operation_feedback(
    row: dict,
    *,
    estimate_id: str | None = None,
    created_at: datetime | None = None,
    source: str = "",
    context: dict | None = None,
) -> FeedbackRecord:
    """One persisted feedback row as a normalized record.

    `context` carries the facts that belong to the estimate rather than the
    operation — the rate set version, the material — so a filter can reach them
    without the analytics layer having to look anything up.
    """
    context = context or {}
    source_type, derived = _source_type_of(row)

    return FeedbackRecord(
        estimate_id=row.get("estimate_id") or estimate_id,
        estimate_created_at=created_at,
        source=source,
        component_id=row.get("component_id"),
        component_name=row.get("component_name"),
        feature_id=row.get("feature_id"),
        feature_ids=list(row.get("feature_ids") or []),
        feature_type=row.get("feature_type"),
        operation_id=row.get("operation_id") or "",
        system_operation=row.get("suggested_operation"),
        user_operation=row.get("final_operation"),
        system_setup_id=row.get("system_setup_id"),
        user_setup_id=row.get("final_setup_id"),
        system_setup_time=_number(row.get("system_setup_time_minutes")),
        user_setup_time=_number(row.get("final_setup_time_minutes")),
        system_cycle_time=_number(row.get("suggested_cycle_time")),
        user_cycle_time=_number(row.get("final_cycle_time")),
        rule_id=row.get("rule_id"),
        source_type=source_type,
        source_type_derived=derived,
        feature_confidence=_text(row.get("feature_confidence")),
        operation_confidence=_text(row.get("suggestion_confidence")),
        time_confidence=_text(row.get("time_confidence")),
        time_method=row.get("time_method"),
        time_status=_text(row.get("time_status")) if row.get("time_status") else None,
        time_profile_id=row.get("time_profile_id"),
        time_profile_version=row.get("time_profile_version"),
        time_profile_name=row.get("time_profile_name"),
        rate_set_id=context.get("rate_set_id"),
        rate_set_version=context.get("rate_set_version"),
        material=context.get("material"),
        setup_grouping=row.get("system_setup_grouping"),
        setup_operation_count=int(row.get("setup_operation_count") or 0),
        user_action=_text(row.get("user_action")),
        origin=_text(row.get("origin")),
        costed=bool(row.get("costed")),
        timestamp=row.get("timestamp"),
        corrections=_corrections_of(row),
    )


def normalize_classification_feedback(
    row: dict,
    *,
    estimate_id: str | None = None,
    created_at: datetime | None = None,
    context: dict | None = None,
) -> ClassificationRecord:
    context = context or {}
    return ClassificationRecord(
        estimate_id=row.get("estimate_id") or estimate_id,
        estimate_created_at=created_at,
        definition_id=row.get("definition_id") or "",
        component_name=row.get("component_name") or "",
        quantity=int(row.get("quantity") or 0),
        system_classification=row.get("system_classification"),
        user_classification=row.get("user_classification"),
        final_classification=row.get("final_classification"),
        changed=bool(row.get("classification_changed")),
        reason=row.get("reason") or "",
        system_confidence=_text(row.get("system_confidence")),
        material=row.get("material") or context.get("material"),
        timestamp=row.get("timestamp"),
    )


def _plan_operations(plan: dict | None) -> dict[str, dict]:
    """A plan's operations, keyed by id, for joining a feedback row to its own.

    The plan travels in the same snapshot as the feedback rows it produced, and
    it carries three things the feedback record did not until Step 29: which
    rule fired, what kind of geometry the evidence came from, and the feature
    type. Joining by `operation_id` recovers all three for every correction ever
    recorded, rather than leaving the whole of history reported as `unknown`
    because a field was added late.

    The join never overrides a recorded value — it only fills an absent one —
    and anything it fills is flagged, so a reader can tell a fact the feedback
    row carried from one reconstructed from the plan beside it.
    """
    if not isinstance(plan, dict):
        return {}
    return {
        operation.get("operation_id"): operation
        for operation in plan.get("operations") or []
        if operation.get("operation_id")
    }


def _plans_in(plan_snapshot: dict) -> dict[str, dict]:
    """Every operation of every plan in one snapshot, keyed by operation id.

    Both shapes: the single plan a part estimate stores, and the per-component
    plans an assembly estimate stores since Step 28. Ids are namespaced by
    component from Step 28 onward, so one flat mapping cannot collide.
    """
    operations = _plan_operations(plan_snapshot.get("plan"))
    assembly = plan_snapshot.get("assembly_plan") or {}
    for entry in assembly.get("component_plans") or []:
        operations.update(_plan_operations(entry.get("plan")))
    return operations


def _enrich(record: FeedbackRecord, operation: dict | None) -> FeedbackRecord:
    """Fill what the feedback row lacks from the plan it came from.

    Only absent fields, and every fill is flagged. `source_type` is derived from
    the viewer linkage when the plan does not state it either: a B-Rep
    recognizer reports faces and a faceted one reports facets, which answers the
    same question — and a test proves the derivation agrees with what the
    recognizers actually report on both a real B-Rep and a real faceted model.
    """
    if not operation:
        return record

    updates: dict = {}
    if record.rule_id is None and operation.get("system_suggestion_rule"):
        updates["rule_id"] = operation["system_suggestion_rule"]
    if record.feature_type is None and operation.get("feature_type"):
        updates["feature_type"] = operation["feature_type"]
    if record.feature_confidence == UNKNOWN and operation.get("system_feature_confidence"):
        updates["feature_confidence"] = _text(operation["system_feature_confidence"])

    if record.source_type == UNKNOWN:
        source_type, derived = _source_type_of(operation)
        if source_type != UNKNOWN:
            updates["source_type"] = source_type
            updates["source_type_derived"] = derived or True

    return record.model_copy(update=updates) if updates else record


def extract_records(estimate) -> tuple[list[FeedbackRecord], list[ClassificationRecord]]:
    """Every correction one stored estimate recorded.

    `estimate` is duck-typed: anything exposing `estimate_id`, `created_at`,
    `source`, `request_snapshot`, `result_snapshot`, `plan_snapshot` and
    `rate_set_snapshot`. Both plan-snapshot shapes are read — the single plan a
    part estimate stores, and the per-component plans an assembly estimate
    stores since Step 28 — because a second source of truth for the same
    corrections is exactly what this layer must not create.
    """
    plan_snapshot = getattr(estimate, "plan_snapshot", None) or {}
    result = getattr(estimate, "result_snapshot", None) or {}
    request = getattr(estimate, "request_snapshot", None) or {}
    rate_set = getattr(estimate, "rate_set_snapshot", None) or {}

    estimate_id = getattr(estimate, "estimate_id", None)
    created_at = getattr(estimate, "created_at", None)
    source = getattr(estimate, "source", "") or ""
    context = {
        "rate_set_id": rate_set.get("rate_set_id") or request.get("rate_set_id"),
        "rate_set_version": rate_set.get("version") or request.get("rate_set_version"),
        "material": request.get("material") or getattr(estimate, "material", None),
    }

    operations: list[FeedbackRecord] = []
    # The plans in the same snapshot, so a feedback row can be joined to the
    # operation it came from and recover the rule, the evidence type and the
    # feature type that the feedback record itself did not carry until Step 29.
    planned = _plans_in(plan_snapshot)

    # A part estimate: one plan, one flat feedback list.
    for row in plan_snapshot.get("feedback") or []:
        operations.append(
            _enrich(
                normalize_operation_feedback(
                    row,
                    estimate_id=estimate_id,
                    created_at=created_at,
                    source=source,
                    context=context,
                ),
                planned.get(row.get("operation_id")),
            )
        )

    # An assembly estimate: one feedback list per component definition.
    per_component = plan_snapshot.get("component_feedback") or {}
    if isinstance(per_component, dict):
        for definition_id, rows in per_component.items():
            for row in rows or []:
                record = _enrich(
                    normalize_operation_feedback(
                        row,
                        estimate_id=estimate_id,
                        created_at=created_at,
                        source=source,
                        context=context,
                    ),
                    planned.get(row.get("operation_id")),
                )
                # This record came out of an assembly's per-component feedback,
                # so a component identity is a thing it can have — and the map
                # is keyed by definition, which is a deterministic recovery
                # rather than a guess. The key is a *definition* id, never an
                # occurrence id: an assembly places one definition many times,
                # and conflating the two would attribute twelve bolts' worth of
                # corrections to one placement.
                updates: dict = {"component_identity_applicable": True}
                if record.component_id is None:
                    updates["component_id"] = definition_id
                operations.append(record.model_copy(update=updates))

    classifications = [
        normalize_classification_feedback(
            row, estimate_id=estimate_id, created_at=created_at, context=context
        )
        for row in result.get("classification_feedback") or []
    ]
    return operations, classifications


# --------------------------------------------------------------------- filtering


def matches(record: FeedbackRecord, filters: FeedbackFilters) -> bool:
    """Whether one record belongs in this slice. Case-insensitive on names."""

    def same(value, wanted) -> bool:
        if wanted is None:
            return True
        return value is not None and str(value).lower() == str(wanted).lower()

    if not same(record.component_name, filters.component) and not same(
        record.component_id, filters.component
    ):
        return False
    if filters.operation_type is not None:
        chosen = record.user_operation or record.system_operation
        if not (
            same(chosen, filters.operation_type)
            or same(record.system_operation, filters.operation_type)
        ):
            return False
    if not same(record.feature_type, filters.feature_type):
        return False
    if not same(record.material, filters.material):
        return False
    if not same(record.source_type, filters.source_type):
        return False
    if filters.confidence is not None and not (
        same(record.operation_confidence, filters.confidence)
        or same(record.time_confidence, filters.confidence)
        or same(record.feature_confidence, filters.confidence)
    ):
        return False
    if not same(record.time_profile_version, filters.time_profile_version):
        return False
    if not same(record.rate_set_version, filters.rate_set_version):
        return False
    if not same(record.rule_id, filters.rule_id):
        return False
    if filters.corrected_only and not record.was_corrected:
        return False
    return True


# --------------------------------------------------------------------- statistics


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 6) if values else None


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def time_stats(records: list[FeedbackRecord], minimum: int) -> TimeErrorStats:
    """Prediction error for a set of records, several honest ways at once.

    Only records carrying *both* a prediction and a decision are included: an
    operation the system never predicted a time for was not corrected, it was
    entered, and counting it would make the estimator look wrong far more often
    than it was.
    """
    pairs = [
        (record.system_cycle_time, record.user_cycle_time)
        for record in records
        if record.system_cycle_time is not None and record.user_cycle_time is not None
    ]
    if not pairs:
        return TimeErrorStats(guard=_guard(0, minimum))

    system = [pair[0] for pair in pairs]
    user = [pair[1] for pair in pairs]
    signed = [final - predicted for predicted, final in pairs]
    absolute = [abs(value) for value in signed]

    relative = [
        abs(final - predicted) / predicted
        for predicted, final in pairs
        if predicted >= RELATIVE_ERROR_FLOOR
    ]
    near_zero = sum(1 for predicted, _ in pairs if predicted < RELATIVE_ERROR_FLOOR)

    outliers = sum(
        1
        for predicted, final in pairs
        if predicted > 0 and (final / predicted >= OUTLIER_RATIO or final / predicted <= 1 / OUTLIER_RATIO)
    )

    system_median = _median(system)
    user_median = _median(user)
    return TimeErrorStats(
        samples=len(pairs),
        system_median=system_median,
        user_median=user_median,
        system_mean=_mean(system),
        user_mean=_mean(user),
        median_ratio=(
            round(user_median / system_median, 6)
            if system_median and system_median >= RELATIVE_ERROR_FLOOR
            else None
        ),
        median_absolute_error=_median(absolute),
        mean_absolute_error=_mean(absolute),
        median_signed_error=_median(signed),
        median_relative_error=_median(relative),
        near_zero_excluded=near_zero,
        outlier_count=outliers,
        guard=_guard(len(pairs), minimum),
    )


def _bias(group: str, records: list[FeedbackRecord], minimum: int) -> BiasFinding:
    """A directional claim, made only where there is enough to claim it."""
    stats = time_stats(records, minimum)
    if not stats.guard.is_actionable:
        return BiasFinding(
            group=group,
            direction="insufficient_data",
            statement=SMALL_SAMPLE_TEXT.format(count=stats.samples),
            stats=stats,
        )

    signed = stats.median_signed_error or 0.0
    # A median correction smaller than the relative-error floor is not a
    # direction, it is arithmetic noise on a two-decimal minute.
    if abs(signed) < RELATIVE_ERROR_FLOOR:
        return BiasFinding(
            group=group,
            direction="no_clear_bias",
            statement=(
                f"{stats.samples} observations, median correction "
                f"{signed:+.2f} min — no consistent direction."
            ),
            stats=stats,
        )
    direction = "under_estimates" if signed > 0 else "over_estimates"
    word = "under-estimates" if signed > 0 else "over-estimates"
    return BiasFinding(
        group=group,
        direction=direction,
        statement=(
            f"{stats.samples} observations. System median {stats.system_median:.2f} min, "
            f"engineer median {stats.user_median:.2f} min — the system {word} by a median of "
            f"{abs(signed):.2f} min."
        ),
        stats=stats,
    )


def _grouped(
    group: str, records: list[FeedbackRecord], minimum: int, *, with_time: bool = True
) -> GroupedRate:
    accepted, resolved, unfinished = _resolved(records)
    return GroupedRate(
        group=group,
        reviewed=len(records),
        accepted=accepted,
        corrected=sum(1 for record in records if record.was_corrected),
        not_finished=unfinished,
        acceptance=_acceptance("acceptance", records, minimum),
        time=time_stats(records, minimum) if with_time else TimeErrorStats(),
    )


def _by(records: list[FeedbackRecord], key) -> dict[str, list[FeedbackRecord]]:
    buckets: dict[str, list[FeedbackRecord]] = {}
    for record in records:
        value = key(record)
        buckets.setdefault(UNKNOWN if value in (None, "") else str(value), []).append(record)
    return buckets


# --------------------------------------------------------------------- reports


def summarise(
    records: list[FeedbackRecord],
    classifications: list[ClassificationRecord],
    *,
    minimum: int,
    window: FeedbackWindow | None = None,
    filters: FeedbackFilters | None = None,
) -> FeedbackSummary:
    """The headline figures. Every rate carries its own denominator and guard."""
    reviewed = len(records)
    counts = {name: 0 for name in CORRECTION_TYPES}
    for record in records:
        for correction in record.corrections:
            counts[correction] += 1

    # Denominators are chosen so a rate answers a real question. Operation and
    # time corrections are only meaningful over the records where the system
    # actually predicted something to correct.
    predicted_operation = sum(1 for r in records if r.system_operation is not None)
    predicted_time = sum(
        1 for r in records if r.system_cycle_time is not None and r.user_cycle_time is not None
    )

    return FeedbackSummary(
        window=window or FeedbackWindow(),
        filters=filters or FeedbackFilters(),
        minimum_sample=minimum,
        reviewed_operations=reviewed,
        correction_counts=counts,
        acceptance=_acceptance("accepted without change", records, minimum),
        not_finished=len(records) - sum(1 for record in records if record.corrections),
        operation_correction=_rate(
            "process changed", counts[OPERATION_CHANGED], predicted_operation, minimum
        ),
        time_correction=_rate(
            "cycle time changed", counts[CYCLE_TIME_CHANGED], predicted_time, minimum
        ),
        setup_correction=_rate(
            "moved between setups", counts[SETUP_CHANGED], reviewed, minimum
        ),
        classification_correction=_rate(
            "classification changed",
            sum(1 for entry in classifications if entry.changed),
            len(classifications),
            minimum,
        ),
        cycle_time=time_stats(records, minimum),
        readiness=readiness(records, minimum),
    )


def operation_report(
    records: list[FeedbackRecord],
    *,
    minimum: int,
    window: FeedbackWindow | None = None,
    filters: FeedbackFilters | None = None,
    examples: int = 5,
) -> OperationAnalytics:
    """The confusion matrix, and how each rule's proposals fared."""
    cells: dict[tuple[str, str], ConfusionCell] = {}
    for record in records:
        if record.system_operation is None or record.user_operation is None:
            continue
        key = (record.system_operation, record.user_operation)
        cell = cells.get(key)
        if cell is None:
            cell = ConfusionCell(
                system_operation=key[0],
                user_operation=key[1],
                changed=key[0] != key[1],
            )
            cells[key] = cell
        cell.count += 1
        if len(cell.example_operation_ids) < examples:
            cell.example_operation_ids.append(record.operation_id)

    matrix = sorted(
        cells.values(), key=lambda cell: (-cell.count, cell.system_operation, cell.user_operation)
    )
    matched = sum(cell.count for cell in matrix if not cell.changed)
    total = sum(cell.count for cell in matrix)

    rules: list[RulePerformance] = []
    for rule_id, group in sorted(_by(records, lambda r: r.rule_id).items()):
        if rule_id == UNKNOWN:
            continue
        accepted = sum(1 for r in group if ACCEPTED in r.corrections)
        changed = sum(1 for r in group if OPERATION_CHANGED in r.corrections)
        rejected = sum(1 for r in group if REJECTED in r.corrections)
        became: dict[str, int] = {}
        for record in group:
            if OPERATION_CHANGED in record.corrections and record.user_operation:
                became[record.user_operation] = became.get(record.user_operation, 0) + 1
        _, resolved, unfinished = _resolved(group)
        rules.append(
            RulePerformance(
                rule_id=rule_id,
                suggestions=len(group),
                accepted=accepted,
                changed=changed,
                rejected=rejected,
                not_finished=unfinished,
                acceptance=_acceptance("accepted without change", group, minimum),
                became=dict(sorted(became.items())),
            )
        )

    return OperationAnalytics(
        window=window or FeedbackWindow(),
        filters=filters or FeedbackFilters(),
        minimum_sample=minimum,
        reviewed=total,
        accuracy=_rate("engineer kept the suggested process", matched, total, minimum),
        confusion=matrix,
        rules=sorted(rules, key=lambda rule: (-rule.suggestions, rule.rule_id)),
    )


def time_report(
    records: list[FeedbackRecord],
    *,
    minimum: int,
    window: FeedbackWindow | None = None,
    filters: FeedbackFilters | None = None,
    outlier_limit: int = 25,
) -> TimeAnalytics:
    """Calibration per process and per profile version, outliers listed apart."""
    by_operation = [
        _bias(name, group, minimum)
        for name, group in sorted(
            _by(records, lambda r: r.user_operation or r.system_operation).items()
        )
        if name != UNKNOWN
    ]

    by_profile = [
        _grouped(version, group, minimum)
        for version, group in sorted(_by(records, lambda r: r.time_profile_version).items())
    ]

    outliers: list[TimeOutlier] = []
    for record in records:
        predicted, final = record.system_cycle_time, record.user_cycle_time
        if predicted is None or final is None or predicted <= 0:
            continue
        ratio = final / predicted
        if ratio >= OUTLIER_RATIO or ratio <= 1 / OUTLIER_RATIO:
            outliers.append(
                TimeOutlier(
                    estimate_id=record.estimate_id,
                    operation_id=record.operation_id,
                    component_name=record.component_name,
                    feature_id=record.feature_id,
                    operation=record.user_operation or record.system_operation,
                    system_cycle_time=predicted,
                    user_cycle_time=final,
                    ratio=round(ratio, 6),
                )
            )
    outliers.sort(key=lambda item: -(item.ratio or 0.0))

    return TimeAnalytics(
        window=window or FeedbackWindow(),
        filters=filters or FeedbackFilters(),
        minimum_sample=minimum,
        overall=time_stats(records, minimum),
        by_operation=sorted(by_operation, key=lambda finding: -finding.stats.samples),
        by_time_profile=sorted(by_profile, key=lambda entry: -entry.reviewed),
        outliers=outliers[:outlier_limit],
        outlier_note=OUTLIER_NOTE,
    )


def setup_report(
    records: list[FeedbackRecord],
    *,
    minimum: int,
    window: FeedbackWindow | None = None,
    filters: FeedbackFilters | None = None,
) -> SetupAnalytics:
    """What happened to the system's fixturing assumption, as frequencies."""
    reviewed = len(records)
    moved = sum(1 for record in records if SETUP_CHANGED in record.corrections)
    retimed = sum(1 for record in records if SETUP_TIME_CHANGED in record.corrections)
    accepted_grouping = sum(
        1
        for record in records
        if SETUP_CHANGED not in record.corrections and record.system_setup_id is not None
    )
    grouped_denominator = sum(1 for record in records if record.system_setup_id is not None)

    # How often one assumed setup became several. Counted per estimate, because
    # "split into more setups" is a property of a plan and not of an operation.
    by_estimate: dict[str, tuple[set[str], set[str]]] = {}
    for record in records:
        if record.estimate_id is None:
            continue
        system, final = by_estimate.setdefault(record.estimate_id, (set(), set()))
        if record.system_setup_id:
            system.add(record.system_setup_id)
        if record.user_setup_id:
            final.add(record.user_setup_id)

    one_setup = 0
    split = 0
    for system, final in by_estimate.values():
        if len(system) == 1:
            one_setup += 1
            if len(final) > 1:
                split += 1

    return SetupAnalytics(
        reviewed_operations=reviewed,
        grouping_accepted=_rate(
            "left in the setup the system chose", accepted_grouping, grouped_denominator, minimum
        ),
        moved_between_setups=_rate("moved to another setup", moved, reviewed, minimum),
        setup_time_changed=_rate("setup time changed", retimed, reviewed, minimum),
        estimates_with_one_system_setup=one_setup,
        estimates_split_into_more=split,
        split_rate=_rate("one assumed setup became several", split, one_setup, minimum),
        note=SETUP_NOTE,
    )


def classification_report(
    classifications: list[ClassificationRecord], *, minimum: int, examples: int = 5
) -> ClassificationAnalytics:
    """Which make/buy suggestions engineers changed, and to what."""
    pairs: dict[tuple[str, str], ClassificationPair] = {}
    for entry in classifications:
        if not entry.system_classification or not entry.final_classification:
            continue
        key = (entry.system_classification, entry.final_classification)
        pair = pairs.get(key)
        if pair is None:
            pair = ClassificationPair(system_classification=key[0], user_classification=key[1])
            pairs[key] = pair
        pair.count += 1
        if entry.component_name and len(pair.examples) < examples:
            pair.examples.append(entry.component_name)

    changed = sum(1 for entry in classifications if entry.changed)
    return ClassificationAnalytics(
        reviewed=len(classifications),
        changed=_rate(
            "engineer chose differently", changed, len(classifications), minimum
        ),
        pairs=sorted(
            pairs.values(),
            key=lambda pair: (-pair.count, pair.system_classification, pair.user_classification),
        ),
        note=CLASSIFICATION_NOTE,
    )


def confidence_report(
    records: list[FeedbackRecord],
    *,
    minimum: int,
    window: FeedbackWindow | None = None,
    filters: FeedbackFilters | None = None,
) -> ConfidenceAnalytics:
    """Empirical acceptance by confidence band and by evidence type."""

    def bands(key) -> list[GroupedRate]:
        order = {"high": 0, "medium": 1, "low": 2, UNKNOWN: 3}
        groups = [_grouped(name, group, minimum) for name, group in _by(records, key).items()]
        return sorted(groups, key=lambda entry: (order.get(entry.group, 9), entry.group))

    return ConfidenceAnalytics(
        window=window or FeedbackWindow(),
        filters=filters or FeedbackFilters(),
        minimum_sample=minimum,
        by_operation_confidence=bands(lambda r: r.operation_confidence),
        by_feature_confidence=bands(lambda r: r.feature_confidence),
        by_time_confidence=bands(lambda r: r.time_confidence),
        by_source_type=sorted(
            (
                _grouped(name, group, minimum)
                for name, group in _by(records, lambda r: r.source_type).items()
            ),
            key=lambda entry: (-entry.reviewed, entry.group),
        ),
        calibration_note=CALIBRATION_NOTE,
    )


def _classify_feature_confidence(record: FeedbackRecord) -> str:
    """Feature confidence: known, not applicable, or gone for good.

    The recognizer genuinely exposes it — `OperationSuggestion.feature_confidence`
    has existed since Step 19 — and it reaches the plan and the feedback record
    since Step 29. An operation nobody suggested has no feature and no
    confidence, which is `not_applicable`. Anything else absent is
    **unrecoverable**: the CAD file is not stored, no snapshot holds the
    recognizer's output, and re-deriving it is impossible. It is never
    manufactured from acceptance frequency, from rule performance, or from a
    binary rule match.
    """
    if record.feature_confidence != UNKNOWN:
        return EVIDENCE_KNOWN
    if record.system_operation is None:
        return EVIDENCE_NOT_APPLICABLE
    return EVIDENCE_UNRECOVERABLE


def _classify_component_identity(record: FeedbackRecord) -> str:
    """Component identity: known, or not a thing this record can have.

    Measured on the real database: every one of the 264 assembly records
    already carries it, and all 449 without one came from single-part estimates.
    A standalone part has no component definition, so its absence is correct.
    """
    if record.component_id:
        return EVIDENCE_KNOWN
    if not record.component_identity_applicable:
        return EVIDENCE_NOT_APPLICABLE
    # An assembly record with no identity in the row and none in the snapshot
    # key. Recoverable in principle — the plan names its component — so it is
    # `unrecorded` rather than permanent.
    return EVIDENCE_UNRECORDED


def _classify_paired_time(record: FeedbackRecord) -> str:
    """A paired time observation, or the honest reason it is not one.

    An unfinished operation is `not_applicable`, not missing: Step 29 requires
    it to stay out of every time statistic, and a chosen time is never
    manufactured merely because an operation was accepted — an accepted
    prediction is paired because the engineer committed that figure, which the
    plan records as `system_time_accepted`.
    """
    pairing = record.time_pairing
    if pairing == PAIRED:
        return EVIDENCE_KNOWN
    return EVIDENCE_NOT_APPLICABLE


def _classify_time_profile_version(record: FeedbackRecord) -> str:
    """A published profile's version, or the absence of a published profile.

    Measured on the real database: all 205 records with a predicted time and no
    version had no `time_profile_id` either. A plan built without a profile runs
    under the built-in baseline, which has no published version — so the absence
    is correct, not a lost value, and reporting it as permanently unknown
    overstated the gap by 198 records.

    An id recorded *with* no version would be a genuine loss, and is reported as
    one; it does not occur in the current history.
    """
    if record.time_profile_version:
        return EVIDENCE_KNOWN
    if record.system_cycle_time is None or not record.time_profile_id:
        return EVIDENCE_NOT_APPLICABLE
    return EVIDENCE_UNRECOVERABLE


def _classify_simple(value, applicable: bool, recoverable: bool) -> str:
    if value:
        return EVIDENCE_KNOWN
    if not applicable:
        return EVIDENCE_NOT_APPLICABLE
    return EVIDENCE_UNRECORDED if recoverable else EVIDENCE_UNRECOVERABLE


_EVIDENCE_FIELDS = (
    (
        "feature_confidence",
        "Feature-recognition confidence",
        _classify_feature_confidence,
        "OperationSuggestion.feature_confidence, recorded on the plan since Step 29",
        False,
        "the records that came from a system suggestion",
    ),
    (
        "component_identity",
        "Component identity",
        _classify_component_identity,
        "ComponentDefinition.definition_id, via the assembly's per-component feedback",
        True,
        "the records from an assembly estimate, where a component exists",
    ),
    (
        "paired_cycle_time",
        "Paired cycle time",
        _classify_paired_time,
        "the plan's committed time beside the prediction it was compared with",
        False,
        "the records an engineer actually committed a time for",
    ),
    (
        "rule_id",
        "Suggestion rule",
        lambda r: _classify_simple(r.rule_id, r.system_operation is not None, True),
        "OperationSuggestion.rule_id, recovered from the plan beside the feedback",
        True,
        "the records that came from a system suggestion",
    ),
    (
        "source_type",
        "Evidence type (B-Rep or faceted)",
        lambda r: _classify_simple(
            r.source_type if r.source_type != UNKNOWN else None,
            r.system_operation is not None,
            True,
        ),
        "the feature's own source type, or the viewer linkage it implies",
        True,
        "the records that came from a system suggestion",
    ),
    (
        "time_profile_version",
        "Time-profile version",
        _classify_time_profile_version,
        "the published profile a prediction was made under",
        False,
        "the predictions made under a published profile rather than the built-in baseline",
    ),
)

UNRECOVERABLE_NOTE = (
    "{count} of these were never captured and nothing stored can recover them — the CAD file "
    "is not kept, so the recognizer's answer cannot be re-derived. A permanent limitation of "
    "the historical data, not work outstanding, and it is never filled with a guess."
)


def evidence_fields(records: list[FeedbackRecord], minimum: int) -> list[EvidenceField]:
    """Per-field completeness, classified rather than counted.

    Every record lands in exactly one of four states per field, and the four sum
    to the total. That is the whole point: Step 29 counted an absence as a gap
    and so described 449 single-part records as missing a component identity.
    """
    report: list[EvidenceField] = []
    for field, label, classify, source, recoverable, basis in _EVIDENCE_FIELDS:
        counts = {state: 0 for state in EVIDENCE_STATES}
        for record in records:
            counts[classify(record)] += 1

        total = len(records)
        applicable = total - counts[EVIDENCE_NOT_APPLICABLE]
        known = counts[EVIDENCE_KNOWN]
        unrecoverable = counts[EVIDENCE_UNRECOVERABLE]

        note = ""
        if not applicable:
            note = "No record in this slice can carry this field."
        elif unrecoverable:
            note = UNRECOVERABLE_NOTE.format(count=unrecoverable)

        guard = _guard(known, minimum)
        if not guard.is_actionable and applicable:
            note = (note + " " if note else "") + SMALL_SAMPLE_TEXT.format(count=known)

        report.append(
            EvidenceField(
                field=field,
                label=label,
                total=total,
                known=known,
                not_applicable=counts[EVIDENCE_NOT_APPLICABLE],
                unrecorded=counts[EVIDENCE_UNRECORDED],
                unrecoverable=unrecoverable,
                applicable=applicable,
                percent_known=(known / applicable) if applicable else None,
                basis=basis,
                source_of_truth=source,
                recoverable=recoverable,
                guard=guard,
                note=note.strip(),
            )
        )
    return report


def readiness(records: list[FeedbackRecord], minimum: int = 0) -> DatasetReadiness:
    """What a later learning step would have to work with, and what it lacks."""
    total = len(records)
    paired = sum(
        1
        for r in records
        if r.system_operation is not None and (r.user_operation is not None or r.costed)
    )
    usable_time = sum(
        1 for r in records if r.system_cycle_time is not None and r.user_cycle_time is not None
    )
    usable_operation = sum(
        1 for r in records if r.system_operation is not None and r.user_operation is not None
    )
    with_component = sum(1 for r in records if r.component_id or r.component_name)
    with_feature = sum(1 for r in records if r.feature_id or r.feature_ids)
    with_rule = sum(1 for r in records if r.rule_id)
    with_source = sum(1 for r in records if r.source_type != UNKNOWN)
    with_feature_confidence = sum(1 for r in records if r.feature_confidence != UNKNOWN)
    with_profile = sum(1 for r in records if r.time_profile_version)

    trainable = sum(
        1
        for r in records
        if r.system_operation is not None
        and r.user_operation is not None
        and (r.feature_id or r.feature_ids)
    )

    evidence = evidence_fields(records, minimum)
    by_field = {entry.field: entry for entry in evidence}

    # Only where the field could have existed. A standalone part is not missing
    # a component identity, and an unfinished operation is not missing a paired
    # time — counting either as a gap is what Step 30 exists to stop.
    missing = {
        entry.field: entry.unrecorded + entry.unrecoverable
        for entry in evidence
        if entry.unrecorded or entry.unrecoverable
    }
    missing["feature_identity"] = total - with_feature
    missing["user_operation"] = total - usable_operation
    missing = {field: count for field, count in missing.items() if count}

    return DatasetReadiness(
        total_records=total,
        with_system_prediction_and_user_outcome=paired,
        with_usable_time_correction=usable_time,
        with_usable_operation_correction=usable_operation,
        with_component_identity=with_component,
        with_feature_identity=with_feature,
        with_rule_id=with_rule,
        with_known_source_type=with_source,
        with_known_feature_confidence=with_feature_confidence,
        with_time_profile_version=with_profile,
        potential_training_rows=trainable,
        insufficient_rows=total - trainable,
        missing_fields=missing,
        evidence=evidence,
        permanently_unknown=sum(entry.unrecoverable for entry in evidence),
        safe_backfill_available=sum(entry.unrecorded for entry in evidence),
        note=READINESS_NOTE,
    )
