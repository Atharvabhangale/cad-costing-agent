"""The one place upload bytes become an exact B-Rep shape for the feature layer.

    upload bytes → (cheap classification) → the single STEP / IGES reader → LoadedShape

Recognition, the wall and pull-direction measurements, the setup proposal and
the sheet-metal measurement all need the same thing first: the exact shape in
the file, or the reason there is none. Each used to decide that for itself —
the same gates for a faceted export, an IGES surface model and a triangle
mesh, written three times. This module decides it once.

**It decides; it does not word.** `load_exact_shape` returns a typed reason
(`NotExact`) and the evidence behind it (the STEP classification, the IGES
analysis, the assembly component). Every consumer keeps its own sentence,
because what a missing exact shape *means* differs: recognition falls back to
fitting polygons, a setup proposal is reported unavailable, sheet-metal
measurement is refused.

**It is not a parser.** STEP is parsed by `cad_geometry.read_step_shape`, IGES
by `cad_iges.read_iges_shape`, and an assembly's product structure by
`cad_assembly.read_step_assembly`. This is the only module of the feature
layer that calls them, and a structural test holds that. A call here reads the
file at most once, and not at all when the cheap classification already rules
an exact shape out.

It takes the format's canonical name and reads no file extension, measures
nothing and recognizes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.features.step_text import WIREFRAME, StepRepresentation, classify_step_bytes


class NotExact(str, Enum):
    """Why a file gave no exact analytic solid. A fact about the file, not a sentence."""

    FACETED_STEP = "faceted_step"
    """Flat polygons only (FACETED_BREP). Decided before the parse; no shape."""

    WIREFRAME_STEP = "wireframe_step"
    """Points or curves but no faces or facets. Decided before the parse; no shape."""

    IGES_SURFACE_MODEL = "iges_surface_model"
    """The Directory section declares no solid (entity 186). Reported only to a
    caller that asked for `analytic_solid_only`, and then before the parse; no shape."""

    IGES_NO_SHAPE = "iges_no_shape"
    """The IGES analysis kept no shape at all."""

    IGES_NO_FACES = "iges_no_faces"
    """The IGES file transferred curves or points but no surfaces. `shape` is what was read."""

    IGES_SEWN = "iges_sewn"
    """No solid came with the file; its loose surfaces were sewn. `shape` is the
    sewn result, whose cylinders are no longer analytic."""

    NOT_A_BREP_FORMAT = "not_a_brep_format"
    """Neither STEP nor IGES (a triangle mesh, or something unknown). Nothing is read."""

    UNKNOWN_COMPONENT = "unknown_component"
    """The assembly has no definition with the requested id."""

    COMPONENT_WITHOUT_GEOMETRY = "component_without_geometry"
    """The component exists but the reader transferred no geometry for it."""


@dataclass(frozen=True)
class LoadedShape:
    """What one upload gave the feature layer: a shape, or the reason there is none.

    `reason` is None exactly when `shape` is an exact analytic B-Rep. For two
    reasons a shape is still carried — `IGES_SEWN` and `IGES_NO_FACES` — because
    recognition has an answer for each (the planes of a surface model; a refusal
    worded from the scan) and must give it from the shape that was read, never a
    second read.
    """

    source_format: str
    shape: object | None = None
    reason: NotExact | None = None
    step_representation: StepRepresentation | None = None
    """The STEP classification, for a STEP file read whole."""
    iges: object | None = None
    """The `cad_iges.IgesTopology`, for an IGES file that was parsed."""
    component: object | None = None
    """The assembly definition, when a component was asked for and found."""

    @property
    def is_exact(self) -> bool:
        return self.reason is None and self.shape is not None


def load_exact_shape(
    data: bytes,
    source_format: str,
    *,
    component_id: str | None = None,
    analytic_solid_only: bool = False,
) -> LoadedShape:
    """Read an upload's exact B-Rep shape once, or say why it has none.

    `source_format` is the canonical format name (`"step"`, `"iges"`; anything
    else is `NOT_A_BREP_FORMAT`). `component_id` asks for one definition of a
    STEP assembly instead of the whole file, read through the product-structure
    reader.

    `analytic_solid_only` is for a caller with no use for an IGES surface model
    (the setup proposal): the Directory scan then refuses one before the unit
    guard and before the parse. Without it a surface model is parsed, because
    recognition reports its planes and names what is missing.

    The gates run in one order — classification, the format's own guards, the
    parse, the closure analysis. Raises ValueError only for what the readers
    and `cad_iges.ensure_readable` raise: content that is not the format, or
    an IGES file that declares no unit.
    """
    if component_id is not None:
        from cad_costing_agent.cad_assembly import read_step_assembly

        definition = read_step_assembly(data).definitions.get(component_id)
        if definition is None:
            return LoadedShape(source_format, reason=NotExact.UNKNOWN_COMPONENT)
        if not definition.has_geometry:
            return LoadedShape(
                source_format, reason=NotExact.COMPONENT_WITHOUT_GEOMETRY, component=definition
            )
        return LoadedShape(source_format, shape=definition.shape, component=definition)

    if source_format == "step":
        representation = classify_step_bytes(data)
        if representation.is_faceted:
            return LoadedShape(
                source_format, reason=NotExact.FACETED_STEP, step_representation=representation
            )
        if representation.representation == WIREFRAME:
            return LoadedShape(
                source_format, reason=NotExact.WIREFRAME_STEP, step_representation=representation
            )
        # An `empty` classification is not decided here: content that is not STEP
        # at all also has no entities, and the reader gives a better message for
        # that than a keyword count can.
        return LoadedShape(
            source_format, shape=read_step_shape(data), step_representation=representation
        )

    if source_format == "iges":
        from cad_costing_agent.cad_iges import (
            analyse_iges_shape,
            carries_a_solid,
            ensure_readable,
            read_iges_shape,
        )

        if analytic_solid_only and not carries_a_solid(data):
            return LoadedShape(source_format, reason=NotExact.IGES_SURFACE_MODEL)

        # The same two guards, in the same order, as geometry extraction applies.
        ensure_readable(data)
        iges = analyse_iges_shape(read_iges_shape(data))
        if iges.shape is None:
            reason = NotExact.IGES_NO_SHAPE
        elif iges.face_count == 0:
            reason = NotExact.IGES_NO_FACES
        elif iges.sewing_applied:
            reason = NotExact.IGES_SEWN
        else:
            reason = None
        return LoadedShape(source_format, shape=iges.shape, reason=reason, iges=iges)

    return LoadedShape(source_format, reason=NotExact.NOT_A_BREP_FORMAT)


__all__ = ["LoadedShape", "NotExact", "load_exact_shape"]
