"""Pricing the non-machining stages of a decided route from the rate set's stage rates.

Casting, forging, sheet cutting, heat treatment, plating, a bought-out part:
none of these is priced by the hour, and nothing in this application can
estimate how long they take. So they are priced the way a supplier quotes them
— a rate per kg or per piece, and/or a one-off charge per batch for a pattern
or a die — from rates someone published in a rate set. Nothing here invents a
rate, a mass or a time.

What it refuses
---------------

Every stage must be priceable, or none is. A stage with no rate in the selected
rate set, or a per-kg stage with no mass entered for it, is returned as a
problem with its reason; the caller refuses the whole estimate. Pricing the
stages that can be priced and leaving the rest out would present part of the
cost as all of it — the same rule the route classification applies.

The mass a per-kg rate applies to is the one a person entered for that stage,
or — when none was entered — the finished part's own mass from the CAD model
(its measured volume x the material's density). Each line records which
(`mass_source`) and why (`mass_note`). The CAD mass is right for a part made as
formed; for a casting or forging machined afterwards the blank is heavier by
material the CAD file does not record, and the note says so rather than adding
a hidden allowance. An entered blank lighter than the finished part is refused,
because no blank is lighter than the part made from it.

Yield
-----

The stage that supplies the material may also carry a yield a person entered
(`yield_percent`, 0 < y <= 100): the share of the metal poured, or of the billet
bought, that ends up in the blank. The rest is gating, risers, flash and melt
loss, which a foundry or forge charges for and the CAD file does not record.
The per-kg rate is then charged on `base mass / (yield / 100)`. The base is the
same mass as without a yield — the blank's weight a person entered, or else the
finished part's CAD mass — so an entered mass and a yield are not alternatives:
the first says what the blank weighs, the second how much metal it took.

There is no default yield and nothing is assumed about one: absent, the line is
exactly what it was, down to its keys. With one, the line states the base mass
and where it came from, the yield, and the mass charged, and `mass_source` reads
`yield_applied`. A later per-kg step with no mass of its own (fettling) still
works on the blank, not on the metal poured. A yield on anything but the per-kg
stage that supplies the material is refused rather than ignored: a per-piece or
per-batch price has no mass to divide, and a finishing step pours nothing.

Tool amortisation
-----------------

A stage's one-off `batch_charge` (a pattern, a die) is charged whole to the
batch being estimated, unless a person states how many pieces the tool is
expected to make (`amortisation_quantity`). Then only this estimate's share is
charged: `batch_charge x quantity / amortisation_quantity`, unrounded. The line
states the full charge, the quantity it is spread over, the share charged, and
a note that the rest of the tool's cost is carried by pieces outside this
estimate. There is no default tool life and no annual volume is guessed:
absent, the line is exactly what it was, down to its keys. A quantity smaller
than this estimate's is refused — the stated tool would not make the pieces
being quoted, and a second tool is not assumed — and so is one on a stage whose
rate has no one-off charge to share.

Material
--------

A route that starts by casting, forging, moulding, building or buying the part
gets its material from that stage: a foundry's rate per kg includes the metal,
and a purchased part's price is the whole part. The material line is then not
charged separately, and the estimate says which stage carries it. Routes that
start any other way are unchanged.

Formulas, per stage
-------------------

    unit_cost_per_part = unit_rate x mass_kg     (per_kg)
                       = unit_rate               (per_piece)
    mass_kg            = base_mass_kg / (yield_percent / 100)   (only with a yield)
    run_cost           = unit_cost_per_part x quantity
    total_cost         = run_cost + batch_charge
    batch_charge       = full_batch_charge x quantity / amortisation_quantity
                                                 (only with an amortisation quantity)

`batch_charge` is one-off for the batch, like tooling, and is never multiplied
by quantity — except as the share of a tool amortised over a stated quantity.

This module imports `money` and `rate_sets` and nothing else from the package.
It does not know what a route is; the caller hands it stage ids in route order.
"""

import math

from pydantic import BaseModel, Field, model_serializer

from cad_costing_agent.money import Money, round_money
from cad_costing_agent.rate_sets import PER_KG, RateSet

# Stages whose rate includes the part's material. When a route starts with one,
# the material line is not charged on top of it.
MATERIAL_SUPPLYING_STAGES = frozenset(
    {"casting", "forging", "moulding", "additive_build", "purchase"}
)

# Stages an engineer may choose to price by time instead of by stage rate, and
# the hourly process type their operations are costed as: the stages with a
# time model in `operation_time` — welding (the weld formula), sheet cutting
# (cut perimeter and pierces from the flat pattern) and bending (the measured
# bend count). A timed stage is left out of `price_stages` — its cost comes
# from its operations — so it can never be charged twice.
#
# Fit-up (clamping and tacking) and weld finishing (grinding and cleaning) are
# timed by the same weld operations: each weld's `welding_tack_minutes_per_weld`
# and `weld_finishing_minutes`, when its profile states them. Each may be timed
# only together with welding — timing one alone would put the welds' whole
# time, arc included, beside a welding stage rate. Whether the welds actually
# carry that time is checked where the plan is costed.
TIMEABLE_STAGES = {
    "welding": "welding",
    "fit_up": "welding",
    "weld_finishing": "welding",
    "sheet_cutting": "sheet_cutting",
    "bending": "bending",
}
TIMED_ONLY_WITH = {"fit_up": "welding", "weld_finishing": "welding"}
STAGE_RATE_PRICING = "stage_rate"
TIMED_PRICING = "timed"


def timed_stages(stage_input_values: dict, route_stage_ids: list[str]) -> dict[str, str]:
    """Stage id → process type for every stage of this route the engineer chose to time.

    Reads each stage's `pricing` from the stage inputs. Absent is the stage rate.
    A stage not in this route is ignored (it belongs to a route being compared,
    not this one); asking to time a stage that has no time model is refused.
    """
    timed: dict[str, str] = {}
    for stage_id, values in (stage_input_values or {}).items():
        pricing = (values or {}).get("pricing", STAGE_RATE_PRICING) if isinstance(values, dict) else None
        if pricing not in (STAGE_RATE_PRICING, TIMED_PRICING):
            raise ValueError(
                f"Stage '{stage_id}' pricing must be '{STAGE_RATE_PRICING}' or '{TIMED_PRICING}'."
            )
        if pricing != TIMED_PRICING:
            continue
        if stage_id not in TIMEABLE_STAGES:
            raise ValueError(
                f"Stage '{stage_id}' cannot be priced by time: only welding, sheet cutting and "
                "bending have a time model (fit-up and weld finishing may be timed with welding, "
                "from each weld's tack and finishing time). It is priced from its stage rate."
            )
        if stage_id in route_stage_ids:
            timed[stage_id] = TIMEABLE_STAGES[stage_id]
    for stage_id, needs in TIMED_ONLY_WITH.items():
        if stage_id in timed and needs not in timed:
            raise ValueError(
                f"Stage '{stage_id}' can be priced by time only when '{needs}' is too: its time "
                f"is part of each weld's time, and with '{needs}' on its stage rate the welds' "
                "arc time would be charged beside that rate."
            )
    return timed


STAGE_COST_FORMULA = (
    "total_cost = unit_rate x (mass_kg if per_kg else 1) x quantity + batch_charge"
)
STAGE_COST_FORMULA_WITH_YIELD = (
    "total_cost = unit_rate x (base_mass_kg / (yield_percent / 100)) x quantity + batch_charge"
)

# `mass_source` of a line whose charged mass is a base mass divided by a yield.
YIELD_APPLIED = "yield_applied"

# Present on a line only when a yield was entered, so a line without one
# serializes exactly as it did before yields existed.
_YIELD_ONLY_KEYS = ("yield_percent", "base_mass_kg", "base_mass_source")

# `batch_charge_basis` of a line whose one-off charge is a share of a tool
# amortised over a quantity a person stated.
AMORTISED_SHARE = "amortised_share"
AMORTISED_BATCH_CHARGE = "full_batch_charge x quantity / amortisation_quantity"

# Present on a line only when an amortisation quantity was entered, so a line
# without one serializes exactly as it did before.
_AMORTISATION_ONLY_KEYS = (
    "full_batch_charge",
    "amortisation_quantity",
    "batch_charge_basis",
    "batch_charge_note",
)


class StageInput(BaseModel):
    """One stage to price, in route order, with anything a person entered for it."""

    stage: str = Field(min_length=1)
    label: str = Field(min_length=1)
    mass_kg: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        description=(
            "Per part, entered by a person. When absent, a per-kg stage uses the finished part's "
            "CAD mass and labels it."
        ),
    )
    yield_percent: float | None = Field(
        default=None,
        gt=0,
        le=100,
        allow_inf_nan=False,
        description=(
            "Entered by a person, never defaulted: the blank's mass as a percentage of the metal "
            "poured or billet bought. The per-kg rate is then charged on mass / (yield / 100). "
            "Only the per-kg stage that supplies the material takes one."
        ),
    )
    amortisation_quantity: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Entered by a person, never defaulted: how many pieces the pattern or die is expected "
            "to make. The stage's one-off charge is then charged as batch_charge x quantity / "
            "amortisation_quantity. Only a stage whose rate has a one-off charge takes one, and "
            "never one smaller than the quantity being estimated."
        ),
    )


class StageCostLine(BaseModel):
    stage: str
    label: str
    unit_basis: str | None = None
    unit_rate: float | None = None
    mass_kg: float | None = None
    quantity: int
    unit_cost_per_part: Money | None = None
    run_cost: Money = 0.0
    batch_charge: Money = 0.0
    total_cost: Money
    supplies_material: bool = False
    rate_note: str = ""
    formula: str = STAGE_COST_FORMULA
    # For a per-kg stage: where the mass came from — 'entered' by a person, or
    # 'cad_finished_mass' (the CAD part's own volume x density, used when none
    # was entered) — and what that means for this route.
    # With a yield it is 'yield_applied': `mass_kg` is then the mass charged,
    # `base_mass_kg` the mass it was derived from and `base_mass_source` where
    # that came from ('entered' or 'cad_finished_mass').
    mass_source: str | None = None
    mass_note: str = ""
    yield_percent: float | None = None
    base_mass_kg: float | None = None
    base_mass_source: str | None = None
    # With an amortisation quantity: `batch_charge` is the share charged to this
    # estimate, `full_batch_charge` the one-off charge as published, and the note
    # says the rest is carried by pieces outside this estimate.
    full_batch_charge: Money | None = None
    amortisation_quantity: int | None = None
    batch_charge_basis: str | None = None
    batch_charge_note: str | None = None

    @model_serializer(mode="wrap")
    def _without_an_absent_yield(self, handler):
        data = handler(self)
        if self.yield_percent is None:
            for key in _YIELD_ONLY_KEYS:
                data.pop(key, None)
        if self.amortisation_quantity is None:
            for key in _AMORTISATION_ONLY_KEYS:
                data.pop(key, None)
        return data


class StageProblem(BaseModel):
    stage: str
    label: str
    code: str = Field(description="'no_rate_set', 'no_stage_rate' or 'needs_mass'.")
    detail: str


class StagePricing(BaseModel):
    lines: list[StageCostLine] = Field(default_factory=list)
    problems: list[StageProblem] = Field(default_factory=list)
    total: Money = 0.0
    material_supplied_by: str | None = Field(
        default=None, description="Label of the stage whose rate includes the material."
    )

    @property
    def priceable(self) -> bool:
        return not self.problems


def material_supplying_stage(route_stage_ids: list[str]) -> str | None:
    """The route's first stage, if its rate includes the material. Else None."""
    if route_stage_ids and route_stage_ids[0] in MATERIAL_SUPPLYING_STAGES:
        return route_stage_ids[0]
    return None


def price_stages(
    stages: list[StageInput],
    rate_set: RateSet | None,
    quantity: int,
    *,
    material_stage: str | None = None,
    finished_mass_kg: float | None = None,
    route_machines: bool = False,
) -> StagePricing:
    """Price every stage, or report why each one that cannot be priced cannot be.

    `material_stage` is the stage id whose rate includes the material (see
    `material_supplying_stage`). `finished_mass_kg` is the CAD part's own mass:
    a per-kg stage with no entered mass is charged on it, labelled as such, and
    an entered blank lighter than it is refused. `route_machines` says the route
    machines the part afterwards, so a blank's real weight is above the CAD mass.

    A `yield_percent` on a stage that cannot take one raises `ValueError`: it is
    a contradiction in what was entered, not a stage waiting for a rate or a mass.
    So does an `amortisation_quantity` on a stage with no one-off charge, or one
    smaller than `quantity`.
    """
    if quantity < 1:
        raise ValueError(f"quantity must be at least 1, got {quantity}.")

    lines: list[StageCostLine] = []
    problems: list[StageProblem] = []
    material_label: str | None = None
    # The blank the forming stage produced (a casting, a forging): a per-kg step
    # after it with no mass of its own — fettling, trimming — works on that blank.
    blank_mass: float | None = None

    for stage in stages:
        supplies_material = stage.stage == material_stage
        if supplies_material:
            material_label = stage.label
        if rate_set is None:
            problems.append(
                StageProblem(
                    stage=stage.stage,
                    label=stage.label,
                    code="no_rate_set",
                    detail=f"{stage.label} is priced from a rate set's stage rates; select a rate set.",
                )
            )
            continue
        rate = rate_set.stage_rates.get(stage.stage)
        if rate is None:
            problems.append(
                StageProblem(
                    stage=stage.stage,
                    label=stage.label,
                    code="no_stage_rate",
                    detail=(
                        f"Rate set '{rate_set.rate_set_id}' v{rate_set.version} has no rate for "
                        f"{stage.label}. Publish a version with one."
                    ),
                )
            )
            continue

        takes_yield = (
            rate.unit_rate is not None and rate.unit_basis == PER_KG and supplies_material
        )
        if stage.yield_percent is not None and not takes_yield:
            if rate.unit_rate is None or rate.unit_basis != PER_KG:
                reason = (
                    f"rate set '{rate_set.rate_set_id}' v{rate_set.version} prices it "
                    f"{'per piece' if rate.unit_rate is not None else 'per batch only'}, so there "
                    "is no mass for a yield to apply to"
                )
            else:
                reason = (
                    "a yield belongs to the stage that supplies the material (the metal poured "
                    "or billet bought), and this stage does not"
                )
            raise ValueError(
                f"A yield of {stage.yield_percent:g}% was entered for {stage.label}, but "
                f"{reason}. Remove the yield."
            )

        if stage.amortisation_quantity is not None:
            if not rate.batch_charge:
                raise ValueError(
                    f"An amortisation quantity of {stage.amortisation_quantity} pieces was "
                    f"entered for {stage.label}, but rate set '{rate_set.rate_set_id}' "
                    f"v{rate_set.version} has no one-off batch charge for it, so there is no tool "
                    "charge to share. Remove the amortisation quantity."
                )
            if stage.amortisation_quantity < quantity:
                raise ValueError(
                    f"The {stage.label} one-off charge was to be amortised over "
                    f"{stage.amortisation_quantity} pieces, fewer than the {quantity} this "
                    "estimate is for: the stated tool would not make them all, and a second tool "
                    f"is not assumed. Enter at least {quantity}, or leave it blank to charge the "
                    "whole tool to this batch."
                )

        unit_cost: float | None = None
        yield_percent: float | None = None
        base_mass_kg: float | None = None
        base_mass_source: str | None = None
        formula = STAGE_COST_FORMULA
        mass_kg = stage.mass_kg
        mass_source: str | None = None
        mass_note = ""
        if rate.unit_rate is not None:
            if rate.unit_basis == PER_KG:
                if mass_kg is not None:
                    mass_source = "entered"
                    mass_note = "Mass entered by the engineer."
                elif blank_mass is not None and not supplies_material:
                    mass_kg = blank_mass
                    mass_source = "blank_mass"
                    mass_note = (
                        f"Same mass as the {material_label} blank, {blank_mass:.4g} kg — the part "
                        "this step works on. Edit it if the mass charged on differs."
                    )
                elif finished_mass_kg is not None:
                    # Derived, not invented: the finished part's own CAD volume x
                    # density. It is the right figure for a part made as formed;
                    # for one machined afterwards the real blank is heavier, by
                    # material the CAD file does not record, and the note says so.
                    mass_kg = finished_mass_kg
                    mass_source = "cad_finished_mass"
                    mass_note = f"Mass from the CAD model: the finished part, {finished_mass_kg:.4g} kg."
                    if supplies_material and route_machines:
                        mass_note += (
                            " This route machines the part afterwards, so the blank is heavier "
                            "by the material removed, which the CAD file does not record. Enter "
                            "the blank's weight to correct it."
                        )
                    else:
                        mass_note += " Edit it if the mass charged on differs."
                else:
                    problems.append(
                        StageProblem(
                            stage=stage.stage,
                            label=stage.label,
                            code="needs_mass",
                            detail=(
                                f"{stage.label} is priced per kg, and no mass was entered or "
                                "measured from the CAD model. Enter the mass it is charged on, "
                                "per part."
                            ),
                        )
                    )
                    continue
                if (
                    mass_source == "entered"
                    and supplies_material
                    and finished_mass_kg is not None
                    and mass_kg < finished_mass_kg
                    and not math.isclose(mass_kg, finished_mass_kg, rel_tol=1e-9)
                ):
                    problems.append(
                        StageProblem(
                            stage=stage.stage,
                            label=stage.label,
                            code="needs_mass",
                            detail=(
                                f"The {stage.label.lower()} mass entered ({mass_kg:g} kg) "
                                f"is less than the finished part ({finished_mass_kg:.4g} kg). A "
                                "blank cannot be lighter than the part made from it."
                            ),
                        )
                    )
                    continue
                if supplies_material:
                    # Before any yield: a later step works on the blank, not on
                    # the metal poured to make it.
                    blank_mass = mass_kg
                if stage.yield_percent is not None:
                    yield_percent = stage.yield_percent
                    base_mass_kg = mass_kg
                    base_mass_source = mass_source
                    mass_kg = base_mass_kg / (yield_percent / 100.0)
                    mass_source = YIELD_APPLIED
                    mass_note += (
                        f" Charged on {mass_kg:.4g} kg per part: that {base_mass_kg:.4g} kg "
                        f"divided by a yield of {yield_percent:g}%, entered by the engineer — the "
                        "share of the metal poured or billet bought that ends up in it. No yield "
                        "is assumed when none is entered."
                    )
                    formula = STAGE_COST_FORMULA_WITH_YIELD
                unit_cost = rate.unit_rate * mass_kg
            else:
                unit_cost = rate.unit_rate

        run_cost = (unit_cost or 0.0) * quantity
        batch_charge = rate.batch_charge
        amortisation: dict = {}
        if stage.amortisation_quantity is not None:
            # The share, unrounded: rounding happens only when the line is
            # serialized, so the total is the rounded exact value.
            batch_charge = rate.batch_charge * quantity / stage.amortisation_quantity
            currency = rate_set.currency
            amortisation = dict(
                full_batch_charge=rate.batch_charge,
                amortisation_quantity=stage.amortisation_quantity,
                batch_charge_basis=AMORTISED_SHARE,
                batch_charge_note=(
                    f"Amortised share of the tool: {currency} {round_money(batch_charge):,.2f} of "
                    f"the {currency} {round_money(rate.batch_charge):,.2f} one-off charge — this "
                    f"estimate's {quantity} of the {stage.amortisation_quantity} pieces the "
                    "engineer stated the pattern or die will make. The remaining "
                    f"{currency} {round_money(rate.batch_charge - batch_charge):,.2f} is carried "
                    "by pieces outside this estimate. No tool life is assumed when none is "
                    "entered."
                ),
            )
            formula = formula.replace("+ batch_charge", "+ " + AMORTISED_BATCH_CHARGE)
        lines.append(
            StageCostLine(
                stage=stage.stage,
                label=stage.label,
                unit_basis=rate.unit_basis,
                unit_rate=rate.unit_rate,
                mass_kg=mass_kg if rate.unit_basis == PER_KG else None,
                mass_source=mass_source,
                mass_note=mass_note,
                yield_percent=yield_percent,
                base_mass_kg=base_mass_kg,
                base_mass_source=base_mass_source,
                formula=formula,
                quantity=quantity,
                unit_cost_per_part=unit_cost,
                run_cost=run_cost,
                batch_charge=batch_charge,
                total_cost=run_cost + batch_charge,
                supplies_material=supplies_material,
                rate_note=rate.note,
                **amortisation,
            )
        )

    if problems:
        return StagePricing(problems=problems, material_supplied_by=material_label)
    return StagePricing(
        lines=lines,
        total=sum(line.total_cost for line in lines),
        material_supplied_by=material_label,
    )
