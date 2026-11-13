"""How many flat blanks fit on one sheet: a rectangular grid nest, estimated.

A sheet part is cut from a sheet, and what it consumes is its share of that
sheet — the blank, the spacing around it, the margin at the sheet's edge and
the skeleton left behind — not the finished part's own mass. This module
estimates the share with the plainest nest there is, and says so.

The method
----------
Each blank occupies its flat pattern's bounding rectangle (`b_L x b_W`, from
`features.sheet_metal` for the K-factor the engineer entered). The sheet is
`S_L x S_W`; the engineer states a part-to-part spacing `s` and an edge margin
`m`. Along one sheet direction with usable length `U = S - 2m`:

    count = floor((U + s) / (b + s))       (0 when b > U)

because `n` blanks need `n·b + (n - 1)·s` of it. Two orientations are tried —
0°, the blank's bounding-rectangle length along the sheet's length, and 90°,
turned a quarter — each a grid of rows and columns, and

    parts per sheet = count along x count across
    utilisation     = net flat area x parts per sheet / sheet area

with the net flat area the blank's own (holes removed). The estimate uses the
orientation with more parts per sheet, 0° on a tie, and says that is the
estimate's choice. A blank that fits in neither orientation is refused with
the reason.

What it is not
--------------
A bounding-rectangle grid estimate. It ignores true-shape nesting (an L nests
into another L), mixed orientations on one sheet, common-line cutting and
mixing other parts on the sheet, so a real nest usually uses the sheet
better than this. The remnant and offcut are charged to the parts nested: no
value is credited for a usable remnant or for scrap sold back. The sheet
size, spacing and margin are the engineer's; none has a default, because
they depend on what the shop buys and how it cuts.

This module imports nothing from the package. It is arithmetic on rectangles:
it reads no CAD, no rate, no density and no price.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

METHOD = "rectangular_grid_v1"

# Two lengths within this are equal, so a blank that fits the sheet exactly is
# not lost to floating-point noise. A micrometre — the flat pattern is reported
# to six decimals.
FIT_TOLERANCE_MM = 1e-6

ORIENTATION_0 = "0"
ORIENTATION_90 = "90"

CHOICE_NOTE = (
    "The estimate's choice: the orientation with more parts per sheet (0° on a tie). It is not a "
    "decision about how the shop nests — enter your own parts per sheet if your nest differs."
)

LIMITATIONS = (
    "A bounding-rectangle grid estimate: every blank takes its whole flat-pattern bounding "
    "rectangle, all in one orientation, in rows and columns. True-shape nesting, mixed "
    "orientations, common-line cutting and mixing other parts on the sheet are not modelled, so "
    "a real nest usually uses the sheet better than this.",
    "The remnant and offcut are charged to the parts nested: no value is credited for a usable "
    "remnant or for scrap sold back.",
    "Sheet size, part-to-part spacing and edge margin are the engineer's, entered for this "
    "estimate; none is assumed.",
)


class SheetSize(BaseModel):
    """The sheet and how blanks are laid on it. Every figure is the engineer's; none defaults."""

    model_config = ConfigDict(frozen=True)

    sheet_length_mm: float = Field(gt=0, allow_inf_nan=False)
    sheet_width_mm: float = Field(gt=0, allow_inf_nan=False)
    spacing_mm: float = Field(ge=0, allow_inf_nan=False, description="Between neighbouring blanks.")
    edge_margin_mm: float = Field(ge=0, allow_inf_nan=False, description="Kept clear at every sheet edge.")

    @property
    def area_mm2(self) -> float:
        return self.sheet_length_mm * self.sheet_width_mm


class NestOrientation(BaseModel):
    """One orientation of the grid: how many blanks along and across the sheet."""

    model_config = ConfigDict(frozen=True)

    orientation: Literal["0", "90"]
    label: str
    blank_along_mm: float = Field(description="The blank's extent along the sheet's length.")
    blank_across_mm: float = Field(description="The blank's extent along the sheet's width.")
    count_along: int
    count_across: int
    parts_per_sheet: int
    working: str = Field(description="The two counts, worked out in the formula's own terms.")


class SheetNest(BaseModel):
    """Both orientations, the one the estimate uses, and the utilisation it gives."""

    model_config = ConfigDict(frozen=True)

    method: str = METHOD
    blank_length_mm: float
    blank_width_mm: float
    blank_net_area_mm2: float
    sheet: SheetSize
    usable_length_mm: float
    usable_width_mm: float
    orientations: list[NestOrientation]
    chosen_orientation: Literal["0", "90"]
    parts_per_sheet: int
    utilisation_percent: float = Field(
        description="Net flat area x parts per sheet / sheet area, as a percentage."
    )
    utilisation_formula: str
    choice_note: str = CHOICE_NOTE
    limitations: list[str] = Field(default_factory=lambda: list(LIMITATIONS))


def _count(usable: float, blank: float, spacing: float) -> int:
    if blank > usable + FIT_TOLERANCE_MM:
        return 0
    return int(math.floor((usable + spacing + FIT_TOLERANCE_MM) / (blank + spacing)))


def utilisation_percent(net_area_mm2: float, parts_per_sheet: int, sheet_area_mm2: float) -> float:
    """Net flat area x parts / sheet area, as a percentage, to six decimals."""
    return round(100.0 * net_area_mm2 * parts_per_sheet / sheet_area_mm2, 6)


def nest_blank(
    blank_length_mm: float,
    blank_width_mm: float,
    blank_net_area_mm2: float,
    sheet: SheetSize,
) -> SheetNest:
    """Grid-nest one blank's bounding rectangle on a sheet, in both orientations.

    Raises ValueError, with the reason, when the blank fits in neither.
    """
    for name, value in (
        ("blank length", blank_length_mm),
        ("blank width", blank_width_mm),
        ("blank net area", blank_net_area_mm2),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"The {name} must be a positive number; the flat pattern gave {value}.")
    margin = sheet.edge_margin_mm
    usable_length = sheet.sheet_length_mm - 2.0 * margin
    usable_width = sheet.sheet_width_mm - 2.0 * margin
    if usable_length <= 0 or usable_width <= 0:
        raise ValueError(
            f"A {margin:g} mm edge margin on each side leaves nothing of a "
            f"{sheet.sheet_length_mm:g} × {sheet.sheet_width_mm:g} mm sheet to nest on."
        )
    s = sheet.spacing_mm
    orientations: list[NestOrientation] = []
    for orientation, along, across, label in (
        (ORIENTATION_0, blank_length_mm, blank_width_mm, "0° — blank length along the sheet length"),
        (ORIENTATION_90, blank_width_mm, blank_length_mm, "90° — blank length across the sheet"),
    ):
        count_along = _count(usable_length, along, s)
        count_across = _count(usable_width, across, s)
        orientations.append(
            NestOrientation(
                orientation=orientation,
                label=label,
                blank_along_mm=along,
                blank_across_mm=across,
                count_along=count_along,
                count_across=count_across,
                parts_per_sheet=count_along * count_across,
                working=(
                    f"along: floor(({usable_length:g} + {s:g}) / ({along:g} + {s:g})) = {count_along}; "
                    f"across: floor(({usable_width:g} + {s:g}) / ({across:g} + {s:g})) = {count_across}"
                ),
            )
        )
    best = orientations[0]
    if orientations[1].parts_per_sheet > best.parts_per_sheet:
        best = orientations[1]
    if best.parts_per_sheet == 0:
        raise ValueError(
            f"A {blank_length_mm:g} × {blank_width_mm:g} mm blank does not fit a "
            f"{sheet.sheet_length_mm:g} × {sheet.sheet_width_mm:g} mm sheet with a {margin:g} mm "
            f"edge margin ({usable_length:g} × {usable_width:g} mm usable) in either orientation."
        )
    percent = utilisation_percent(blank_net_area_mm2, best.parts_per_sheet, sheet.area_mm2)
    return SheetNest(
        blank_length_mm=blank_length_mm,
        blank_width_mm=blank_width_mm,
        blank_net_area_mm2=blank_net_area_mm2,
        sheet=sheet,
        usable_length_mm=usable_length,
        usable_width_mm=usable_width,
        orientations=orientations,
        chosen_orientation=best.orientation,
        parts_per_sheet=best.parts_per_sheet,
        utilisation_percent=percent,
        utilisation_formula=(
            f"{blank_net_area_mm2:g} mm² net flat area × {best.parts_per_sheet} / "
            f"{sheet.area_mm2:g} mm² sheet = {percent:.2f}%"
        ),
    )


__all__ = [
    "CHOICE_NOTE",
    "LIMITATIONS",
    "METHOD",
    "NestOrientation",
    "SheetNest",
    "SheetSize",
    "nest_blank",
    "utilisation_percent",
]
