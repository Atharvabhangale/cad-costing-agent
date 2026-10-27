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
    run_cost           = unit_cost_per_part x quantity
    total_cost         = run_cost + batch_charge

`batch_charge` is one-off for the batch, like tooling, and is never multiplied
by quantity.

This module imports `money` and `rate_sets` and nothing else from the package.
It does not know what a route is; the caller hands it stage ids in route order.
"""

import math

from pydantic import BaseModel, Field

from cad_costing_agent.money import Money
from cad_costing_agent.rate_sets import PER_KG, RateSet

# Stages whose rate includes the part's material. When a route starts with one,
# the material line is not charged on top of it.
MATERIAL_SUPPLYING_STAGES = frozenset(
    {"casting", "forging", "moulding", "additive_build", "purchase"}
)

STAGE_COST_FORMULA = (
    "total_cost = unit_rate x (mass_kg if per_kg else 1) x quantity + batch_charge"
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
    mass_source: str | None = None
    mass_note: str = ""


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

        unit_cost: float | None = None
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
                unit_cost = rate.unit_rate * mass_kg
                if supplies_material:
                    blank_mass = mass_kg
            else:
                unit_cost = rate.unit_rate

        run_cost = (unit_cost or 0.0) * quantity
        lines.append(
            StageCostLine(
                stage=stage.stage,
                label=stage.label,
                unit_basis=rate.unit_basis,
                unit_rate=rate.unit_rate,
                mass_kg=mass_kg if rate.unit_basis == PER_KG else None,
                mass_source=mass_source,
                mass_note=mass_note,
                quantity=quantity,
                unit_cost_per_part=unit_cost,
                run_cost=run_cost,
                batch_charge=rate.batch_charge,
                total_cost=run_cost + rate.batch_charge,
                supplies_material=supplies_material,
                rate_note=rate.note,
            )
        )

    if problems:
        return StagePricing(problems=problems, material_supplied_by=material_label)
    return StagePricing(
        lines=lines,
        total=sum(line.total_cost for line in lines),
        material_supplied_by=material_label,
    )
