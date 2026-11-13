"""The one-shot line a measured moulded or die-cast part adds to its manufacturing plan.

    governing wall (measured) + cavities (engineer) → **one moulding operation, timed** → review → costing
    volume and area (measured) + cavities (engineer) → **one die-casting operation, timed** → review → costing

An injection-moulded or die-cast part's machine time is not a recognized
feature: there is no hole or face to hang it on. It comes from a measurement —
for moulding the governing wall, the thickest region `features.wall_thickness`
found, which governs the cooling time; for die casting the casting modulus, the
part's exact volume over its surface area, which governs solidification by
Chvorinov's rule — and from the cavity count, which only the engineer knows. One
operation for the shot, like the sheet cut and bend lines (`sheet_operations`):

- `system_suggested_operation` is `injection_moulding` or `die_casting`, and
  `system_suggestion_rule` names the measurement rule that proposed it;
  `dimensions` carries the measured figures and the cavity count (and any core
  count the engineer confirmed) the time was estimated from. The material, which
  picks the profile's row, is in the line's label and in the estimate's recorded
  parameters.
- Its time is estimated under the selected profile and **not accepted**:
  `system_time_accepted` is false until a person accepts the estimate or types
  a time. Generating the line commits nothing.
- It has its own setup: the press or die-casting machine with its tool is a
  machine of its own.

The ids are fixed (`OP-MOULD`/`SETUP-MOULD`, `OP-DIECAST`/`SETUP-DIECAST`), so
generating again — for another cavity count, say — replaces the line rather
than adding a second, and the replaced line starts unaccepted, because its time
was for another tool.

A core count is only ever the engineer's confirmed one. The pull-direction
measurement proposes undercut regions; this module is never handed them, so a
proposal cannot become a time here.

This module estimates a time and builds a plan line. It prices nothing, imports
no costing and does not import any measurement: `main.py` measures and hands it
numbers. `manufacturing_plan` stays the only bridge to money.
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

DIE_CAST_OPERATION_ID = "OP-DIECAST"
DIE_CAST_SETUP_ID = "SETUP-DIECAST"
DIE_CAST_RULE = "die_casting_modulus_v1"
DIE_CAST_PROCESS = "die_casting"

SETUP_REASON = (
    "The moulding press with its mould is a machine of its own, so the shot is its own setup: "
    "hanging and heating the mould and proving the first shots, charged once."
)
DIE_CAST_SETUP_REASON = (
    "The die-casting machine with its die is a machine of its own, so the shot is its own setup: "
    "hanging and heating the die and proving the first shots, charged once."
)

# The measurement is on exact B-Rep, but whether the part really is moulded or
# die cast is the route decision's, and a generic time baseline never justifies high.
SUGGESTION_CONFIDENCE = Confidence.MEDIUM


class MouldingPlanLines(BaseModel):
    """The shot line and its setup, ready to add to a plan (moulding or die casting)."""

    setups: list[ManufacturingSetup] = Field(default_factory=list)
    operations: list[PlannedOperation] = Field(default_factory=list)
    time_profile_id: str | None = None
    time_profile_version: str | None = None
    time_profile_name: str | None = None
    warnings: list[str] = Field(default_factory=list)


def _count_words(count: int | None, one: str, many: str, absent: str) -> str:
    if count is None:
        return absent
    return f"{count} {one if count == 1 else many}"


def _shot_lines(
    *,
    process: str,
    operation_id: str,
    setup_id: str,
    rule: str,
    dimensions: dict[str, float],
    feature_label: str,
    operation_name: str,
    setup_name: str,
    setup_reason: str,
    material: str,
    profile,
    assumptions: TimeAssumptions | None,
    warnings: list[str],
) -> MouldingPlanLines:
    """One unaccepted shot operation in a setup of its own, timed under the profile."""
    estimate = estimate_operation_time(
        operation_type=process,
        dimensions=dimensions,
        units="mm",
        suggestion_confidence=SUGGESTION_CONFIDENCE,
        assumptions=assumptions,
        profile=profile,
        material=material,
    )
    line = PlannedOperation(
        operation_id=operation_id,
        sequence_position=1,
        system_setup_id=setup_id,
        feature_ids=[],
        feature_label=feature_label,
        operation_name=operation_name,
        origin=SuggestionOrigin.SYSTEM,
        user_action=UserAction.PENDING,
        system_suggested_operation=process,
        system_suggestion_confidence=SUGGESTION_CONFIDENCE,
        system_suggestion_rule=rule,
        system_source_type="brep",
        dimensions=dimensions,
    ).with_estimate(estimate)
    setup = ManufacturingSetup(
        setup_id=setup_id,
        sequence_position=1,
        name=setup_name,
        process_context=process,
        origin=SuggestionOrigin.SYSTEM,
        system_operation_ids=[line.operation_id],
        system_grouping="system_assumed",
        system_grouping_reason=setup_reason,
        system_grouping_confidence=Confidence.LOW,
        system_grouping_assumptions=[setup_reason],
        system_setup_time_minutes=line.system_suggested_setup_time,
        system_setup_time_source=(
            "largest_process_baseline" if line.system_suggested_setup_time is not None else None
        ),
    )
    return MouldingPlanLines(
        setups=[setup],
        operations=[line],
        time_profile_id=line.system_time_profile_id,
        time_profile_version=line.system_time_profile_version,
        time_profile_name=line.system_time_profile_name,
        warnings=warnings,
    )


def _check_counts(cavities: int | None, cores: int | None, tool: str) -> None:
    if cavities is not None and cavities < 1:
        raise ValueError(f"A {tool} has at least one cavity; got {cavities}.")
    if cores is not None and cores < 0:
        raise ValueError(f"A core count cannot be negative; got {cores}.")


def _core_dimension(dimensions: dict[str, float], cores: int | None) -> None:
    # Zero confirmed cores is the same shot as none: nothing is added.
    if cores:
        dimensions["cores"] = float(cores)


_PROPOSAL_WARNING = (
    "This line is a proposal: its time is an estimate and is not committed until you accept "
    "it or enter your own."
)
_NO_CAVITIES_WARNING = (
    "No cavity count was entered, so the time per part is unknown and needs input. None "
    "is assumed."
)


def _core_label(cores: int | None) -> str:
    return f", {_count_words(cores, 'core', 'cores', '')}" if cores else ""


def moulding_plan_lines(
    governing_wall_mm: float,
    *,
    material: str,
    cavities: int | None,
    profile=None,
    assumptions: TimeAssumptions | None = None,
    cores: int | None = None,
) -> MouldingPlanLines:
    """The one moulding line for a part whose governing wall was measured.

    Without a cavity count the line is still proposed, with its time needing
    input and the reason naming the cavities: no count is assumed, not even one.
    `cores` is a count the engineer confirmed; omitted, the shot has none.
    """
    if not governing_wall_mm or governing_wall_mm <= 0:
        raise ValueError("A moulding line needs a measured governing wall above 0 mm.")
    _check_counts(cavities, cores, "mould")
    dimensions: dict[str, float] = {"governing_wall_mm": float(governing_wall_mm)}
    if cavities is not None:
        dimensions["cavities"] = float(cavities)
    _core_dimension(dimensions, cores)
    shot = _count_words(cavities, "cavity", "cavities", "cavities not entered")
    warnings = [
        _PROPOSAL_WARNING,
        "The cooling time is governed by the thickest wall the measurement found. A thicker "
        "region between its sample points would cool longer than this.",
    ]
    if cavities is None:
        warnings.append(_NO_CAVITIES_WARNING)
    return _shot_lines(
        process=MOULD_PROCESS,
        operation_id=MOULD_OPERATION_ID,
        setup_id=MOULD_SETUP_ID,
        rule=MOULD_RULE,
        dimensions=dimensions,
        feature_label=f"Governing wall {governing_wall_mm:g} mm, {material}, {shot}{_core_label(cores)}",
        operation_name="Mould the part (one shot)",
        setup_name="Moulding press",
        setup_reason=SETUP_REASON,
        material=material,
        profile=profile,
        assumptions=assumptions,
        warnings=warnings,
    )


def die_casting_plan_lines(
    volume_mm3: float,
    surface_area_mm2: float,
    *,
    material: str,
    cavities: int | None,
    cores: int | None = None,
    profile=None,
    assumptions: TimeAssumptions | None = None,
) -> MouldingPlanLines:
    """The one die-casting line for a part whose volume and surface area were measured.

    Without a cavity count the line is still proposed, with its time needing
    input: no count is assumed, not even one. `cores` is a count the engineer
    confirmed; omitted, the shot has none.
    """
    if not volume_mm3 or volume_mm3 <= 0 or not surface_area_mm2 or surface_area_mm2 <= 0:
        raise ValueError("A die-casting line needs a measured volume and surface area above 0.")
    _check_counts(cavities, cores, "die")
    dimensions: dict[str, float] = {
        "casting_volume_mm3": float(volume_mm3),
        "casting_surface_area_mm2": float(surface_area_mm2),
    }
    if cavities is not None:
        dimensions["cavities"] = float(cavities)
    _core_dimension(dimensions, cores)
    shot = _count_words(cavities, "cavity", "cavities", "cavities not entered")
    modulus = volume_mm3 / surface_area_mm2
    warnings = [
        _PROPOSAL_WARNING,
        "The solidification time is Chvorinov's rule on the whole part: its volume over its "
        "whole surface area. A thick section inside a thin part solidifies later than this, and "
        "the biscuit, runners and overflows are not modelled.",
    ]
    if cavities is None:
        warnings.append(_NO_CAVITIES_WARNING)
    return _shot_lines(
        process=DIE_CAST_PROCESS,
        operation_id=DIE_CAST_OPERATION_ID,
        setup_id=DIE_CAST_SETUP_ID,
        rule=DIE_CAST_RULE,
        dimensions=dimensions,
        feature_label=(
            f"Casting modulus {modulus:.4g} mm (V/A), {material}, {shot}{_core_label(cores)}"
        ),
        operation_name="Die cast the part (one shot)",
        setup_name="Die-casting machine",
        setup_reason=DIE_CAST_SETUP_REASON,
        material=material,
        profile=profile,
        assumptions=assumptions,
        warnings=warnings,
    )


__all__ = [
    "DIE_CAST_OPERATION_ID",
    "DIE_CAST_PROCESS",
    "DIE_CAST_SETUP_ID",
    "MOULD_OPERATION_ID",
    "MOULD_PROCESS",
    "MOULD_SETUP_ID",
    "MouldingPlanLines",
    "die_casting_plan_lines",
    "moulding_plan_lines",
]
