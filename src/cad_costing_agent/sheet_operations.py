"""The cut and bend lines a measured sheet part adds to its manufacturing plan.

    sheet measurement → **cut and bend operations, with estimated times** → review → costing

A sheet part's laser (or punch) cutting and its press-brake bending are not
recognized features: there is no hole or pocket to hang them on. They come from
the measurement itself (`features.sheet_metal`): one cutting operation for the
whole flat blank — its cut perimeter, its pierces, its thickness — and one
bending operation for all of its bends. Each is a system proposal like any
suggested operation:

- `system_suggested_operation` is the process, and `system_suggestion_rule`
  names the measurement rule that proposed it; `dimensions` carries the measured
  figures the time was estimated from, the K-factor among them.
- Its time is estimated under the selected time profile, and **not accepted**:
  `system_time_accepted` is false and stays false until a person accepts the
  estimate or types a time. Generating the lines commits nothing.
- Cutting and bending each get their own setup, because they run on different
  machines; each setup carries its process's setup time as a proposal, also
  unaccepted.

The ids are fixed (`OP-SHEET-CUT`, `OP-SHEET-BEND` and their setups), so
measuring again — with another K-factor, say — replaces the lines rather than
adding a second set, and a replaced line starts unaccepted again, because its
time was for a different blank.

This module estimates times and builds plan lines. It prices nothing and
imports no costing; `manufacturing_plan` stays the only bridge to money.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cad_costing_agent.features.model import Confidence
from cad_costing_agent.features.sheet_metal import SHEET, SheetMetalAnalysis
from cad_costing_agent.manufacturing_plan import ManufacturingSetup, PlannedOperation
from cad_costing_agent.operation_suggestions import SuggestionOrigin, UserAction
from cad_costing_agent.operation_time import TimeAssumptions, estimate_operation_time

CUT_OPERATION_ID = "OP-SHEET-CUT"
BEND_OPERATION_ID = "OP-SHEET-BEND"
CUT_SETUP_ID = "SETUP-SHEET-CUT"
BEND_SETUP_ID = "SETUP-SHEET-BEND"
CUT_RULE = "sheet_flat_pattern_cut_v1"
BEND_RULE = "sheet_bend_count_v1"
SHEET_OPERATION_IDS = (CUT_OPERATION_ID, BEND_OPERATION_ID)
SHEET_SETUP_IDS = (CUT_SETUP_ID, BEND_SETUP_ID)

SETUP_REASON = (
    "Cutting runs on a laser, plasma or punch and bending on a press brake: different machines, "
    "so each is its own setup."
)

# The measurement is exact B-Rep, but whether the part really is laser cut and
# bent is the route decision's, and a generic time baseline never justifies high.
SUGGESTION_CONFIDENCE = Confidence.MEDIUM


class SheetPlanLines(BaseModel):
    """The plan lines and setups for a measured sheet part, ready to add to a plan."""

    setups: list[ManufacturingSetup] = Field(default_factory=list)
    operations: list[PlannedOperation] = Field(default_factory=list)
    time_profile_id: str | None = None
    time_profile_version: str | None = None
    time_profile_name: str | None = None
    warnings: list[str] = Field(default_factory=list)


def _line(
    *,
    operation_id: str,
    setup_id: str,
    process: str,
    name: str,
    label: str,
    rule: str,
    dimensions: dict[str, float],
    profile,
    assumptions: TimeAssumptions | None,
) -> PlannedOperation:
    estimate = estimate_operation_time(
        operation_type=process,
        dimensions=dimensions,
        units="mm",
        suggestion_confidence=SUGGESTION_CONFIDENCE,
        assumptions=assumptions,
        profile=profile,
    )
    line = PlannedOperation(
        operation_id=operation_id,
        sequence_position=1,
        system_setup_id=setup_id,
        feature_ids=[],
        feature_label=label,
        operation_name=name,
        origin=SuggestionOrigin.SYSTEM,
        user_action=UserAction.PENDING,
        system_suggested_operation=process,
        system_suggestion_confidence=SUGGESTION_CONFIDENCE,
        system_suggestion_rule=rule,
        system_source_type="brep",
        dimensions=dimensions,
    )
    return line.with_estimate(estimate)


def _setup(setup_id: str, position: int, name: str, line: PlannedOperation) -> ManufacturingSetup:
    return ManufacturingSetup(
        setup_id=setup_id,
        sequence_position=position,
        name=name,
        process_context=line.system_suggested_operation,
        origin=SuggestionOrigin.SYSTEM,
        system_operation_ids=[line.operation_id],
        system_grouping="system_assumed",
        system_grouping_reason=SETUP_REASON,
        system_grouping_confidence=Confidence.LOW,
        system_grouping_assumptions=[SETUP_REASON],
        system_setup_time_minutes=line.system_suggested_setup_time,
        system_setup_time_source=(
            "largest_process_baseline" if line.system_suggested_setup_time is not None else None
        ),
    )


def sheet_plan_lines(
    analysis: SheetMetalAnalysis,
    *,
    profile=None,
    assumptions: TimeAssumptions | None = None,
) -> SheetPlanLines:
    """The cutting line (and the bending line, if the part has bends) for a sheet part.

    Raises ValueError for a part the measurement did not find to be a sheet.
    Without a flat pattern (no K-factor entered) the cutting line is still
    proposed, with its time needing input and the reason naming the K-factor.
    """
    if analysis.status != SHEET or analysis.thickness_mm is None:
        raise ValueError(
            "These lines come from a sheet-metal measurement, and this part was not measured as "
            f"a sheet ({analysis.status}): " + " ".join(analysis.reasons)
        )
    t = analysis.thickness_mm
    flat = analysis.flat_pattern

    cut_dimensions: dict[str, float] = {"thickness_mm": t, "pierce_count": float(analysis.pierce_count)}
    if flat is not None:
        cut_dimensions["cut_perimeter_mm"] = flat.cut_perimeter_mm
        cut_dimensions["k_factor"] = flat.k_factor
        cut_label = (
            f"Flat pattern (K {flat.k_factor:g}): {flat.cut_perimeter_mm:.2f} mm of cut, "
            f"{flat.pierce_count} pierce(s), {t:g} mm sheet"
        )
    else:
        cut_label = f"Flat pattern not worked out — enter a K-factor ({t:g} mm sheet)"
    cut = _line(
        operation_id=CUT_OPERATION_ID,
        setup_id=CUT_SETUP_ID,
        process="sheet_cutting",
        name="Cut the flat blank",
        label=cut_label,
        rule=CUT_RULE,
        dimensions=cut_dimensions,
        profile=profile,
        assumptions=assumptions,
    )
    operations = [cut]
    setups = [_setup(CUT_SETUP_ID, 1, "Sheet cutting", cut)]

    if analysis.bends:
        angles = ", ".join(f"{bend.angle_deg:g}°" for bend in analysis.bends)
        bend = _line(
            operation_id=BEND_OPERATION_ID,
            setup_id=BEND_SETUP_ID,
            process="bending",
            name=f"Bend ({len(analysis.bends)} bend{'s' if len(analysis.bends) != 1 else ''})",
            label=f"{len(analysis.bends)} bend(s): {angles}; {t:g} mm sheet",
            rule=BEND_RULE,
            dimensions={"bend_count": float(len(analysis.bends)), "thickness_mm": t},
            profile=profile,
            assumptions=assumptions,
        )
        operations.append(bend)
        setups.append(_setup(BEND_SETUP_ID, 2, "Press-brake bending", bend))

    warnings = [
        "These lines are proposals: their times are estimates and are not committed until you "
        "accept them or enter your own.",
        "Material is costed from the finished part's CAD mass unless a sheet share — the "
        "blank nested on your sheet — is confirmed in the sheet card; these lines do not change "
        "that.",
    ]
    if flat is None:
        warnings.append(
            "No K-factor was entered, so the flat pattern and the cut perimeter are unknown and "
            "the cutting time needs input."
        )
    estimate_of = cut
    return SheetPlanLines(
        setups=setups,
        operations=operations,
        time_profile_id=estimate_of.system_time_profile_id,
        time_profile_version=estimate_of.system_time_profile_version,
        time_profile_name=estimate_of.system_time_profile_name,
        warnings=warnings,
    )


__all__ = [
    "BEND_OPERATION_ID",
    "BEND_SETUP_ID",
    "CUT_OPERATION_ID",
    "CUT_SETUP_ID",
    "SHEET_OPERATION_IDS",
    "SHEET_SETUP_IDS",
    "SheetPlanLines",
    "sheet_plan_lines",
]
