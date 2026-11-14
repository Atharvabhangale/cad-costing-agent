"""The one notice a whole-file analysis carries when the file is an assembly.

    CAD file ──→ cad_import (declares an assembly) ──→ main.py ──→ this notice

Every whole-file endpoint reads an upload as one shape. For a file that
declares an assembly that shape is all of its components together, so a wall,
a feature count, a modulus or a cost measured on it is true of the lump and of
no part. `/cad/import` has always said so (`assembly_not_a_part`); the analysis
responses did not, and their figures read like findings about a part.

This module only words the notice. It decides nothing: whether a file declares
an assembly is `cad_import.declares_assembly`, read from the same cheap
declaration `/cad/import` reports, and which endpoint notices and which refuses
is `main.py`'s. It imports nothing from this package, measures nothing and
reads no file.

Two forms, one code
-------------------
- **A notice** rides on a measurement that still ran. The figures are real
  figures about the lump, and a person may want them.
- **A refusal** (400: the format reads, this one analysis cannot be done on
  it) is for an analysis whose result on a lump is an artefact of the other
  components rather than a blurred figure: a ray along a pull axis or a tool
  approach is stopped by the neighbouring component, so an "undercut" or an
  "unreachable feature" there is not a property of any part.

A file that declares no assembly gets neither, and its responses carry no key
for it at all.
"""

from __future__ import annotations

CODE = "assembly_measured_as_one_body"

KEY = "assembly_notice"
"""The response key a notice travels under. Absent — not null — for a part."""

SEE_INSTEAD = (
    "Open the Assembly section and decompose the file: each component is then measured, "
    "planned and costed on its own shape, and the roll-up is the assembly's cost."
)

# What each analysis offers for one component, keyed by the analysis. `available`
# is whether a per-component route exists today; where it does not, the sentence
# says so rather than pointing somewhere that would not answer.
PER_COMPONENT: dict[str, tuple[bool, str]] = {
    "geometry": (
        True,
        "POST /cad/assembly measures every component definition on its own shape.",
    ),
    "mass": (
        True,
        "POST /cad/assembly measures every component definition on its own shape, and "
        "POST /cost/assembly reports each component's mass.",
    ),
    "features": (
        True,
        "POST /cad/assembly/features with a component_id recognizes one component's features; "
        "in the Assembly section, select the component.",
    ),
    "suggestions": (
        True,
        "POST /cad/assembly/plans suggests operations for each manufactured component on its "
        "own shape; the Assembly section builds those plans when the file is decomposed.",
    ),
    "dfm": (
        False,
        "There is no per-component DFM check yet. The flags below are evaluated on every "
        "component's faces together; a component's walls can be measured alone with "
        "component_id on POST /cad/wall-thickness.",
    ),
    "wall_thickness": (
        True,
        "Send component_id to this endpoint to measure one component's walls on its own shape.",
    ),
    "governing_wall": (
        False,
        "There is no per-component governing wall yet. A component's walls can be measured "
        "alone with component_id on POST /cad/wall-thickness.",
    ),
    "sheet_metal": (
        False,
        "There is no per-component sheet-metal measurement yet.",
    ),
    "casting_modulus": (
        False,
        "There is no per-component casting modulus yet. POST /cad/assembly reports each "
        "component's own volume and surface area.",
    ),
    "draw_direction": (
        True,
        "Send component_id to this endpoint to measure one component's pull direction, draft "
        "and undercuts on its own shape.",
    ),
    "cores": (
        False,
        "There is no per-component core proposal yet. A component's undercut faces can be "
        "measured alone with component_id on POST /cad/draw-direction.",
    ),
    "setups": (
        True,
        "Send component_id to propose setups for one component's plan on its own shape; the "
        "Assembly section does this when a component's plan is open.",
    ),
    "cost": (
        True,
        "POST /cost/assembly costs each component on its own and rolls them up; in the "
        "Assembly section, use Cost every component. This estimate prices the fused volume "
        "as one part, which answers a different question from the roll-up.",
    ),
}

REFUSAL_REASONS: dict[str, str] = {
    "draw_direction": (
        "A pull direction is not measured on an assembly read as one body: every ray along "
        "the pull is stopped by the neighbouring components, so nearly every face would be "
        "reported as an undercut that no single part has."
    ),
    "cores": (
        "Cores are not proposed on an assembly read as one body: they are grouped from "
        "undercut faces, and on fused components those are faces hidden by a neighbouring "
        "component, not undercuts of any part."
    ),
    "setups": (
        "Setups are not proposed across an assembly read as one body: a tool's approach would "
        "be judged blocked by neighbouring components, and one setup would hold operations of "
        "different components, which are never fixtured together."
    ),
}


def _counts(component_placements: int | None, product_definitions: int | None) -> str:
    if not component_placements:
        return "This file describes an assembly."
    text = f"This file describes an assembly of {component_placements:,} component placement"
    text += "" if component_placements == 1 else "s"
    if product_definitions:
        text += f" across {product_definitions:,} product definitions"
    return text + "."


def lump_notice(
    analysis: str,
    *,
    component_placements: int | None = None,
    product_definitions: int | None = None,
) -> dict:
    """The notice for one analysis of an assembly file, as a plain JSON-ready dict."""
    available, route = PER_COMPONENT[analysis]
    return {
        "code": CODE,
        "message": (
            f"{_counts(component_placements, product_definitions)} The figures below describe "
            "all of its components fused together as one body. They are not a finding about "
            "any part."
        ),
        "component_placements": component_placements or None,
        "product_definitions": product_definitions or None,
        "see_instead": SEE_INSTEAD,
        "per_component_available": available,
        "per_component": route,
    }


def lump_refusal(
    analysis: str,
    *,
    component_placements: int | None = None,
    product_definitions: int | None = None,
) -> dict:
    """The detail of a 400 for an analysis that is not run on an assembly as one body."""
    notice = lump_notice(
        analysis,
        component_placements=component_placements,
        product_definitions=product_definitions,
    )
    available, route = PER_COMPONENT[analysis]
    reason = REFUSAL_REASONS[analysis]
    notice["message"] = f"{_counts(component_placements, product_definitions)} {reason}"
    return {
        "error": CODE,
        "message": f"{notice['message']} {route} {SEE_INSTEAD}",
        KEY: notice,
    }


__all__ = ["CODE", "KEY", "PER_COMPONENT", "REFUSAL_REASONS", "lump_notice", "lump_refusal"]
