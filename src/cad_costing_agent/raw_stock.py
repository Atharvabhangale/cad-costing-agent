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

A sheet part is the second kind of stock (`SheetStock`, below): its material
is its share of the sheet it is nested on. The same contract holds — the
system proposes (a grid nest from `sheet_nesting`), the engineer confirms it or
enters their own parts per sheet beside it, and nothing changes until they do.

This module imports nothing but the feature models and the nest arithmetic.
It computes a stock's volume, never its mass or its price: density and price
per kilogram stay with the material in the rate set, and `/cost/cad-part`
hands the costing a volume.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, computed_field

from cad_costing_agent.features.model import Feature
from cad_costing_agent.features.rotational import rotational_form
from cad_costing_agent.features.sheet_metal import SheetMetalAnalysis
from cad_costing_agent.sheet_nesting import SheetNest, SheetSize, nest_blank, utilisation_percent

ROUND_BAR = "round_bar"
SHEET = "sheet"

# What `CadPartCostEstimateResult.material_basis` says for each kind of stock.
BAR_STOCK_BASIS = "bar_stock"
SHEET_STOCK_BASIS = "sheet_stock"


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


# ------------------------------------------------------------------------- sheet


def _mm(value: float) -> str:
    """A thickness as a sheet is named: 2.0 mm, 1.5 mm, 0.75 mm."""
    return f"{value:.1f}" if round(value, 1) == value else f"{value:g}"


class SheetStock(BaseModel):
    """A sheet part's share of the sheet it is nested on, proposed, and the engineer's answer.

    The share is the sheet's volume divided by the parts on it, so each part
    carries an equal part of the spacing, the margins and the skeleton. The
    blank's own area is never what is charged.
    """

    model_config = ConfigDict(frozen=True)

    shape: str = SHEET
    thickness_mm: float = Field(gt=0, description="The sheet's thickness: the part's measured one.")
    k_factor: float = Field(
        description="The K-factor the flat pattern, and so the blank, was unfolded for."
    )
    nest: SheetNest
    basis: list[str] = Field(default_factory=list)

    # The engineer's answer. `confirmed` is what makes the sheet count at all.
    confirmed: bool = False
    user_parts_per_sheet: int | None = Field(
        default=None,
        ge=1,
        description="Parts per sheet from the engineer's own nest, kept beside the estimate's.",
    )

    @computed_field
    @property
    def system_parts_per_sheet(self) -> int:
        return self.nest.parts_per_sheet

    @computed_field
    @property
    def parts_per_sheet(self) -> int:
        if self.user_parts_per_sheet is not None:
            return self.user_parts_per_sheet
        return self.nest.parts_per_sheet

    @computed_field
    @property
    def utilisation_percent(self) -> float:
        return utilisation_percent(
            self.nest.blank_net_area_mm2, self.parts_per_sheet, self.nest.sheet.area_mm2
        )

    @computed_field
    @property
    def volume_mm3(self) -> float:
        """The material one part consumes: sheet thickness x sheet area / parts per sheet."""
        return round(self.thickness_mm * self.nest.sheet.area_mm2 / self.parts_per_sheet, 3)

    @computed_field
    @property
    def source(self) -> str:
        """'system' while the estimate's nest stands, 'user' once parts per sheet were entered."""
        return "user" if self.user_parts_per_sheet is not None else "system"

    @computed_field
    @property
    def fits_sheet(self) -> bool:
        """Whether that many blanks' net area fits the sheet at all. More cannot be costed."""
        return self.utilisation_percent <= 100.0

    @computed_field
    @property
    def description(self) -> str:
        sheet = self.nest.sheet
        return (
            f"{_mm(self.thickness_mm)} mm sheet {sheet.sheet_length_mm:g} × "
            f"{sheet.sheet_width_mm:g}, {self.parts_per_sheet} per sheet"
            + (" (entered)" if self.source == "user" else "")
            + f", {self.utilisation_percent:.1f}% utilisation"
        )


def propose_sheet_stock(analysis: SheetMetalAnalysis, sheet: SheetSize) -> SheetStock:
    """The sheet share a measured sheet part would be costed from, unconfirmed.

    Raises ValueError, with the reason, for a part that is not a sheet, one with
    no flat pattern (no K-factor entered, or an unfold that is unsupported), or a
    blank that fits the sheet in neither orientation.
    """
    if analysis.status != SHEET or analysis.thickness_mm is None:
        reasons = " ".join(analysis.reasons) or f"its status is '{analysis.status}'."
        raise ValueError(
            "This part is not one /cad/sheet-metal calls a sheet, so it has no sheet share: "
            + reasons
        )
    flat = analysis.flat_pattern
    if flat is None:
        raise ValueError(
            "There is no flat pattern to nest: "
            + (analysis.flat_pattern_note or "enter the K-factor and measure the part again.")
        )
    nest = nest_blank(flat.rectangle_length_mm, flat.rectangle_width_mm, flat.net_area_mm2, sheet)
    chosen = next(o for o in nest.orientations if o.orientation == nest.chosen_orientation)
    return SheetStock(
        thickness_mm=analysis.thickness_mm,
        k_factor=flat.k_factor,
        nest=nest,
        basis=[
            f"flat blank {flat.rectangle_length_mm:g} × {flat.rectangle_width_mm:g} mm bounding "
            f"rectangle ({flat.rectangle_orientation}), {flat.net_area_mm2:g} mm² net of holes, "
            f"unfolded for the entered K = {flat.k_factor:g}",
            f"sheet {sheet.sheet_length_mm:g} × {sheet.sheet_width_mm:g} mm, {sheet.spacing_mm:g} mm "
            f"spacing, {sheet.edge_margin_mm:g} mm edge margin — all entered, none assumed",
            *(f"{o.label}: {o.working} → {o.parts_per_sheet} per sheet" for o in nest.orientations),
            f"uses {chosen.label}: more parts per sheet (0° on a tie) — the estimate's choice",
            f"utilisation {nest.utilisation_formula}",
            f"material per part = {_mm(analysis.thickness_mm)} mm × {nest.sheet.area_mm2:g} mm² / "
            f"{nest.parts_per_sheet} — the sheet's volume shared equally by the parts on it",
        ],
    )


def record_sheet_stock_decision(
    stock: SheetStock, action: str, *, parts_per_sheet: int | None = None
) -> SheetStock:
    """Confirm the estimate's nest, enter your own parts per sheet, or withdraw.

    The nest itself is never changed. Entering the estimate's own count is a
    confirmation, not a correction. A count whose blanks' net area exceeds the
    sheet is refused.
    """
    if action == "withdraw":
        return stock.model_copy(update={"confirmed": False, "user_parts_per_sheet": None})
    if action == "confirm":
        if parts_per_sheet is not None:
            raise ValueError("Parts per sheet may only be supplied with 'edit'.")
        return stock.model_copy(update={"confirmed": True, "user_parts_per_sheet": None})
    if action == "edit":
        if parts_per_sheet is None:
            raise ValueError("An edited nest needs the number of parts per sheet.")
        if (
            isinstance(parts_per_sheet, bool)
            or not float(parts_per_sheet).is_integer()
            or parts_per_sheet < 1
        ):
            raise ValueError("Parts per sheet must be a whole number, at least 1.")
        count = int(parts_per_sheet)
        user = None if count == stock.nest.parts_per_sheet else count
        edited = stock.model_copy(update={"confirmed": True, "user_parts_per_sheet": user})
        if not edited.fits_sheet:
            raise ValueError(
                f"{count} blanks of {stock.nest.blank_net_area_mm2:g} mm² need more than the "
                f"{stock.nest.sheet.area_mm2:g} mm² sheet ({edited.utilisation_percent:.1f}% "
                "utilisation)."
            )
        return edited
    raise ValueError(f"Unknown sheet stock action '{action}'. Use 'confirm', 'edit' or 'withdraw'.")


__all__ = [
    "BAR_STOCK_BASIS",
    "DEFAULT_RULE",
    "ROUND_BAR",
    "SHEET",
    "SHEET_STOCK_BASIS",
    "BarStock",
    "BarStockRule",
    "SheetStock",
    "propose_bar_stock",
    "propose_sheet_stock",
    "record_sheet_stock_decision",
    "record_stock_decision",
]
