"""Deterministic processing-time estimation. Times only — never money.

    features → suggested operations → **estimated setup/cycle time** → review → costing

This is the first module in the project that produces a *time*, and a time is a
much stronger claim than an operation: "this hole is drilled" is a fact about
geometry, while "it takes 1.8 minutes" is a claim about a machine, a tool, a
material and a shop. So everything here is arranged to make that claim weak,
visible and overridable:

- **Nothing is hidden.** Every estimate reports the formula that produced it,
  the geometry it consumed, and every parameter value it used. There are no
  unexplained constants: they all live in `TimeAssumptions`, which is a normal
  configurable model, and every one that affected a number is echoed back in
  `assumptions`.
- **Nothing is invented.** Where the feature does not carry the dimensions a
  formula needs — or where the file's units were never established — the result
  is `needs_user_input` with the missing pieces named. A default time is never
  substituted, because a time that appears without an explanation becomes money
  that appears without an explanation.
- **Nothing is confident.** A generic parameter baseline cannot justify `high`,
  so every estimate is capped at `medium` and drops to the confidence of the
  operation suggestion behind it when that is lower.
- **Nothing is committed.** Producing an estimate does not adopt it. Whether a
  number reaches costing is decided in `manufacturing_plan`, by a person.

**This module calculates no cost and imports no costing arithmetic.** Time
assumptions and rate sets are deliberately separate concerns: assumptions
determine duration, rates determine money, and mixing them would make a
shop-floor parameter silently change a price.

No AI, no ML, no learning, no randomness. The same inputs always give the same
times — a quotation has to be reproducible.

The formulas
------------
Textbook machining relationships, not a machining database. Each is one line
and each names its inputs.

Spindle speed, shared by all three processes::

    rpm = clamp(1000 · Vc / (π · D), min_rpm, max_rpm)

**Drilling** — one hole, feed per revolution along the axis::

    feed_rate = f_rev · rpm
    travel    = depth + approach + breakthrough (through holes only)
    cycle     = travel / feed_rate

**Milling** — a cutter sweeping an area, feed per tooth::

    feed_rate = f_tooth · teeth · rpm
    stepover  = cutter_diameter · stepover_ratio
    passes    = ceil(depth / axial_depth_of_cut)      (pockets and slots)
    path      = (area / stepover) · passes
    cycle     = (path + approach · passes) / feed_rate

Facing a plane is the same with a single pass and no depth. A fillet or chamfer
is a single pass along its length rather than over an area, because a blend is
finished by following it.

**Turning** — passes along a cylinder, feed per revolution::

    feed_rate = f_rev · rpm
    passes    = ceil(stock_allowance / depth_of_cut)
    cycle     = passes · (length + approach) / feed_rate

Setup time is a flat per-process figure, and is per *operation line* rather than
per physical setup — six drilled holes listed separately carry six setups. That
is stated in the assumptions rather than being quietly consolidated, because
whether two operations share a setup is a fixturing decision this module cannot
make.
"""

from __future__ import annotations

import math
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cad_costing_agent.features.model import Confidence

# Rounded to this many decimals before being reported, so repeated runs are
# byte-identical rather than differing in floating-point noise. A hundredth of a
# minute is 0.6 s — far finer than any of these estimates is actually good to.
TIME_DECIMALS = 2

# A generic parameter baseline can never justify `high`, however cleanly the
# geometry was measured: the numbers below describe a plausible shop, not the
# user's shop. This ceiling is what keeps the UI from asserting an exact
# machining time.
METHOD_CONFIDENCE_CEILING = Confidence.MEDIUM

_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


def _weakest(*levels: Confidence) -> Confidence:
    return min(levels, key=lambda level: _CONFIDENCE_RANK[level])


class TimeStatus(str, Enum):
    """Whether a time could be estimated at all."""

    ESTIMATED = "estimated"
    NEEDS_USER_INPUT = "needs_user_input"


class TimeAssumptions(BaseModel):
    """The cutting parameters the estimator runs on. Deliberately small.

    This is a baseline to be replaced by the user's own figures, not a machining
    database: one representative parameter set per process, each value named
    after the quantity it actually is. Everything here is **illustrative**, in
    exactly the sense the rate sets are — plausible for general steel machining
    on a modest CNC, and not a substitute for a shop's real feeds and speeds.

    Kept separate from `RateSet` on purpose. These decide how long something
    takes; rates decide what an hour costs. Letting one model carry both would
    mean editing a feed rate could silently change a price.
    """

    model_config = ConfigDict(frozen=True)

    is_illustrative: bool = Field(
        default=True,
        description=(
            "True for the built-in baseline. Estimates made with it carry a warning, exactly as "
            "illustrative rates do."
        ),
    )

    # --- drilling ---
    drilling_cutting_speed_m_per_min: float = Field(default=25.0, gt=0, le=2000.0)
    drilling_feed_mm_per_rev: float = Field(default=0.15, gt=0, le=10.0)
    drilling_approach_mm: float = Field(default=2.0, ge=0, le=1000.0)
    drilling_breakthrough_mm: float = Field(
        default=3.0, ge=0, description="Extra travel past a through hole to clear the drill point."
    )
    drilling_setup_minutes: float = Field(default=5.0, ge=0, le=10000.0)

    # --- milling ---
    milling_cutting_speed_m_per_min: float = Field(default=80.0, gt=0, le=2000.0)
    milling_feed_per_tooth_mm: float = Field(default=0.08, gt=0, le=10.0)
    milling_teeth: int = Field(default=4, gt=0, le=100)
    milling_cutter_diameter_mm: float = Field(default=12.0, gt=0, le=1000.0)
    milling_stepover_ratio: float = Field(
        default=0.6, gt=0, le=1.0, description="Fraction of the cutter diameter stepped over."
    )
    milling_axial_depth_of_cut_mm: float = Field(default=2.0, gt=0, le=500.0)
    milling_approach_mm: float = Field(default=5.0, ge=0, le=1000.0)
    milling_setup_minutes: float = Field(default=10.0, ge=0, le=10000.0)

    # --- turning ---
    turning_cutting_speed_m_per_min: float = Field(default=120.0, gt=0, le=2000.0)
    turning_feed_mm_per_rev: float = Field(default=0.2, gt=0, le=10.0)
    turning_depth_of_cut_mm: float = Field(default=1.5, gt=0, le=500.0)
    turning_stock_allowance_mm: float = Field(
        default=2.0, ge=0, description="Radial material to remove; decides the number of passes."
    )
    turning_approach_mm: float = Field(default=3.0, ge=0, le=1000.0)
    turning_setup_minutes: float = Field(default=12.0, ge=0, le=10000.0)

    # --- shared spindle limits ---
    #
    # These two are the one place the model deliberately *clamps* rather than
    # refusing: a machine really does have a top and bottom speed, so
    # 1000·Vc/(π·D) running away as D shrinks is physics meeting a real limit,
    # not bad input. Every other bound above is an upper guard against a typo —
    # a feed of 800 mm/rev is a slipped decimal point, not a machining choice —
    # and none of them silently changes a value the user did enter.
    min_spindle_rpm: float = Field(default=50.0, gt=0, le=200_000.0)
    max_spindle_rpm: float = Field(default=6000.0, gt=0, le=200_000.0)

    @model_validator(mode="after")
    def _check_spindle_range(self) -> "TimeAssumptions":
        if self.min_spindle_rpm >= self.max_spindle_rpm:
            raise ValueError(
                f"min_spindle_rpm ({self.min_spindle_rpm:g}) must be below max_spindle_rpm "
                f"({self.max_spindle_rpm:g}) — the clamp would otherwise pin every operation to "
                "a single speed."
            )
        return self


DEFAULT_ASSUMPTIONS = TimeAssumptions()

NEEDS_INPUT_NOTE = (
    "No time was estimated for this operation, so it cannot be costed until you enter one. "
    "Nothing is assumed."
)


class OperationTimeEstimate(BaseModel):
    """One deterministic time estimate, with everything that produced it.

    `setup_time_minutes` and `cycle_time_minutes` are `None` when
    `time_status` is `needs_user_input` — deliberately null rather than zero, so
    nothing downstream can add a missing time into a total by accident.
    """

    model_config = ConfigDict(frozen=True)

    time_status: TimeStatus
    estimated: bool = Field(description="True when a time was produced; mirrors `time_status`.")

    setup_time_minutes: float | None = None
    cycle_time_minutes: float | None = Field(
        default=None, description="Per part, before quantity is applied by costing."
    )
    total_time_minutes: float | None = Field(
        default=None,
        description=(
            "setup + cycle, for a single part. The batch total depends on quantity, which the "
            "existing costing applies — it is not multiplied in here."
        ),
    )

    confidence: Confidence | None = Field(
        default=None, description="Capped at medium; never higher than the operation suggestion."
    )
    method: str | None = Field(default=None, description="e.g. 'drilling_time_baseline_v1'.")
    reason: str = Field(description="One line: what was estimated, or what is missing.")
    evidence: list[str] = Field(
        default_factory=list, description="The geometry the formula consumed."
    )
    assumptions: list[str] = Field(
        default_factory=list, description="Every parameter value that affected the number."
    )
    missing: list[str] = Field(
        default_factory=list, description="Dimensions the formula needed but did not get."
    )
    is_illustrative: bool = Field(
        default=True, description="Whether the assumptions used were the illustrative baseline."
    )

    # --- provenance: which profile produced this number ---
    #
    # Recorded per estimate so a historical operation can answer "why 2.37
    # minutes?" on its own. Only the identity is carried here; the profile's
    # full contents are snapshotted once on the estimate rather than repeated on
    # every line.
    time_profile_id: str | None = None
    time_profile_version: str | None = None
    time_profile_name: str | None = None
    parameters: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "The parameter values that actually entered this calculation — the ones for this "
            "process plus the spindle limits, not the whole profile. `assumptions` says the same "
            "thing in prose; this says it in numbers a later reader can compute with."
        ),
    )


# --------------------------------------------------------------- the formulas


def _round(value: float) -> float:
    return round(value, TIME_DECIMALS)


def _spindle_rpm(cutting_speed_m_per_min: float, diameter_mm: float, a: TimeAssumptions) -> float:
    """rpm = 1000·Vc / (π·D), clamped to the machine's usable range."""
    raw = (1000.0 * cutting_speed_m_per_min) / (math.pi * diameter_mm)
    return min(max(raw, a.min_spindle_rpm), a.max_spindle_rpm)


def _assumptions_of(profile) -> TimeAssumptions:
    """The parameters a profile carries, or the built-in baseline."""
    if profile is None:
        return DEFAULT_ASSUMPTIONS
    return getattr(profile, "assumptions", None) or DEFAULT_ASSUMPTIONS


def _provenance(profile) -> dict[str, str | None]:
    """Identity fields for whichever profile supplied the parameters, if any."""
    return {
        "time_profile_id": getattr(profile, "time_profile_id", None),
        "time_profile_version": getattr(profile, "version", None),
        "time_profile_name": getattr(profile, "name", None),
    }


def _needs_input(
    reason: str, missing: list[str], illustrative: bool, profile=None
) -> OperationTimeEstimate:
    return OperationTimeEstimate(
        time_status=TimeStatus.NEEDS_USER_INPUT,
        estimated=False,
        reason=reason,
        missing=missing,
        is_illustrative=illustrative,
        **_provenance(profile),
    )


def _positive(dimensions: dict[str, float], key: str) -> float | None:
    value = dimensions.get(key)
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _drilling(
    dimensions: dict[str, float], subtype: str | None, a: TimeAssumptions
) -> tuple[float, str, list[str], list[str], dict[str, float]] | OperationTimeEstimate:
    diameter = _positive(dimensions, "diameter_mm")
    depth = _positive(dimensions, "depth_mm")
    missing = [name for name, value in [("diameter_mm", diameter), ("depth_mm", depth)] if value is None]
    if missing:
        return _needs_input(
            "Drilling time needs the hole's diameter and depth, and this feature does not "
            f"carry {' and '.join(missing)}. No time is assumed.",
            missing,
            a.is_illustrative,
        )

    rpm = _spindle_rpm(a.drilling_cutting_speed_m_per_min, diameter, a)
    feed_rate = a.drilling_feed_mm_per_rev * rpm

    # A through hole must clear the drill point on the far side; a blind hole
    # stops at depth. Anything whose ends were never classified is treated as
    # blind, which is the shorter of the two — stated in the assumptions rather
    # than silently rounded up.
    through = (subtype or "").startswith("through")
    breakthrough = a.drilling_breakthrough_mm if through else 0.0
    travel = depth + a.drilling_approach_mm + breakthrough
    cycle = travel / feed_rate

    evidence = [
        f"Ø{diameter:g} mm × {depth:g} mm deep"
        + (" through hole" if through else f" {subtype or 'hole'}"),
        f"spindle speed {rpm:.0f} rpm = 1000 × {a.drilling_cutting_speed_m_per_min:g} m/min "
        f"/ (π × {diameter:g} mm)",
        f"feed rate {feed_rate:.1f} mm/min = {a.drilling_feed_mm_per_rev:g} mm/rev × {rpm:.0f} rpm",
        f"drill travel {travel:g} mm = {depth:g} mm depth + {a.drilling_approach_mm:g} mm approach"
        + (f" + {a.drilling_breakthrough_mm:g} mm breakthrough" if through else ""),
    ]
    assumptions = [
        f"cutting speed {a.drilling_cutting_speed_m_per_min:g} m/min",
        f"feed {a.drilling_feed_mm_per_rev:g} mm/rev",
        f"approach allowance {a.drilling_approach_mm:g} mm",
        f"breakthrough allowance {a.drilling_breakthrough_mm:g} mm (through holes only)",
        f"spindle limited to {a.min_spindle_rpm:g}–{a.max_spindle_rpm:g} rpm",
    ]
    if not through:
        assumptions.append(
            "this hole's ends were not classified as through, so no breakthrough travel was added"
        )
    parameters = {
        "drilling_cutting_speed_m_per_min": a.drilling_cutting_speed_m_per_min,
        "drilling_feed_mm_per_rev": a.drilling_feed_mm_per_rev,
        "drilling_approach_mm": a.drilling_approach_mm,
        "drilling_breakthrough_mm": breakthrough,
        "drilling_setup_minutes": a.drilling_setup_minutes,
        "min_spindle_rpm": a.min_spindle_rpm,
        "max_spindle_rpm": a.max_spindle_rpm,
        "resolved_spindle_rpm": round(rpm, 3),
        "resolved_feed_rate_mm_per_min": round(feed_rate, 3),
    }
    return cycle, "drilling_time_baseline_v1", evidence, assumptions, parameters


def _milling_area(dimensions: dict[str, float]) -> tuple[float | None, str]:
    """The area a cutter has to sweep, and how it was determined."""
    for key, description in [
        ("floor_area_mm2", "floor area"),
        ("area_mm2", "face area"),
    ]:
        value = _positive(dimensions, key)
        if value is not None:
            return value, description
    length = _positive(dimensions, "length_mm")
    width = _positive(dimensions, "width_mm")
    if length is not None and width is not None:
        return length * width, f"{length:g} × {width:g} mm footprint"
    return None, ""


def _milling(
    dimensions: dict[str, float], feature_type: str | None, a: TimeAssumptions
) -> tuple[float, str, list[str], list[str], dict[str, float]] | OperationTimeEstimate:
    rpm = _spindle_rpm(a.milling_cutting_speed_m_per_min, a.milling_cutter_diameter_mm, a)
    feed_rate = a.milling_feed_per_tooth_mm * a.milling_teeth * rpm

    shared_parameters = {
        "milling_cutting_speed_m_per_min": a.milling_cutting_speed_m_per_min,
        "milling_feed_per_tooth_mm": a.milling_feed_per_tooth_mm,
        "milling_teeth": float(a.milling_teeth),
        "milling_cutter_diameter_mm": a.milling_cutter_diameter_mm,
        "milling_approach_mm": a.milling_approach_mm,
        "milling_setup_minutes": a.milling_setup_minutes,
        "min_spindle_rpm": a.min_spindle_rpm,
        "max_spindle_rpm": a.max_spindle_rpm,
        "resolved_spindle_rpm": round(rpm, 3),
        "resolved_feed_rate_mm_per_min": round(feed_rate, 3),
    }
    shared_assumptions = [
        f"cutting speed {a.milling_cutting_speed_m_per_min:g} m/min",
        f"feed {a.milling_feed_per_tooth_mm:g} mm/tooth × {a.milling_teeth} teeth",
        f"cutter diameter {a.milling_cutter_diameter_mm:g} mm",
        f"approach allowance {a.milling_approach_mm:g} mm",
        f"spindle limited to {a.min_spindle_rpm:g}–{a.max_spindle_rpm:g} rpm",
    ]

    # A blend is finished by following it, not by sweeping an area, so its
    # length is the path rather than one side of a rectangle.
    if feature_type in {"fillet", "chamfer"}:
        length = _positive(dimensions, "length_mm")
        if length is None:
            return _needs_input(
                f"Finishing a {feature_type} needs its length, which this feature does not "
                "carry. No time is assumed.",
                ["length_mm"],
                a.is_illustrative,
            )
        path = length + a.milling_approach_mm
        cycle = path / feed_rate
        evidence = [
            f"{feature_type} {length:g} mm long, finished in one pass along its length",
            f"spindle speed {rpm:.0f} rpm from a {a.milling_cutter_diameter_mm:g} mm cutter",
            f"feed rate {feed_rate:.1f} mm/min",
            f"tool path {path:g} mm = {length:g} mm + {a.milling_approach_mm:g} mm approach",
        ]
        return (
            cycle,
            "milling_blend_finishing_time_baseline_v1",
            evidence,
            [*shared_assumptions, "one finishing pass along the blend, no radial stepover"],
            {**shared_parameters, "passes": 1},
        )

    area, how = _milling_area(dimensions)
    if area is None:
        return _needs_input(
            "Milling time needs an area to sweep — a face area, a floor area, or a length and "
            "width — and this feature carries none. No time is assumed.",
            ["area_mm2"],
            a.is_illustrative,
        )

    stepover = a.milling_cutter_diameter_mm * a.milling_stepover_ratio

    # A pocket or slot is cut to depth in axial passes; a face is one pass.
    depth = _positive(dimensions, "depth_mm")
    if feature_type in {"pocket", "slot"}:
        if depth is None:
            return _needs_input(
                f"Milling a {feature_type} needs its depth to work out how many passes it takes, "
                "and this feature does not carry one. No time is assumed.",
                ["depth_mm"],
                a.is_illustrative,
            )
        passes = math.ceil(depth / a.milling_axial_depth_of_cut_mm)
        method = "milling_pocket_time_baseline_v1"
        depth_evidence = [
            f"{passes} axial pass(es) = ceil({depth:g} mm deep / "
            f"{a.milling_axial_depth_of_cut_mm:g} mm per pass)"
        ]
        depth_assumptions = [f"axial depth of cut {a.milling_axial_depth_of_cut_mm:g} mm"]
    else:
        passes = 1
        method = "milling_facing_time_baseline_v1"
        depth_evidence = ["a single facing pass; no depth of cut is implied by a planar face"]
        depth_assumptions = [
            "one pass — geometry does not say how much stock stands above this face"
        ]

    path = (area / stepover) * passes
    cycle = (path + a.milling_approach_mm * passes) / feed_rate

    evidence = [
        f"{how} {area:g} mm²",
        f"spindle speed {rpm:.0f} rpm from a {a.milling_cutter_diameter_mm:g} mm cutter",
        f"feed rate {feed_rate:.1f} mm/min = {a.milling_feed_per_tooth_mm:g} mm/tooth × "
        f"{a.milling_teeth} teeth × {rpm:.0f} rpm",
        f"stepover {stepover:g} mm = {a.milling_cutter_diameter_mm:g} mm × "
        f"{a.milling_stepover_ratio:g}",
        *depth_evidence,
        f"tool path {path:.0f} mm = ({area:g} mm² / {stepover:g} mm) × {passes} pass(es)",
    ]
    return (
        cycle,
        method,
        evidence,
        [*shared_assumptions, f"stepover {a.milling_stepover_ratio:g} × cutter diameter",
         *depth_assumptions],
        {
            **shared_parameters,
            "milling_stepover_ratio": a.milling_stepover_ratio,
            "resolved_stepover_mm": round(stepover, 3),
            "milling_axial_depth_of_cut_mm": a.milling_axial_depth_of_cut_mm,
            "passes": float(passes),
        },
    )


def _turning(
    dimensions: dict[str, float], a: TimeAssumptions
) -> tuple[float, str, list[str], list[str], dict[str, float]] | OperationTimeEstimate:
    diameter = _positive(dimensions, "diameter_mm")
    length = _positive(dimensions, "length_mm")
    missing = [
        name for name, value in [("diameter_mm", diameter), ("length_mm", length)] if value is None
    ]
    if missing:
        return _needs_input(
            "Turning time needs the cylinder's diameter and length, and this feature does not "
            f"carry {' and '.join(missing)}. No time is assumed.",
            missing,
            a.is_illustrative,
        )

    rpm = _spindle_rpm(a.turning_cutting_speed_m_per_min, diameter, a)
    feed_rate = a.turning_feed_mm_per_rev * rpm
    passes = max(1, math.ceil(a.turning_stock_allowance_mm / a.turning_depth_of_cut_mm))
    travel = length + a.turning_approach_mm
    cycle = passes * travel / feed_rate

    evidence = [
        f"Ø{diameter:g} mm × {length:g} mm long external cylinder",
        f"spindle speed {rpm:.0f} rpm = 1000 × {a.turning_cutting_speed_m_per_min:g} m/min "
        f"/ (π × {diameter:g} mm)",
        f"feed rate {feed_rate:.1f} mm/min = {a.turning_feed_mm_per_rev:g} mm/rev × {rpm:.0f} rpm",
        f"{passes} pass(es) = ceil({a.turning_stock_allowance_mm:g} mm radial stock / "
        f"{a.turning_depth_of_cut_mm:g} mm depth of cut)",
        f"travel per pass {travel:g} mm = {length:g} mm + {a.turning_approach_mm:g} mm approach",
    ]
    assumptions = [
        f"cutting speed {a.turning_cutting_speed_m_per_min:g} m/min",
        f"feed {a.turning_feed_mm_per_rev:g} mm/rev",
        f"depth of cut {a.turning_depth_of_cut_mm:g} mm",
        f"radial stock allowance {a.turning_stock_allowance_mm:g} mm — the CAD says nothing about "
        "the bar this is turned from, so the number of passes rests on this figure",
        f"approach allowance {a.turning_approach_mm:g} mm",
        f"spindle limited to {a.min_spindle_rpm:g}–{a.max_spindle_rpm:g} rpm",
    ]
    parameters = {
        "turning_cutting_speed_m_per_min": a.turning_cutting_speed_m_per_min,
        "turning_feed_mm_per_rev": a.turning_feed_mm_per_rev,
        "turning_depth_of_cut_mm": a.turning_depth_of_cut_mm,
        "turning_stock_allowance_mm": a.turning_stock_allowance_mm,
        "turning_approach_mm": a.turning_approach_mm,
        "turning_setup_minutes": a.turning_setup_minutes,
        "min_spindle_rpm": a.min_spindle_rpm,
        "max_spindle_rpm": a.max_spindle_rpm,
        "resolved_spindle_rpm": round(rpm, 3),
        "resolved_feed_rate_mm_per_min": round(feed_rate, 3),
        "passes": float(passes),
    }
    return cycle, "turning_time_baseline_v1", evidence, assumptions, parameters


_SETUP_MINUTES = {
    "drilling": lambda a: a.drilling_setup_minutes,
    "milling": lambda a: a.milling_setup_minutes,
    "turning": lambda a: a.turning_setup_minutes,
}


# ---------------------------------------------------------------- the entry point


def estimate_operation_time(
    *,
    operation_type: str,
    feature_type: str | None = None,
    feature_subtype: str | None = None,
    dimensions: dict[str, float] | None = None,
    units: str = "mm",
    suggestion_confidence: Confidence | None = None,
    assumptions: TimeAssumptions | None = None,
    profile=None,
) -> OperationTimeEstimate:
    """Estimate setup and cycle time for one operation. Deterministic.

    Takes the *normalized* description of an operation rather than a CAD file or
    a shape: recognition has already run, and re-deriving geometry here would
    both duplicate it and make an estimate cost seconds instead of microseconds.

    Returns `needs_user_input` — never a fallback number — when the operation
    type has no formula, when the file's units were never established, or when
    the geometry lacks a dimension the formula requires.

    `profile` is an optional `TimeProfileReference` recorded on the result so a
    historical estimate can say which published assumptions produced it. It is
    typed loosely on purpose: this module must not import the profile layer,
    because the profile layer imports `TimeAssumptions` from here. When a
    profile is given its `assumptions` are used unless explicit ones override.
    """
    # Explicit assumptions override a profile's — and then no profile produced
    # this number, so none is recorded against it. Attributing a prediction to
    # parameters it was not made with is worse than attributing it to nothing.
    if assumptions is not None:
        profile = None
    a = assumptions if assumptions is not None else _assumptions_of(profile)
    dimensions = dimensions or {}
    process = (operation_type or "").strip().lower()

    # Dimensions from an analytic STEP are normalized to millimetres by the
    # reader. A faceted export's are in whatever the file was written in, which
    # recognition reports as "as written in the file" — a diameter that might be
    # 10 mm or 0.01 m. Guessing between those is a factor-of-1000 error in a
    # time, so it is refused instead.
    if (units or "").strip().lower() != "mm":
        return _needs_input(
            f"This model's dimensions are '{units}', not millimetres, so a machining time "
            "cannot be derived from them without guessing the scale. Enter the times, or use a "
            "STEP file whose units the reader can normalise.",
            ["units"],
            a.is_illustrative,
            profile,
        )

    if process not in _SETUP_MINUTES:
        return _needs_input(
            f"There is no time formula for '{operation_type or 'this operation'}'. Times for it "
            "have to be entered rather than estimated.",
            ["cycle_time_minutes"],
            a.is_illustrative,
            profile,
        )

    if process == "drilling":
        outcome = _drilling(dimensions, feature_subtype, a)
    elif process == "turning":
        outcome = _turning(dimensions, a)
    else:
        outcome = _milling(dimensions, feature_type, a)

    if isinstance(outcome, OperationTimeEstimate):  # the formula declined
        return outcome.model_copy(update=_provenance(profile))

    cycle, method, evidence, formula_assumptions, parameters = outcome
    setup = _SETUP_MINUTES[process](a)

    confidence = (
        METHOD_CONFIDENCE_CEILING
        if suggestion_confidence is None
        else _weakest(suggestion_confidence, METHOD_CONFIDENCE_CEILING)
    )

    cycle = _round(cycle)
    setup = _round(setup)

    return OperationTimeEstimate(
        time_status=TimeStatus.ESTIMATED,
        estimated=True,
        setup_time_minutes=setup,
        cycle_time_minutes=cycle,
        total_time_minutes=_round(setup + cycle),
        confidence=confidence,
        method=method,
        reason=(
            f"{process.capitalize()} time derived from the feature's own dimensions and the "
            "configured cutting parameters."
        ),
        evidence=evidence,
        assumptions=[
            *formula_assumptions,
            f"setup {setup:g} min per operation line — six holes listed separately carry six "
            "setups, because whether they share one is a fixturing decision this model cannot make",
        ],
        is_illustrative=a.is_illustrative,
        parameters=parameters,
        **_provenance(profile),
    )


def estimate_for_suggestion(
    suggestion,
    *,
    units: str = "mm",
    assumptions: TimeAssumptions | None = None,
    profile=None,
) -> OperationTimeEstimate:
    """Estimate the time for a Step 19 `OperationSuggestion`.

    Uses the suggestion's *effective* process, so a user's edit is estimated
    rather than the process that was originally proposed. Typed loosely so this
    module does not depend on the suggestion layer.
    """
    return estimate_operation_time(
        operation_type=getattr(suggestion, "effective_operation_type", suggestion.operation_type),
        feature_type=suggestion.feature_type,
        feature_subtype=suggestion.feature_subtype,
        dimensions=suggestion.dimensions,
        units=units,
        suggestion_confidence=suggestion.confidence,
        assumptions=assumptions,
        profile=profile,
    )


__all__ = [
    "DEFAULT_ASSUMPTIONS",
    "METHOD_CONFIDENCE_CEILING",
    "NEEDS_INPUT_NOTE",
    "OperationTimeEstimate",
    "TimeAssumptions",
    "TimeStatus",
    "estimate_for_suggestion",
    "estimate_operation_time",
]


# ------------------------------------------------------- the batch request shape
#
# Estimation takes *normalized* operations, not a CAD file. Recognition has
# already run by the time anyone wants a time, and re-uploading the model to get
# one would mean parsing a 200 MB export again for an answer that costs
# microseconds. This is the shape `POST /operations/estimate-time` accepts.


class OperationTimeInput(BaseModel):
    """One operation to estimate, described the way the suggestion layer does."""

    operation_id: str = ""
    operation_type: str
    feature_id: str | None = None
    feature_type: str | None = None
    feature_subtype: str | None = None
    dimensions: dict[str, float] = Field(default_factory=dict)
    confidence: Confidence | None = Field(
        default=None, description="The operation suggestion's confidence, which caps the time's."
    )


class OperationTimeRequest(BaseModel):
    """A batch of operations plus the assumptions to estimate them under."""

    units: str = Field(
        default="mm",
        description=(
            "The units the dimensions are in. Anything but 'mm' yields needs_user_input rather "
            "than a guessed scale."
        ),
    )
    assumptions: TimeAssumptions | None = Field(
        default=None, description="Omit to use the illustrative baseline."
    )
    operations: list[OperationTimeInput] = Field(default_factory=list)


class OperationTimeResult(BaseModel):
    """One estimate, tied back to the operation and feature it was asked for."""

    operation_id: str
    feature_id: str | None = None
    operation_type: str
    estimate: OperationTimeEstimate


class OperationTimeResponse(BaseModel):
    units: str
    assumptions_used: TimeAssumptions
    time_profile_id: str | None = None
    time_profile_version: str | None = None
    time_profile_name: str | None = None
    estimates: list[OperationTimeResult]
    estimated_count: int = 0
    needs_input_count: int = 0
    warnings: list[str] = Field(default_factory=list)


def estimate_operations(request: OperationTimeRequest, *, profile=None) -> OperationTimeResponse:
    """Estimate a batch of operations. Pure, deterministic, and cheap.

    `profile` is a resolved `TimeProfile` whose assumptions and identity are
    used for every operation. Explicit `request.assumptions` still win, so an
    API caller can estimate against parameters they have not published — the
    result then carries no profile identity, which is the honest record of it.
    """
    assumptions = request.assumptions or _assumptions_of(profile)

    estimates = [
        OperationTimeResult(
            operation_id=operation.operation_id or f"OP-{index:03d}",
            feature_id=operation.feature_id,
            operation_type=operation.operation_type,
            estimate=estimate_operation_time(
                operation_type=operation.operation_type,
                feature_type=operation.feature_type,
                feature_subtype=operation.feature_subtype,
                dimensions=operation.dimensions,
                units=request.units,
                suggestion_confidence=operation.confidence,
                assumptions=request.assumptions,
                profile=profile,
            ),
        )
        for index, operation in enumerate(request.operations, start=1)
    ]

    estimated = sum(1 for result in estimates if result.estimate.estimated)
    needs_input = len(estimates) - estimated

    warnings = [
        "These are estimated times, not measured ones. They come from a generic parameter "
        "baseline and are capped at medium confidence — review every one before costing from it.",
        "Generating an estimate does not adopt it. A time reaches the cost only when you accept "
        "or enter it.",
    ]
    if assumptions.is_illustrative:
        warnings.append(
            "The cutting parameters used are ILLUSTRATIVE PLACEHOLDER VALUES, not your shop's "
            "feeds and speeds. Do not quote from times derived with them."
        )
    if request.assumptions is not None and profile is not None:
        warnings.append(
            "Explicit assumptions were supplied, so the selected time profile was not used and "
            "no profile is recorded against these times."
        )
    if needs_input:
        warnings.append(
            f"{needs_input} operation(s) could not be estimated and are marked needs_user_input. "
            "No time was assumed for them; enter one or they will not be costed."
        )

    return OperationTimeResponse(
        units=request.units,
        assumptions_used=assumptions,
        time_profile_id=None if request.assumptions is not None else getattr(profile, "time_profile_id", None),
        time_profile_version=None if request.assumptions is not None else getattr(profile, "version", None),
        time_profile_name=None if request.assumptions is not None else getattr(profile, "name", None),
        estimates=estimates,
        estimated_count=estimated,
        needs_input_count=needs_input,
        warnings=warnings,
    )


__all__ += [
    "OperationTimeInput",
    "OperationTimeRequest",
    "OperationTimeResponse",
    "OperationTimeResult",
    "estimate_operations",
]
