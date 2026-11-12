"""Estimate history and comparison models — pure, with no database dependency.

`repository.py` builds `EstimateSummary` rows out of storage; this module holds
the shapes and the comparison arithmetic so both stay testable without standing
up a database.

Comparison works entirely from what was persisted when each estimate was made.
Nothing is recalculated, so a historical estimate keeps reporting the numbers it
was created with even after newer rate set versions are published.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from cad_costing_agent.money import Money, Percent

COMPARISON_BASIS = (
    "Both sides are read from the totals persisted with each estimate. Nothing is "
    "recalculated, so publishing a newer rate set version never changes a past comparison."
)


class EstimateSummary(BaseModel):
    """Concise history row. Deliberately excludes the audit snapshots."""

    estimate_id: str
    sequence: int = Field(
        description=(
            "Monotonic recording order. Ordering and newer/older comparisons use this "
            "rather than created_at, whose resolution is too coarse (~15 ms on Windows) "
            "to separate estimates created in quick succession."
        )
    )
    created_at: datetime
    source: str
    part_id: str
    part_name: str
    quantity: int
    rate_set_id: str | None = None
    rate_set_version: str | None = None
    currency: str
    total_material_cost: Money
    total_operation_cost: Money
    total_additional_cost: Money
    total_part_cost: Money


class EstimateListPage(BaseModel):
    """One page of history, plus enough metadata to page through the rest."""

    total: int
    limit: int
    offset: int
    items: list[EstimateSummary]


class CostDelta(BaseModel):
    baseline: Money
    comparison: Money
    delta: Money
    percent_change: Percent = None


# ---------------------------------------------------------------------------
# Cost impact analysis
#
# "Why did this cost change?" answered from what was persisted, and answered
# without inventing a cause.
#
# The decomposition rests on one piece of arithmetic and one rule.
#
# **The arithmetic.** Almost every cost component here is `rate × driver` — a
# price per kg times a mass, a rate per hour times a number of minutes. When
# both sides move, the change splits exactly three ways:
#
#     delta        = r_a·d_a − r_b·d_b
#     rate effect  = (r_a − r_b)·d_b
#     driver effect= r_b·(d_a − d_b)
#     interaction  = (r_a − r_b)·(d_a − d_b)
#
# Those three sum to the delta with no remainder. This is an algebraic identity,
# not a model: nothing is apportioned by judgement, and when only one factor
# moved the interaction is zero and the attribution is exact.
#
# **The rule.** The interaction term is reported as its own `combined` impact
# rather than folded into either side, because folding it in would silently
# credit one input with an effect that needed both. When several inputs changed
# at once the result says so — `attribution: multiple_inputs` — and shows every
# before/after value rather than dividing the total into causal percentages
# nobody can justify.
#
# Nothing is recalculated. Both sides are read from the snapshots each estimate
# stored, so publishing a newer rate set or time profile can never change a past
# comparison.
# ---------------------------------------------------------------------------

# Below this, a money difference is rounding noise rather than a real effect.
NEGLIGIBLE = 0.005

# Every stored amount is rounded to two decimals, and a decomposition of a dozen
# of them can drift by a few pennies without anything being unexplained. Beyond
# this the remainder is a real gap and is labelled as one.
ROUNDING_ALLOWANCE = 0.5

ATTRIBUTION_SINGLE = "single_input"
ATTRIBUTION_MULTIPLE = "multiple_inputs"
ATTRIBUTION_NONE = "no_change"

MULTIPLE_INPUTS_NOTE = (
    "More than one input changed, so no single one explains the difference. Each impact below is "
    "an exact algebraic share of the total — rate effects at the old volumes, volume effects at "
    "the old rates, and whatever needed both changes at once shown separately as a combined "
    "effect. No causal percentage is inferred."
)

SEQUENCE_NOTE = (
    "The order of operations and setups changed. The current costing model has no "
    "sequence-dependent cost — no setup consolidation, no routing optimisation — so this "
    "contributes nothing to the difference. Setup *membership* does affect cost and is reported "
    "separately."
)

CURRENCY_MISMATCH_NOTE = (
    "These estimates are denominated in different currencies and are never converted, so the "
    "money figures cannot be subtracted from one another. The input differences below are still "
    "meaningful; the cost impacts are not, and are omitted."
)


class EstimateDetail(BaseModel):
    """The persisted snapshots of one estimate.

    A plain carrier so this module can read an estimate's full history without
    importing the repository layer, which imports this one.
    """

    estimate_id: str
    request_snapshot: dict = Field(default_factory=dict)
    result_snapshot: dict = Field(default_factory=dict)
    rate_set_snapshot: dict | None = None
    plan_snapshot: dict | None = None
    time_profile_snapshot: dict | None = None


class SetupAssumption(BaseModel):
    """One setup as it stood when the estimate was made."""

    setup_id: str
    name: str
    sequence_position: int
    setup_time_minutes: float | None = None
    operation_count: int = 0


class EstimateAssumptions(BaseModel):
    """Everything an estimate rested on, gathered from its own snapshots.

    This is the "what was I assuming?" panel in model form. Every field is read
    back from storage rather than recomputed, so it says what the estimate
    actually used, not what the same inputs would produce today.
    """

    estimate_id: str

    # --- CAD ---
    source_file: str | None = None
    source_format: str | None = None
    geometry_source: str | None = None
    geometry_status: str | None = None
    volume_mm3: float | None = None
    volume_method: str | None = None
    mass_kg: float | None = None

    # --- material ---
    material: str | None = None
    density_kg_m3: float | None = None
    material_cost_per_kg: float | None = None
    material_is_illustrative: bool | None = None
    # What material was costed from. `mass_kg` above is always the finished
    # part's own; when the material came from the stock it is cut from, that
    # stock and its mass are named here, never put in the part's place.
    material_basis: str | None = None
    stock_description: str | None = None
    stock_mass_kg: float | None = None

    # --- costing profile (money) ---
    rate_set_id: str | None = None
    rate_set_version: str | None = None
    rate_set_name: str | None = None
    rate_set_is_illustrative: bool | None = None
    currency: str | None = None

    # --- time profile (duration) ---
    time_profile_id: str | None = None
    time_profile_version: str | None = None
    time_profile_name: str | None = None
    time_profile_is_illustrative: bool | None = None

    # --- the manufacturing plan ---
    quantity: int | None = None
    setup_count: int = 0
    operation_count: int = 0
    setups: list[SetupAssumption] = Field(default_factory=list)
    sequence: list[str] = Field(default_factory=list)
    total_setup_time_minutes: float = 0.0
    total_cycle_time_minutes: float = 0.0

    # --- additional costs ---
    overhead_rate_percent: float | None = None
    scrap_rate_percent: float | None = None
    tooling_cost_per_batch: float | None = None

    @property
    def volume_basis_is_assumed(self) -> bool:
        """Whether the volume behind the mass was stated by a person.

        A plain property rather than a computed field, so the assumptions
        response keeps the shape it has always had. It reads `volume_method`,
        which `complete_geometry` sets to one of the two `user_supplied_*`
        methods and never to anything else that a measurement could produce —
        so the distinction is read back out of the record rather than inferred
        from a confidence label or the presence of a number.
        """
        return "user_supplied" in (self.volume_method or "")


class InputChange(BaseModel):
    """One input that differs between the two estimates."""

    field: str
    label: str
    before: str | None = None
    after: str | None = None
    affects_cost: bool = Field(
        default=True,
        description="False for differences the current costing model does not price, e.g. sequence.",
    )
    group: str = Field(
        default="",
        description=(
            "The input this field belongs to. Attribution counts inputs rather than fields, "
            "because one change often moves several fields at once."
        ),
    )


class CostImpact(BaseModel):
    """How much of the difference one factor accounts for.

    `delta` is an exact algebraic share, not an estimate of blame. Where two
    factors moved together the part that required both appears under its own
    `combined` category rather than being attributed to either.
    """

    category: str
    description: str
    before: Money = 0.0
    after: Money = 0.0
    delta: Money = 0.0
    currency: str | None = None


class OperationComparison(BaseModel):
    """One operation across the two estimates, matched by its id."""

    operation_id: str
    status: str = Field(description="'added', 'removed', 'changed' or 'unchanged'.")
    operation_name: str | None = None
    feature_ids: list[str] = Field(default_factory=list)
    process_before: str | None = None
    process_after: str | None = None
    cycle_before: float | None = None
    cycle_after: float | None = None
    setup_before: float | None = None
    setup_after: float | None = None
    cost_before: Money | None = None
    cost_after: Money | None = None
    cost_delta: Money | None = None
    setup_id_before: str | None = None
    setup_id_after: str | None = None
    moved_setup: bool = False
    changes: list[str] = Field(default_factory=list)


class SequenceComparison(BaseModel):
    """Order before and after, and what that costs — which is nothing."""

    changed: bool = False
    before: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)
    cost_impact: Money = 0.0
    note: str = SEQUENCE_NOTE


# --------------------------------------------------------------- extraction


def _get(source: dict | None, *path, default=None):
    """Read a nested key, tolerating a missing branch anywhere along it."""
    current = source
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def extract_assumptions(detail: EstimateDetail) -> EstimateAssumptions:
    """Read back everything one estimate assumed, from its own snapshots."""
    request = detail.request_snapshot or {}
    result = detail.result_snapshot or {}
    part_cost = result.get("part_cost") or {}
    material_cost = part_cost.get("material") or {}
    material = material_cost.get("material") or {}
    additional = part_cost.get("additional_costs") or {}
    geometry = result.get("geometry") or request.get("geometry") or {}

    from_stock = result.get("material_basis") == "bar_stock"
    stock = request.get("raw_stock") or {}
    stock_description = (
        f"round bar Ø{stock['diameter_mm']:g} × {stock['length_mm']:g} mm"
        if from_stock and stock.get("diameter_mm") and stock.get("length_mm")
        else None
    )

    plan = _get(detail.plan_snapshot, "plan", default={}) or {}
    setups = plan.get("setups") or []
    operations = plan.get("operations") or []
    active = [o for o in operations if o.get("user_action") != "rejected"]

    by_setup: dict[str, list[dict]] = {}
    for operation in active:
        key = operation.get("user_setup_id") or operation.get("system_setup_id") or ""
        by_setup.setdefault(key, []).append(operation)

    setup_rows = [
        SetupAssumption(
            setup_id=setup.get("setup_id", ""),
            name=setup.get("name", ""),
            sequence_position=setup.get("sequence_position", 0),
            setup_time_minutes=(
                setup.get("user_setup_time_minutes")
                if setup.get("user_setup_time_minutes") is not None
                else (
                    setup.get("system_setup_time_minutes")
                    if setup.get("system_setup_time_accepted")
                    else None
                )
            ),
            operation_count=len(by_setup.get(setup.get("setup_id", ""), [])),
        )
        for setup in sorted(setups, key=lambda s: s.get("sequence_position", 0))
    ]

    costed = part_cost.get("operations") or []
    quantity = part_cost.get("material", {}).get("quantity") or request.get("quantity")

    # The costed operations are the honest count: a plan row with no committed
    # time never reached the money and should not be described as if it had.
    return EstimateAssumptions(
        estimate_id=detail.estimate_id,
        source_file=request.get("source_file"),
        source_format=geometry.get("source_format"),
        geometry_source=geometry.get("geometry_source"),
        geometry_status=geometry.get("geometry_status"),
        volume_mm3=geometry.get("volume"),
        volume_method=geometry.get("volume_method"),
        # The part's own mass: from the CAD result when it has one, since the
        # material line carries the stock's mass when material came from a bar.
        mass_kg=(
            _get(result, "mass", "mass_kg")
            if from_stock
            else material_cost.get("mass_kg") or _get(request, "mass", "mass_kg")
        ),
        material_basis=result.get("material_basis"),
        stock_description=stock_description,
        stock_mass_kg=_get(result, "stock_mass", "mass_kg") if from_stock else None,
        material=material.get("name") or request.get("material"),
        density_kg_m3=material.get("density_kg_m3"),
        material_cost_per_kg=material_cost.get("material_cost_per_kg"),
        material_is_illustrative=material.get("illustrative"),
        rate_set_id=_get(detail.rate_set_snapshot, "rate_set_id") or request.get("rate_set_id"),
        rate_set_version=_get(detail.rate_set_snapshot, "version") or request.get("rate_set_version"),
        rate_set_name=_get(detail.rate_set_snapshot, "name"),
        rate_set_is_illustrative=_get(detail.rate_set_snapshot, "is_illustrative"),
        currency=part_cost.get("currency"),
        time_profile_id=plan.get("time_profile_id"),
        time_profile_version=plan.get("time_profile_version"),
        time_profile_name=plan.get("time_profile_name"),
        time_profile_is_illustrative=_time_profile_is_illustrative(detail),
        quantity=quantity,
        setup_count=len(setup_rows),
        operation_count=len(costed),
        setups=setup_rows,
        sequence=_sequence_of(plan, costed),
        total_setup_time_minutes=round(
            sum(float(o.get("setup_time_minutes") or 0.0) for o in costed), 6
        ),
        # Per part, deliberately: multiplying by quantity would make a quantity
        # change look like a second, independent change to the plan.
        total_cycle_time_minutes=round(
            sum(float(o.get("cycle_time_minutes") or 0.0) for o in costed), 6
        ),
        overhead_rate_percent=additional.get("overhead_rate_percent"),
        scrap_rate_percent=additional.get("scrap_rate_percent"),
        tooling_cost_per_batch=additional.get("tooling_cost_per_batch"),
    )


def _time_profile_is_illustrative(detail: EstimateDetail) -> bool | None:
    profiles = _get(detail.time_profile_snapshot, "profiles", default={}) or {}
    for profile in profiles.values():
        if isinstance(profile, dict) and "is_illustrative" in profile:
            return profile["is_illustrative"]
    return None


def _sequence_of(plan: dict, costed: list[dict]) -> list[str]:
    """The routing as it was: setups in order, operations in order within them.

    Falls back to the costed operations when there is no plan, so an estimate
    made from hand-entered requirements still reports an order.
    """
    setups = plan.get("setups") or []
    operations = plan.get("operations") or []
    if not setups:
        return [str(o.get("process_type") or o.get("operation_id") or "") for o in costed]

    position = {s.get("setup_id"): s.get("sequence_position", 0) for s in setups}
    active = [o for o in operations if o.get("user_action") != "rejected"]
    ordered = sorted(
        active,
        key=lambda o: (
            position.get(o.get("user_setup_id") or o.get("system_setup_id"), 10**6),
            o.get("sequence_position", 0),
            o.get("operation_id", ""),
        ),
    )
    return [
        f"{o.get('user_operation_type') or o.get('system_suggested_operation') or '?'}"
        f"@{o.get('user_setup_id') or o.get('system_setup_id') or '?'}"
        for o in ordered
    ]


# ------------------------------------------------------------- decomposition


def _two_factor(
    *,
    rate_category: str,
    driver_category: str,
    rate_label: str,
    driver_label: str,
    rate_before: float,
    rate_after: float,
    driver_before: float,
    driver_after: float,
    currency: str | None,
    driver_display: tuple[float, float] | None = None,
) -> list[CostImpact]:
    """Split `rate × driver` into rate, driver and interaction effects.

    The three sum exactly to the change with no remainder — an identity, not an
    apportionment. The interaction is reported separately rather than folded
    into either side, because it is the part that required *both* to move and
    belongs to neither alone.

    `rate` and `driver` must multiply to the actual cost, so a per-hour rate is
    paired with **hours**. `driver_display` carries the figures a person would
    rather read — minutes — without letting them into the arithmetic, because a
    per-hour rate times minutes is sixty times the truth.
    """
    shown_before, shown_after = (
        driver_display if driver_display is not None else (driver_before, driver_after)
    )
    before = rate_before * driver_before
    after = rate_after * driver_after
    if abs(after - before) < NEGLIGIBLE:
        return []

    rate_effect = (rate_after - rate_before) * driver_before
    driver_effect = rate_before * (driver_after - driver_before)
    interaction = (rate_after - rate_before) * (driver_after - driver_before)

    impacts: list[CostImpact] = []
    if abs(rate_effect) >= NEGLIGIBLE:
        impacts.append(
            CostImpact(
                category=rate_category,
                description=f"{rate_label} {rate_before:g} → {rate_after:g}",
                before=before,
                after=before + rate_effect,
                delta=rate_effect,
                currency=currency,
            )
        )
    if abs(driver_effect) >= NEGLIGIBLE:
        impacts.append(
            CostImpact(
                category=driver_category,
                description=f"{driver_label} {shown_before:g} → {shown_after:g}",
                before=before,
                after=before + driver_effect,
                delta=driver_effect,
                currency=currency,
            )
        )
    if abs(interaction) >= NEGLIGIBLE:
        impacts.append(
            CostImpact(
                category="combined",
                description=(
                    f"{rate_label} and {driver_label} both changed; this part needed both and is "
                    "attributed to neither alone"
                ),
                before=before,
                after=before + interaction,
                delta=interaction,
                currency=currency,
            )
        )
    return impacts


def _flat(category: str, description: str, before: float, after: float, currency: str | None):
    if abs(after - before) < NEGLIGIBLE:
        return []
    return [
        CostImpact(
            category=category,
            description=description,
            before=before,
            after=after,
            delta=after - before,
            currency=currency,
        )
    ]


def _operations_of(detail: EstimateDetail) -> dict[str, dict]:
    costed = _get(detail.result_snapshot, "part_cost", "operations", default=[]) or []
    return {str(o.get("operation_id")): o for o in costed}


def _setup_membership(detail: EstimateDetail) -> dict[str, str | None]:
    operations = _get(detail.plan_snapshot, "plan", "operations", default=[]) or []
    return {
        str(o.get("operation_id")): (o.get("user_setup_id") or o.get("system_setup_id"))
        for o in operations
    }


def _run_minutes(operation: dict) -> float:
    return float(operation.get("cycle_time_minutes") or 0.0) * int(operation.get("quantity") or 1)


def _run_cost(operation: dict) -> float:
    return float(operation.get("machine_cost") or 0.0) + float(operation.get("labor_cost") or 0.0)


def _effective_rate(cost: float, minutes: float) -> float:
    """The blended per-hour rate a set of operations actually worked out at."""
    return (cost / (minutes / 60.0)) if minutes else 0.0


def decompose(
    baseline: EstimateDetail, comparison: EstimateDetail, currency: str | None
) -> list[CostImpact]:
    """Account for the difference between two estimates, factor by factor.

    Every impact is read from the stored snapshots and is an exact share of the
    total. A closing `unexplained` entry appears if the parts ever fail to sum
    to the whole, so a gap can never pass silently as an explanation.
    """
    before_pc = _get(baseline.result_snapshot, "part_cost", default={}) or {}
    after_pc = _get(comparison.result_snapshot, "part_cost", default={}) or {}
    impacts: list[CostImpact] = []

    # --- material: price per kg against mass × quantity. A material carried by
    # --- a stage's rate was not charged, so it enters at a rate of zero.
    before_material = before_pc.get("material") or {}
    after_material = after_pc.get("material") or {}

    def charged_rate(part_cost: dict, material: dict) -> float:
        if part_cost.get("material_charged") is False:
            return 0.0
        return float(material.get("material_cost_per_kg") or 0.0)

    impacts.extend(
        _two_factor(
            rate_category="material_rate",
            driver_category="material_quantity",
            rate_label="cost/kg",
            driver_label="mass × quantity (kg)",
            rate_before=charged_rate(before_pc, before_material),
            rate_after=charged_rate(after_pc, after_material),
            driver_before=float(before_material.get("mass_kg") or 0.0)
            * int(before_material.get("quantity") or 1),
            driver_after=float(after_material.get("mass_kg") or 0.0)
            * int(after_material.get("quantity") or 1),
            currency=currency,
        )
    )

    # --- operations, matched by id so an added one is never mistaken for a
    # --- longer cycle time on an existing one.
    before_ops = _operations_of(baseline)
    after_ops = _operations_of(comparison)
    matched = sorted(set(before_ops) & set(after_ops))
    removed = sorted(set(before_ops) - set(after_ops))
    added = sorted(set(after_ops) - set(before_ops))

    run_before = sum(_run_cost(before_ops[k]) for k in matched)
    run_after = sum(_run_cost(after_ops[k]) for k in matched)
    minutes_before = sum(_run_minutes(before_ops[k]) for k in matched)
    minutes_after = sum(_run_minutes(after_ops[k]) for k in matched)
    impacts.extend(
        _two_factor(
            rate_category="operation_rate",
            driver_category="operation_time",
            rate_label="blended machine+labour rate/hour",
            driver_label="run minutes",
            rate_before=_effective_rate(run_before, minutes_before),
            rate_after=_effective_rate(run_after, minutes_after),
            driver_before=minutes_before / 60.0,
            driver_after=minutes_after / 60.0,
            driver_display=(minutes_before, minutes_after),
            currency=currency,
        )
    )

    setup_cost_before = sum(float(before_ops[k].get("setup_cost") or 0.0) for k in matched)
    setup_cost_after = sum(float(after_ops[k].get("setup_cost") or 0.0) for k in matched)
    setup_minutes_before = sum(float(before_ops[k].get("setup_time_minutes") or 0.0) for k in matched)
    setup_minutes_after = sum(float(after_ops[k].get("setup_time_minutes") or 0.0) for k in matched)
    impacts.extend(
        _two_factor(
            rate_category="setup_rate",
            driver_category="setup_time",
            rate_label="machine rate/hour on setups",
            driver_label="setup minutes",
            rate_before=_effective_rate(setup_cost_before, setup_minutes_before),
            rate_after=_effective_rate(setup_cost_after, setup_minutes_after),
            driver_before=setup_minutes_before / 60.0,
            driver_after=setup_minutes_after / 60.0,
            driver_display=(setup_minutes_before, setup_minutes_after),
            currency=currency,
        )
    )

    for key in removed:
        operation = before_ops[key]
        impacts.extend(
            _flat(
                "operation_removed",
                f"{operation.get('operation_name') or key} ({operation.get('process_type')}) "
                "is no longer in the plan",
                float(operation.get("total_operation_cost") or 0.0),
                0.0,
                currency,
            )
        )
    for key in added:
        operation = after_ops[key]
        impacts.extend(
            _flat(
                "operation_added",
                f"{operation.get('operation_name') or key} ({operation.get('process_type')}) "
                "was added to the plan",
                0.0,
                float(operation.get("total_operation_cost") or 0.0),
                currency,
            )
        )

    # --- non-machining route stages, matched by stage id, each a stored total ---
    before_stages = {s.get("stage"): s for s in before_pc.get("stage_costs") or []}
    after_stages = {s.get("stage"): s for s in after_pc.get("stage_costs") or []}
    for stage_id in [*before_stages, *(s for s in after_stages if s not in before_stages)]:
        before_stage = before_stages.get(stage_id) or {}
        after_stage = after_stages.get(stage_id) or {}
        label = after_stage.get("label") or before_stage.get("label") or stage_id
        # An entered yield that differs is named, read as stored. A line with
        # no yield has no such key, and reads "none".
        yields = [stage.get("yield_percent") for stage in (before_stage, after_stage)]
        yield_change = (
            ""
            if yields[0] == yields[1]
            else "; yield "
            + " → ".join("none" if value is None else f"{value:g}%" for value in yields)
        )
        impacts.extend(
            _flat(
                "route_stage",
                f"{label} (route stage{yield_change})"
                if before_stage and after_stage
                else f"{label} is no longer a route stage"
                if before_stage
                else f"{label} was added as a route stage",
                float(before_stage.get("total_cost") or 0.0),
                float(after_stage.get("total_cost") or 0.0),
                currency,
            )
        )

    # --- additional costs, each already a separate figure in the snapshot ---
    before_additional = before_pc.get("additional_costs") or {}
    after_additional = after_pc.get("additional_costs") or {}
    for category, cost_key, rate_key, label in [
        ("overhead", "overhead_cost", "overhead_rate_percent", "overhead"),
        ("scrap", "scrap_cost", "scrap_rate_percent", "scrap allowance"),
        ("tooling", "tooling_cost", "tooling_cost_per_batch", "tooling per batch"),
    ]:
        before_rate = before_additional.get(rate_key)
        after_rate = after_additional.get(rate_key)
        rate_note = (
            f" ({label} {before_rate:g} → {after_rate:g})"
            if before_rate is not None and after_rate is not None and before_rate != after_rate
            else f" ({label} unchanged at {before_rate:g}; it applies to a different direct cost)"
            if before_rate is not None and after_rate is not None
            else ""
        )
        impacts.extend(
            _flat(
                category,
                f"{label.capitalize()}{rate_note}",
                float(before_additional.get(cost_key) or 0.0),
                float(after_additional.get(cost_key) or 0.0),
                currency,
            )
        )

    # --- the parts must sum to the whole, or the gap is reported as a gap ---
    total_before = float(before_pc.get("total_part_cost") or 0.0)
    total_after = float(after_pc.get("total_part_cost") or 0.0)
    explained = sum(float(impact.delta) for impact in impacts)
    residual = (total_after - total_before) - explained
    if abs(residual) >= NEGLIGIBLE:
        # Every stored amount is rounded to two decimals, so a few pennies can
        # fail to line up without anything being unaccounted for. That is worth
        # distinguishing from a real gap: one is arithmetic, the other would be
        # a hole in the explanation.
        is_rounding = abs(residual) <= ROUNDING_ALLOWANCE
        impacts.append(
            CostImpact(
                category="rounding" if is_rounding else "unexplained",
                description=(
                    "Stored amounts are rounded to two decimals, so the parts differ from the "
                    "whole by a few pennies."
                    if is_rounding
                    else "The impacts above do not fully account for the difference. This "
                    "remainder is reported rather than absorbed, because a missing explanation "
                    "must not look like a complete one."
                ),
                before=0.0,
                after=residual,
                delta=residual,
                currency=currency,
            )
        )

    return impacts


# ------------------------------------------------------------ input changes


# Each field carries the *input* it belongs to. Attribution counts inputs, not
# fields: adding a setup changes both the setup count and the total setup
# minutes, but the engineer changed one thing, and saying "multiple inputs
# changed" there would be technically true and practically misleading.
_COMPARABLE_FIELDS: tuple[tuple[str, str, bool, str], ...] = (
    ("source_file", "CAD file", True, "cad"),
    ("geometry_source", "Geometry source", True, "cad"),
    ("volume_mm3", "Volume (mm³)", True, "cad"),
    ("mass_kg", "Mass (kg)", True, "cad"),
    ("material", "Material", True, "material"),
    ("density_kg_m3", "Density (kg/m³)", True, "material"),
    # Costing material from the bar instead of the finished part is one input,
    # and it moves the material cost without the part's mass changing at all.
    ("material_basis", "Material costed from", True, "material"),
    ("stock_mass_kg", "Stock mass (kg)", True, "material"),
    ("material_cost_per_kg", "Material cost/kg", True, "rates"),
    ("rate_set_id", "Costing profile", True, "rates"),
    ("rate_set_version", "Costing profile version", True, "rates"),
    ("currency", "Currency", True, "rates"),
    ("overhead_rate_percent", "Overhead %", True, "rates"),
    ("scrap_rate_percent", "Scrap %", True, "rates"),
    ("tooling_cost_per_batch", "Tooling per batch", True, "rates"),
    ("time_profile_id", "Time profile", True, "time_profile"),
    ("time_profile_version", "Time profile version", True, "time_profile"),
    ("quantity", "Quantity", True, "quantity"),
    ("setup_count", "Setup count", True, "setups"),
    ("total_setup_time_minutes", "Total setup minutes", True, "setups"),
    ("operation_count", "Costed operations", True, "operations"),
    ("total_cycle_time_minutes", "Cycle minutes per part", True, "operations"),
)

# The material's own price comes from the rate set, so a rate-set change that
# moves it is one input, not two. A change of *material* is separate, and its
# own group above.
_GROUP_LABELS = {
    "cad": "the CAD model",
    "material": "the material",
    "rates": "the costing profile",
    "time_profile": "the time profile",
    "quantity": "the quantity",
    "setups": "the setup grouping",
    "operations": "the operations",
    "sequence": "the order of work",
}


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def input_changes(
    before: EstimateAssumptions, after: EstimateAssumptions, *, sequence_changed: bool
) -> list[InputChange]:
    """Every input that differs, named rather than summarised as "inputs"."""
    changes = [
        InputChange(
            field=field,
            label=label,
            before=_as_text(getattr(before, field)),
            after=_as_text(getattr(after, field)),
            affects_cost=affects_cost,
            group=group,
        )
        for field, label, affects_cost, group in _COMPARABLE_FIELDS
        if getattr(before, field) != getattr(after, field)
    ]
    if sequence_changed:
        changes.append(
            InputChange(
                field="sequence",
                label="Operation / setup order",
                before=" → ".join(before.sequence) or None,
                after=" → ".join(after.sequence) or None,
                # Stated rather than hidden: the current model prices no ordering.
                affects_cost=False,
                group="sequence",
            )
        )
    return changes


def compare_operations(
    baseline: EstimateDetail, comparison: EstimateDetail
) -> list[OperationComparison]:
    """Match operations by id and report what moved, in a stable order."""
    before_ops = _operations_of(baseline)
    after_ops = _operations_of(comparison)
    before_setup = _setup_membership(baseline)
    after_setup = _setup_membership(comparison)

    rows: list[OperationComparison] = []
    for operation_id in sorted(set(before_ops) | set(after_ops)):
        before = before_ops.get(operation_id)
        after = after_ops.get(operation_id)

        if before is None:
            status, changes = "added", ["added to the plan"]
        elif after is None:
            status, changes = "removed", ["removed from the plan"]
        else:
            changes = []
            if before.get("process_type") != after.get("process_type"):
                changes.append(
                    f"process {before.get('process_type')} → {after.get('process_type')}"
                )
            if before.get("cycle_time_minutes") != after.get("cycle_time_minutes"):
                changes.append(
                    f"cycle {before.get('cycle_time_minutes')} → {after.get('cycle_time_minutes')} min"
                )
            if before.get("setup_time_minutes") != after.get("setup_time_minutes"):
                changes.append(
                    f"setup {before.get('setup_time_minutes')} → {after.get('setup_time_minutes')} min"
                )
            if before_setup.get(operation_id) != after_setup.get(operation_id):
                changes.append(
                    f"moved to setup {after_setup.get(operation_id)}"
                )
            status = "changed" if changes else "unchanged"

        source = after or before or {}
        cost_before = float(before.get("total_operation_cost") or 0.0) if before else None
        cost_after = float(after.get("total_operation_cost") or 0.0) if after else None
        rows.append(
            OperationComparison(
                operation_id=operation_id,
                status=status,
                operation_name=source.get("operation_name"),
                process_before=before.get("process_type") if before else None,
                process_after=after.get("process_type") if after else None,
                cycle_before=before.get("cycle_time_minutes") if before else None,
                cycle_after=after.get("cycle_time_minutes") if after else None,
                setup_before=before.get("setup_time_minutes") if before else None,
                setup_after=after.get("setup_time_minutes") if after else None,
                cost_before=cost_before,
                cost_after=cost_after,
                cost_delta=(
                    (cost_after or 0.0) - (cost_before or 0.0)
                    if cost_before is not None or cost_after is not None
                    else None
                ),
                setup_id_before=before_setup.get(operation_id),
                setup_id_after=after_setup.get(operation_id),
                moved_setup=(
                    before is not None
                    and after is not None
                    and before_setup.get(operation_id) != after_setup.get(operation_id)
                ),
                changes=changes,
            )
        )
    return rows


class EstimateComparison(BaseModel):
    """Two estimates side by side, and an account of why they differ.

    The totals were always here. What Step 22 adds is the *why*: the assumptions
    each side rested on, every input that differs, an exact decomposition of the
    money, and an operation-by-operation diff.
    """

    baseline: EstimateSummary
    comparison: EstimateSummary
    older_estimate_id: str
    newer_estimate_id: str
    comparison_is_newer: bool
    material_cost: CostDelta
    operation_cost: CostDelta
    additional_cost: CostDelta
    total_part_cost: CostDelta
    basis: str = COMPARISON_BASIS

    # --- Step 22: what changed, and what each change cost -------------------
    baseline_assumptions: EstimateAssumptions | None = None
    comparison_assumptions: EstimateAssumptions | None = None
    changes: list[InputChange] = Field(default_factory=list)
    impacts: list[CostImpact] = Field(default_factory=list)
    operations: list[OperationComparison] = Field(default_factory=list)
    sequence: SequenceComparison | None = None

    attribution: str = Field(
        default=ATTRIBUTION_NONE,
        description="One of 'no_change', 'single_input', 'multiple_inputs'.",
    )
    attribution_note: str = ""
    currencies_match: bool = True
    detailed: bool = Field(
        default=False,
        description="False when the stored snapshots were not supplied, leaving totals only.",
    )


def _delta(baseline: float, comparison: float) -> CostDelta:
    difference = comparison - baseline
    # A percentage against a zero baseline is undefined rather than infinite.
    percent_change = (difference / baseline) * 100 if baseline else None
    return CostDelta(
        baseline=baseline,
        comparison=comparison,
        delta=difference,
        percent_change=percent_change,
    )


def compare_estimates(
    baseline: EstimateSummary,
    comparison: EstimateSummary,
    *,
    baseline_detail: EstimateDetail | None = None,
    comparison_detail: EstimateDetail | None = None,
) -> EstimateComparison:
    """Compare two persisted estimates. Deltas are `comparison - baseline`.

    Which side is newer is decided by recording order, independently of the
    argument order. `sequence` is used rather than `created_at` because the
    clock's resolution is too coarse to separate two estimates created in quick
    succession, which would otherwise make the answer arbitrary.

    Supplying both stored snapshots adds the Step 22 sections: the assumptions
    each side rested on, the inputs that differ, an exact cost decomposition and
    an operation-level diff. Without them the result is the totals-only
    comparison it has always been, marked `detailed: false` rather than
    pretending the detail is simply empty.
    """
    comparison_is_newer = comparison.sequence > baseline.sequence
    older, newer = (
        (baseline, comparison) if comparison_is_newer else (comparison, baseline)
    )

    result = EstimateComparison(
        baseline=baseline,
        comparison=comparison,
        older_estimate_id=older.estimate_id,
        newer_estimate_id=newer.estimate_id,
        comparison_is_newer=comparison_is_newer,
        material_cost=_delta(baseline.total_material_cost, comparison.total_material_cost),
        operation_cost=_delta(baseline.total_operation_cost, comparison.total_operation_cost),
        additional_cost=_delta(baseline.total_additional_cost, comparison.total_additional_cost),
        total_part_cost=_delta(baseline.total_part_cost, comparison.total_part_cost),
        currencies_match=baseline.currency == comparison.currency,
    )

    if baseline_detail is None or comparison_detail is None:
        return result

    before = extract_assumptions(baseline_detail)
    after = extract_assumptions(comparison_detail)

    sequence = SequenceComparison(
        changed=before.sequence != after.sequence,
        before=before.sequence,
        after=after.sequence,
        cost_impact=0.0,
    )
    changes = input_changes(before, after, sequence_changed=sequence.changed)
    operations = compare_operations(baseline_detail, comparison_detail)

    # Money in two currencies is not subtractable, and no amount of
    # decomposition would make it so. The input differences still stand.
    if not result.currencies_match:
        return result.model_copy(
            update={
                "baseline_assumptions": before,
                "comparison_assumptions": after,
                "changes": changes,
                "operations": operations,
                "sequence": sequence,
                "impacts": [],
                "attribution": ATTRIBUTION_NONE,
                "attribution_note": CURRENCY_MISMATCH_NOTE,
                "detailed": True,
            }
        )

    impacts = decompose(baseline_detail, comparison_detail, before.currency)
    priced_changes = [change for change in changes if change.affects_cost]
    groups = sorted({change.group for change in priced_changes})

    if not changes:
        attribution = ATTRIBUTION_NONE
        note = "Nothing differs between these two estimates."
    elif not priced_changes:
        attribution = ATTRIBUTION_NONE
        note = (
            "The only differences are ones the current costing model does not price, so the cost "
            "is unchanged."
        )
    elif len(groups) == 1:
        attribution = ATTRIBUTION_SINGLE
        detail_text = "; ".join(
            f"{change.label} {change.before} → {change.after}" for change in priced_changes
        )
        note = (
            f"One input changed — {_GROUP_LABELS.get(groups[0], groups[0])} "
            f"({detail_text}) — so the whole difference is attributable to it."
        )
    else:
        attribution = ATTRIBUTION_MULTIPLE
        named = ", ".join(_GROUP_LABELS.get(group, group) for group in groups)
        note = f"{MULTIPLE_INPUTS_NOTE} Changed: {named}."

    return result.model_copy(
        update={
            "baseline_assumptions": before,
            "comparison_assumptions": after,
            "changes": changes,
            "impacts": impacts,
            "operations": operations,
            "sequence": sequence,
            "attribution": attribution,
            "attribution_note": note,
            "detailed": True,
        }
    )
