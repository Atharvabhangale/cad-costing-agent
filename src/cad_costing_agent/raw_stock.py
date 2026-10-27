"""The bar a turned part is cut from: proposed from geometry, confirmed by a person.

A turned part's material is the bar it is cut from, not the finished part:
the chips are bought too. A Ø40 × 120 mm shaft weighs 0.67 kg but is cut from a
Ø45 × 127 mm bar of 1.59 kg. Costing material from the finished mass is right
for nothing that is machined from stock, and most wrong for a turned part.

What the geometry gives: the part is a body of revolution
(`features.rotational`), its largest diameter and its overall length. What
it does not give: the shop's bar sizes, its allowances and its parting
practice. So the proposal runs on a small rule, stated with every proposal and
ILLUSTRATIVE by default:

    bar Ø      = largest Ø + 2 × radial allowance, rounded up to the bar size step
    bar length = part length + facing allowance at each end + parting allowance

The engineer can confirm the proposed bar or enter the bar they actually use.
**Until they do, nothing changes**: material is costed from the finished
part's mass as it always was. The proposal never overwrites itself with the
engineer's answer; both are kept, as everywhere else in this codebase.

This module imports nothing but the feature models. It computes a bar's
volume, never its mass or its price: density and price per kilogram stay with
the material in the rate set, and `/cost/cad-part` hands the costing a volume.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, computed_field

from cad_costing_agent.features.model import Feature
from cad_costing_agent.features.rotational import rotational_form

ROUND_BAR = "round_bar"


class BarStockRule(BaseModel):
    """How a bar is sized from a turned part. ILLUSTRATIVE defaults; no standard behind them."""

    model_config = ConfigDict(frozen=True)

    radial_allowance_mm: float = Field(
        default=2.5, ge=0, le=100.0, description="Stock left on the largest diameter, per side."
    )
    bar_size_step_mm: float = Field(
        default=5.0, gt=0, le=100.0, description="Bar diameters are rounded up to a multiple of this."
    )
    facing_allowance_mm: float = Field(
        default=2.0, ge=0, le=100.0, description="Faced off each end of the part."
    )
    parting_allowance_mm: float = Field(
        default=3.0, ge=0, le=100.0, description="The width lost to parting the part off the bar."
    )
    is_illustrative: bool = True


DEFAULT_RULE = BarStockRule()


class BarStock(BaseModel):
    """A proposed bar and, beside it, the bar the engineer says they use."""

    model_config = ConfigDict(frozen=True)

    shape: str = ROUND_BAR
    part_max_diameter_mm: float
    part_length_mm: float
    system_diameter_mm: float
    system_length_mm: float
    rule: BarStockRule = DEFAULT_RULE
    basis: list[str] = Field(default_factory=list)

    # The engineer's answer. `confirmed` is what makes the bar count at all.
    confirmed: bool = False
    user_diameter_mm: float | None = Field(default=None, gt=0)
    user_length_mm: float | None = Field(default=None, gt=0)

    @computed_field
    @property
    def diameter_mm(self) -> float:
        return self.user_diameter_mm if self.user_diameter_mm is not None else self.system_diameter_mm

    @computed_field
    @property
    def length_mm(self) -> float:
        return self.user_length_mm if self.user_length_mm is not None else self.system_length_mm

    @computed_field
    @property
    def volume_mm3(self) -> float:
        return round(math.pi * (self.diameter_mm / 2.0) ** 2 * self.length_mm, 3)

    @computed_field
    @property
    def source(self) -> str:
        """'system' while the proposal stands, 'user' once a size was entered."""
        if self.user_diameter_mm is not None or self.user_length_mm is not None:
            return "user"
        return "system"

    @computed_field
    @property
    def fits_part(self) -> bool:
        """Whether the bar is at least as large as the part. A bar that is not cannot be costed."""
        return self.diameter_mm >= self.part_max_diameter_mm and self.length_mm >= self.part_length_mm


def propose_bar_stock(features: list[Feature], rule: BarStockRule = DEFAULT_RULE) -> BarStock | None:
    """The bar a rotational part would be turned from, or None if the part is not rotational."""
    form = rotational_form(features)
    if not form.observed or not form.max_diameter_mm or not form.length_mm:
        return None
    raw = form.max_diameter_mm + 2.0 * rule.radial_allowance_mm
    # Round up to the step, without a floating-point hair pushing 45.0 to 50.
    diameter = math.ceil(round(raw / rule.bar_size_step_mm, 9)) * rule.bar_size_step_mm
    length = form.length_mm + 2.0 * rule.facing_allowance_mm + rule.parting_allowance_mm
    return BarStock(
        part_max_diameter_mm=round(form.max_diameter_mm, 3),
        part_length_mm=round(form.length_mm, 3),
        system_diameter_mm=round(diameter, 3),
        system_length_mm=round(length, 3),
        rule=rule,
        basis=[
            form.detail,
            f"largest diameter Ø{form.max_diameter_mm:g} mm + 2 × {rule.radial_allowance_mm:g} mm "
            f"radial allowance = Ø{raw:g} mm, rounded up to a {rule.bar_size_step_mm:g} mm bar size "
            f"step: Ø{diameter:g} mm",
            f"overall length {form.length_mm:g} mm + {rule.facing_allowance_mm:g} mm facing at each "
            f"end + {rule.parting_allowance_mm:g} mm parting = {length:g} mm",
            "illustrative allowances, not the shop's — confirm the bar or enter the one you use",
        ],
    )


def record_stock_decision(
    stock: BarStock,
    action: str,
    *,
    diameter_mm: float | None = None,
    length_mm: float | None = None,
) -> BarStock:
    """Confirm the proposal, enter a bar, or withdraw. The proposal itself is never changed.

    `confirm` keeps the proposed size; `edit` takes a diameter and/or length;
    `withdraw` returns to finished-part material costing.
    """
    if action == "withdraw":
        return stock.model_copy(update={"confirmed": False, "user_diameter_mm": None, "user_length_mm": None})
    if action == "confirm":
        if diameter_mm is not None or length_mm is not None:
            raise ValueError("A size may only be supplied with 'edit'.")
        return stock.model_copy(update={"confirmed": True, "user_diameter_mm": None, "user_length_mm": None})
    if action == "edit":
        if diameter_mm is None and length_mm is None:
            raise ValueError("An edited bar needs a diameter, a length, or both.")
        for name, value in (("diameter", diameter_mm), ("length", length_mm)):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"The bar {name} must be a positive number of millimetres.")
        # Entering the proposed size again is a confirmation, not a correction.
        user_d = None if diameter_mm in (None, stock.system_diameter_mm) else round(diameter_mm, 3)
        user_l = None if length_mm in (None, stock.system_length_mm) else round(length_mm, 3)
        edited = stock.model_copy(
            update={"confirmed": True, "user_diameter_mm": user_d, "user_length_mm": user_l}
        )
        if not edited.fits_part:
            raise ValueError(
                f"A Ø{edited.diameter_mm:g} × {edited.length_mm:g} mm bar is smaller than the part "
                f"(Ø{stock.part_max_diameter_mm:g} × {stock.part_length_mm:g} mm)."
            )
        return edited
    raise ValueError(f"Unknown stock action '{action}'. Use 'confirm', 'edit' or 'withdraw'.")


__all__ = [
    "DEFAULT_RULE",
    "ROUND_BAR",
    "BarStock",
    "BarStockRule",
    "propose_bar_stock",
    "record_stock_decision",
]
