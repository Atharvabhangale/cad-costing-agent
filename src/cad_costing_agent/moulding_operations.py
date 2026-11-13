"""The moulding line a measured part adds to its manufacturing plan.

    governing wall (measured) + cavities (engineer) → **one moulding operation, timed** → review → costing

An injection-moulded part's press time is not a recognized feature: there is no
hole or face to hang it on. It comes from a measurement — the governing wall,
the thickest region `features.wall_thickness` found, which governs the cooling
time — and from the cavity count, which only the engineer knows. One operation
for the shot, like the sheet cut and bend lines (`sheet_operations`):

- `system_suggested_operation` is `injection_moulding`, and
  `system_suggestion_rule` names the measurement rule that proposed it;
  `dimensions` carries the measured wall and the cavity count the time was
  estimated from. The material, which picks the profile's thermal row, is in
  the line's label and in the estimate's recorded parameters.
- Its time is estimated under the selected profile and **not accepted**:
  `system_time_accepted` is false until a person accepts the estimate or types
  a time. Generating the line commits nothing.
- It has its own setup: the press and the mould are a machine of their own.

The ids are fixed (`OP-MOULD`, `SETUP-MOULD`), so generating again — for
another cavity count, say — replaces the line rather than adding a second, and
the replaced line starts unaccepted, because its time was for another mould.

This module estimates a time and builds a plan line. It prices nothing, imports
no costing and does not import the wall measurement: `main.py` measures and
hands it a number. `manufacturing_plan` stays the only bridge to money.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cad_costing_agent.features.model import Confidence
from cad_costing_agent.manufacturing_plan import ManufacturingSetup, PlannedOperation
from cad_costing_agent.operation_suggestions import SuggestionOrigin, UserAction
from cad_costing_agent.operation_time import TimeAssumptions, estimate_operation_time

MOULD_OPERATION_ID = "OP-MOULD"
MOULD_SETUP_ID = "SETUP-MOULD"
MOULD_RULE = "moulding_governing_wall_v1"
MOULD_PROCESS = "injection_moulding"

SETUP_REASON = (
    "The moulding press with its mould is a machine of its own, so the shot is its own setup: "
    "hanging and heating the mould and proving the first shots, charged once."
)

# The wall is measured on exact B-Rep, but whether the part really is moulded is
# the route decision's, and a generic time baseline never justifies high.
SUGGESTION_CONFIDENCE = Confidence.MEDIUM


class MouldingPlanLines(BaseModel):
    """The moulding line and its setup, ready to add to a plan."""

    setups: list[ManufacturingSetup] = Field(default_factory=list)
    operations: list[PlannedOperation] = Field(default_factory=list)
    time_profile_id: str | None = None
    time_profile_version: str | None = None
    time_profile_name: str | None = None
    warnings: list[str] = Field(default_factory=list)


def moulding_plan_lines(
    governing_wall_mm: float,
    *,
    material: str,
    cavities: int | None,
    profile=None,
    assumptions: TimeAssumptions | None = None,
) -> MouldingPlanLines:
    """The one moulding line for a part whose governing wall was measured.

    Without a cavity count the line is still proposed, with its time needing
    input and the reason naming the cavities: no count is assumed, not even one.
    """
    if not governing_wall_mm or governing_wall_mm <= 0:
        raise ValueError("A moulding line needs a measured governing wall above 0 mm.")
    if cavities is not None and cavities < 1:
        raise ValueError(f"A mould has at least one cavity; got {cavities}.")
    dimensions: dict[str, float] = {"governing_wall_mm": float(governing_wall_mm)}
    if cavities is not None:
        dimensions["cavities"] = float(cavities)
    estimate = estimate_operation_time(
        operation_type=MOULD_PROCESS,
        dimensions=dimensions,
        units="mm",
        suggestion_confidence=SUGGESTION_CONFIDENCE,
        assumptions=assumptions,
        profile=profile,
        material=material,
    )
    shot = (
        f"{cavities} cavit{'y' if cavities == 1 else 'ies'}"
        if cavities is not None
        else "cavities not entered"
    )
    line = PlannedOperation(
        operation_id=MOULD_OPERATION_ID,
        sequence_position=1,
        system_setup_id=MOULD_SETUP_ID,
        feature_ids=[],
        feature_label=f"Governing wall {governing_wall_mm:g} mm, {material}, {shot}",
        operation_name="Mould the part (one shot)",
        origin=SuggestionOrigin.SYSTEM,
        user_action=UserAction.PENDING,
        system_suggested_operation=MOULD_PROCESS,
        system_suggestion_confidence=SUGGESTION_CONFIDENCE,
        system_suggestion_rule=MOULD_RULE,
        system_source_type="brep",
        dimensions=dimensions,
    ).with_estimate(estimate)
    setup = ManufacturingSetup(
        setup_id=MOULD_SETUP_ID,
        sequence_position=1,
        name="Moulding press",
        process_context=MOULD_PROCESS,
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
    warnings = [
        "This line is a proposal: its time is an estimate and is not committed until you accept "
        "it or enter your own.",
        "The cooling time is governed by the thickest wall the measurement found. A thicker "
        "region between its sample points would cool longer than this.",
    ]
    if cavities is None:
        warnings.append(
            "No cavity count was entered, so the time per part is unknown and needs input. None "
            "is assumed."
        )
    return MouldingPlanLines(
        setups=[setup],
        operations=[line],
        time_profile_id=line.system_time_profile_id,
        time_profile_version=line.system_time_profile_version,
        time_profile_name=line.system_time_profile_name,
        warnings=warnings,
    )


__all__ = [
    "MOULD_OPERATION_ID",
    "MOULD_PROCESS",
    "MOULD_SETUP_ID",
    "MouldingPlanLines",
    "moulding_plan_lines",
]
