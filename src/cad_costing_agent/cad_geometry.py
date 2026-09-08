"""Deterministic CAD geometry extraction.

This module is deliberately isolated from costing: it returns purely
geometric properties (volume, surface area, bounding box, source format) and
knows nothing about material density, material cost, machine rates, labor
rates, or any costing formula.

Supported formats:

- **STL** via `trimesh`. A triangulated mesh; volume and surface area are
  computed analytically from the triangles.
- **STEP** (`.step`/`.stp`) via `cadquery-ocp` (OpenCASCADE / OCCT). Volume and
  surface area come from `BRepGProp`, which integrates over the exact B-Rep
  faces rather than a mesh approximation, and the bounding box comes from
  `BRepBndLib.AddOptimal`, which is tight rather than triangulation-padded.
- **IGES** (`.iges`/`.igs`) via OCCT's separate IGES reader, measured the same
  way once closure has been established. IGES usually carries no solid, only
  loose trimmed surfaces, so `cad_iges` sews them and tests the result before
  any volume is reported — see that module for why that test is not optional.

Both readers are deterministic: the same input bytes always yield the same
output.

Unit semantics differ between the two formats, and this module reports which
case applies via `GeometryProperties.detected_unit`:

- STL embeds no unit information at all, so `detected_unit` is `None` and the
  caller must state the unit themselves downstream.
- IGES declares a unit in a fixed field of its Global section, which is read
  before anything else; a file that declares none is refused rather than read,
  because OCCT's reader silently defaults such a file to inches.
- STEP *does* carry unit metadata, and the OCCT reader reads it and converts
  the geometry to millimetres on load (its target unit, `xstep.cascade.unit`,
  defaults to `MM`). A STEP file authored in inches is therefore returned in
  millimetres. `detected_unit` is `"mm"` to record this, so nothing downstream
  has to guess.
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import trimesh
from pydantic import BaseModel, Field

STL_FORMATS = frozenset({"stl"})
STEP_FORMATS = frozenset({"step", "stp"})
IGES_FORMATS = frozenset({"iges", "igs"})
BREP_FORMATS = STEP_FORMATS | IGES_FORMATS
"""Formats that can carry exact surfaces, and so can be measured over real
geometry rather than over a tessellation of it."""

SUPPORTED_FORMATS = STL_FORMATS | STEP_FORMATS | IGES_FORMATS

# Extensions naming the same format report one canonical name.
_CANONICAL_FORMAT = {
    "stl": "stl",
    "step": "step",
    "stp": "step",
    "iges": "iges",
    "igs": "iges",
}

# OCCT converts STEP geometry to millimetres while reading it.
STEP_NORMALIZED_UNIT = "mm"

STL_UNITS_NOTE = (
    "Units match whatever length unit the source CAD file used; STL does not "
    "embed unit information, so no unit conversion is applied here."
)
STEP_UNITS_NOTE = (
    "STEP embeds unit information, and the OpenCASCADE reader converted this "
    "geometry to millimetres while loading it, so these values are in mm "
    "regardless of the unit the file was authored in."
)

_occ_console_silenced = False


class BoundingBoxDimensions(BaseModel):
    """Extents of the geometry's axis-aligned bounding box."""

    x: float
    y: float
    z: float


COMPLETE = "complete"
INCOMPLETE = "incomplete_requires_input"

TOTAL_VOLUME = "total_volume"
MISSING_VOLUME = "missing_volume"
UPLOAD_CORRECTED_CAD = "upload_corrected_cad"


class UnresolvedComponent(BaseModel):
    """One piece of a model that could not be measured.

    Faceted models fill in the polygon counts; a surface model that would not
    close fills in `face_count` and `free_edge_count` instead, and leaves the
    polygon fields at zero rather than reporting faces as though they were
    polygons.
    """

    index: int
    polygon_count: int = 0
    boundary_edge_count: int = 0
    non_manifold_edge_count: int = 0
    boundary_chain_count: int = 0
    face_count: int = Field(default=0, description="Exact surfaces, for a surface model.")
    free_edge_count: int = Field(
        default=0, description="Edges belonging to only one face, for a surface model."
    )


class IncompleteGeometry(BaseModel):
    """What is known, what is missing, and how the user can resolve it.

    Returned instead of a bare failure so a caller can act. Nothing here is a
    volume the system is willing to use on its own — `known_volume_mm3` is the
    watertight part only, and using it means the user has explicitly chosen to.
    """

    reason: str
    component_count: int
    closed_component_count: int
    open_component_count: int
    known_volume_mm3: float = Field(
        description=(
            "Volume of the watertight components alone. NOT the part's volume: the open "
            "components are unmeasured. Offered so the user can add the remainder."
        )
    )
    known_polygon_share: float = Field(
        description="Fraction of the model's polygons that lie in watertight components."
    )
    restored_facet_count: int = 0
    unresolved: list[UnresolvedComponent] = Field(default_factory=list)
    resolution_methods: list[str] = Field(
        default_factory=lambda: [TOTAL_VOLUME, MISSING_VOLUME, UPLOAD_CORRECTED_CAD]
    )
    explanation: str = ""


class GeometryCompletion(BaseModel):
    """A user's explicit assumption about geometry the file does not contain."""

    method: str = Field(description=f"'{TOTAL_VOLUME}' or '{MISSING_VOLUME}'.")
    volume_mm3: float = Field(gt=0, description="The volume the user is supplying, in mm³.")

    def validated(self) -> "GeometryCompletion":
        if self.method not in (TOTAL_VOLUME, MISSING_VOLUME):
            raise ValueError(
                f"Unknown geometry completion method '{self.method}'. Supported methods: "
                f"{TOTAL_VOLUME}, {MISSING_VOLUME}."
            )
        return self


class GeometryProperties(BaseModel):
    source_format: str
    volume: float
    surface_area: float
    bounding_box: BoundingBoxDimensions
    detected_unit: str | None = None
    units_note: str = STL_UNITS_NOTE
    geometry_source: str = Field(
        default="brep",
        description=(
            "Where the measurements came from: 'brep' for exact CAD surfaces, 'mesh' for "
            "polygons. A mesh volume is exact for the polygons it was given, but the polygons "
            "are themselves an approximation of the original surfaces."
        ),
    )
    volume_method: str = Field(
        default="brep_mass_properties",
        description=(
            "'brep_mass_properties' integrates over exact faces; 'mesh_signed_volume' sums the "
            "signed volume of a closed triangle mesh."
        ),
    )
    volume_confidence: str = Field(
        default="high",
        description=(
            "The geometry's confidence. 'high' when the volume came from geometry that was "
            "already sound; 'repaired' when facets the exporter omitted had to be restored "
            "first; 'unavailable' when no volume could be derived at all."
        ),
    )
    repair_applied: bool = Field(
        default=False, description="Whether any facet had to be restored before measuring."
    )
    repair_method: str | None = Field(
        default=None,
        description=(
            "Which repair ran. Only one exists: restoring facets whose absence leaves a "
            "three-edge boundary loop, where exactly one triangle can fit and nothing is chosen."
        ),
    )
    repaired_facet_count: int = Field(default=0, description="How many facets were restored.")
    repair_tolerance_mm: float | None = Field(
        default=None,
        description=(
            "The vertex-welding tolerance, in millimetres — the only tolerance involved in "
            "measuring a faceted model. Restoring a facet is purely topological and uses none."
        ),
    )
    geometry_status: str = Field(
        default=COMPLETE,
        description=(
            f"'{COMPLETE}' when the volume can be trusted as it stands. "
            f"'{INCOMPLETE}' when the file does not contain enough geometry to measure and "
            "the user must supply the missing information; `volume` is then 0 and `incomplete` "
            "says what is missing."
        ),
    )
    incomplete: IncompleteGeometry | None = Field(
        default=None, description="Present only when geometry_status is incomplete."
    )
    user_supplied_total_volume_mm3: float | None = Field(
        default=None, description="A total volume the user supplied in place of measurement."
    )
    user_supplied_missing_volume_mm3: float | None = Field(
        default=None, description="A volume the user added to the measured watertight part."
    )
    facet_count: int = Field(
        default=0, description="Polygons in a faceted model. Zero for analytic B-Rep."
    )
    closed_component_count: int = Field(
        default=0,
        description=(
            "Watertight connected components of a faceted mesh. Volume is only reported when "
            "every component is closed."
        ),
    )
    open_component_count: int = Field(
        default=0, description="Components with boundary or non-manifold edges."
    )


def _silence_occ_console() -> None:
    """Stop OCCT writing parser diagnostics straight to the console.

    Purely cosmetic — failures are still surfaced as exceptions — so a failure
    to silence it must never break extraction.
    """
    global _occ_console_silenced
    if _occ_console_silenced:
        return
    try:
        from OCP.Message import Message, Message_PrinterOStream

        Message.DefaultMessenger_s().RemovePrinters(Message_PrinterOStream.get_type_descriptor_s())
    except Exception:  # noqa: BLE001 - cosmetic only, never worth failing extraction over
        pass
    _occ_console_silenced = True


def _extract_stl_geometry(data: bytes) -> tuple[float, float, tuple[float, float, float]]:
    try:
        mesh = trimesh.load(io.BytesIO(data), file_type="stl")
    except Exception as exc:
        raise ValueError(f"Could not parse CAD file as 'stl': {exc}") from exc

    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.vertices) == 0:
        raise ValueError("CAD file does not contain a valid mesh.")

    extents = mesh.bounding_box.extents
    return (
        abs(float(mesh.volume)),
        float(mesh.area),
        (float(extents[0]), float(extents[1]), float(extents[2])),
    )


def read_step_shape(data: bytes):
    """Read STEP bytes into an OpenCASCADE shape.

    The single place STEP is parsed. Geometry extraction, viewer tessellation
    and feature recognition all start here, so there is one definition of what
    "a readable STEP file" means and one place where its failure modes are
    worded.

    The returned geometry is in millimetres: the reader honours the file's own
    unit metadata and converts on load.

    Raises ValueError for content that is not valid STEP or that carries no
    transferable geometry.
    """
    # Imported lazily: OCP loads large native libraries and costs seconds to
    # import, which STL-only callers should not have to pay at startup.
    try:
        from OCP.IFSelect import IFSelect_ReturnStatus
        from OCP.STEPControl import STEPControl_Reader
    except ImportError as exc:
        raise ValueError(
            "STEP support requires the 'cadquery-ocp' package, which is not installed."
        ) from exc

    _silence_occ_console()

    # OCCT's STEP reader reads from a path, not from a buffer.
    file_descriptor, temp_path = tempfile.mkstemp(suffix=".step")
    os.close(file_descriptor)
    try:
        Path(temp_path).write_bytes(data)

        reader = STEPControl_Reader()
        if reader.ReadFile(temp_path) != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise ValueError(
                "Could not parse CAD file as 'step': the content is not valid "
                "STEP (ISO 10303-21) data."
            )
        reader.TransferRoots()
        shape = reader.OneShape()
    finally:
        os.unlink(temp_path)

    if shape.IsNull():
        raise ValueError("STEP file does not contain any transferable geometry.")
    return shape


def measure_brep_shape(shape) -> tuple[float, float, tuple[float, float, float]]:
    """Volume, surface area and tight extents of an already-parsed B-Rep shape.

    The one implementation of "measure exact geometry", shared by whole-file
    STEP extraction and by a single component of a decomposed assembly. A
    component measured by a second copy of this arithmetic could disagree with
    the part it is, which is the kind of drift this codebase spends its
    structural tests preventing.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    volume_properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, volume_properties)

    surface_properties = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, surface_properties)

    volume = abs(float(volume_properties.Mass()))
    surface_area = float(surface_properties.Mass())

    bounding_box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, bounding_box)
    if bounding_box.IsVoid():
        # A shape the reader transferred nothing into. Asking a void box for its
        # corners raises from deep inside OpenCASCADE, which would replace the
        # caller's clear "this file yielded no surfaces" with a stack trace.
        return (volume, surface_area, (0.0, 0.0, 0.0))

    x_min, y_min, z_min, x_max, y_max, z_max = bounding_box.Get()

    return (
        volume,
        surface_area,
        (float(x_max - x_min), float(y_max - y_min), float(z_max - z_min)),
    )


def geometry_from_shape(shape, source_format: str = "step") -> GeometryProperties:
    """`GeometryProperties` for one already-parsed shape.

    Used to measure a single assembly component. It goes through
    `measure_brep_shape`, so a component's volume is produced by exactly the
    code that produces a part's.
    """
    volume, surface_area, extents = measure_brep_shape(shape)
    return GeometryProperties(
        source_format=source_format,
        volume=volume,
        surface_area=surface_area,
        bounding_box=BoundingBoxDimensions(x=extents[0], y=extents[1], z=extents[2]),
        detected_unit=STEP_NORMALIZED_UNIT,
        units_note=STEP_UNITS_NOTE,
        geometry_source="brep",
        volume_method="brep_mass_properties",
        volume_confidence="high",
    )


def _extract_step_geometry(data: bytes) -> tuple[float, float, tuple[float, float, float]]:
    try:
        from OCP.Bnd import Bnd_Box  # noqa: F401 - checked here so the message is right
        from OCP.BRepGProp import BRepGProp  # noqa: F401
    except ImportError as exc:
        raise ValueError(
            "STEP support requires the 'cadquery-ocp' package, which is not installed."
        ) from exc

    volume, surface_area, extents = measure_brep_shape(read_step_shape(data))

    # A shape with neither volume nor surface has no faces: the reader
    # transferred points or stray edges and nothing else. Returning zeroes here
    # would look like a successful measurement of an empty part, and the
    # failure would only surface much later as "volume must be positive" during
    # costing, by which point the cause is invisible.
    if volume <= 0.0 and surface_area <= 0.0:
        raise ValueError(
            "This STEP file was read but yielded no surfaces, so it has no measurable "
            "geometry. The usual cause is a faceted export whose FACETED_BREP entities are "
            "written over an OPEN_SHELL, which ISO 10303-42 forbids and OpenCASCADE therefore "
            "rejects. Re-export as solid/B-Rep geometry."
        )

    return (volume, surface_area, extents)


def extract_geometry_from_bytes(data: bytes, file_format: str) -> GeometryProperties:
    """Extract geometry properties from raw CAD file bytes (STL or STEP).

    Raises ValueError for an unsupported format, empty input, or content that
    cannot be parsed as valid geometry in the given format.
    """
    normalized_format = file_format.strip().lower().lstrip(".")
    if normalized_format not in SUPPORTED_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_FORMATS))
        raise ValueError(f"Unsupported CAD file format '{file_format}'. Supported formats: {supported}")

    if not data:
        raise ValueError("CAD file is empty.")

    if normalized_format in IGES_FORMATS:
        return _iges_geometry(data)

    if normalized_format in STEP_FORMATS:
        # Which representation the file uses decides how it can be measured at
        # all, so it is classified before anything is loaded.
        from cad_costing_agent.features.step_text import classify_step_bytes

        if classify_step_bytes(data).is_faceted:
            return _faceted_step_geometry(data, _CANONICAL_FORMAT[normalized_format])

        volume, surface_area, extents = _extract_step_geometry(data)
        detected_unit: str | None = STEP_NORMALIZED_UNIT
        units_note = STEP_UNITS_NOTE
        source, method = "brep", "brep_mass_properties"
    else:
        volume, surface_area, extents = _extract_stl_geometry(data)
        detected_unit = None
        units_note = STL_UNITS_NOTE
        source, method = "mesh", "mesh_signed_volume"

    return GeometryProperties(
        source_format=_CANONICAL_FORMAT[normalized_format],
        volume=volume,
        surface_area=surface_area,
        bounding_box=BoundingBoxDimensions(x=extents[0], y=extents[1], z=extents[2]),
        detected_unit=detected_unit,
        units_note=units_note,
        geometry_source=source,
        volume_method=method,
        volume_confidence="high",
    )


def _iges_geometry(data: bytes) -> GeometryProperties:
    """Measure an IGES file, after establishing that there is a solid to measure.

    The two-stage shape of this function is the whole point of it. IGES's usual
    export is loose trimmed surfaces with no solid above them, and
    `BRepGProp.VolumeProperties` returns a number for loose surfaces just as
    readily as for a solid — measured here, three faces of a box report
    10666.7 mm3, which is the volume of nothing. So the surfaces are sewn and
    tested, and a volume is reported only if that test passes. If it does not,
    the result is `incomplete_requires_input` with the surface area (which is
    real) and an explanation, exactly as a faceted STEP that does not close.
    """
    from cad_costing_agent.cad_iges import (
        SEWING_METHOD,
        analyse_iges_shape,
        ensure_readable,
        read_iges_shape,
    )

    # Refuses an unreadable file before the parse, and names which of the two
    # problems it has. See `cad_iges.ensure_readable`.
    unit = ensure_readable(data)
    topology = analyse_iges_shape(read_iges_shape(data))

    if topology.face_count == 0:
        raise ValueError(
            "This IGES file was read but yielded no surfaces, so it has no measurable "
            "geometry. IGES files holding only annotation, dimensions or drawing views "
            "contain no surfaces to measure. " + " ".join(topology.notes)
        )

    bounding_box = BoundingBoxDimensions(
        x=topology.bbox[0], y=topology.bbox[1], z=topology.bbox[2]
    )
    unit_sentence = (
        f"The file declares its length unit as {unit.name}, and the reader converted the "
        "geometry to millimetres while loading it, so these values are in mm regardless of "
        "the unit the file was authored in."
    )

    if not topology.has_measurable_volume:
        explanation = (
            " ".join(topology.notes)
            + " The part's volume is therefore not derivable from this file. Supply it, or "
            "re-export with solid geometry — in Creo, NX or SolidWorks that means choosing "
            "the solid or B-Rep option rather than the surface option, or exporting STEP "
            "instead, which always carries the solid."
        )
        return GeometryProperties(
            source_format="iges",
            volume=0.0,
            surface_area=topology.surface_area,
            bounding_box=bounding_box,
            detected_unit=STEP_NORMALIZED_UNIT,
            units_note=(
                f"{unit_sentence} Its surfaces do not enclose a solid, so no volume was "
                "derived from them."
            ),
            geometry_source="brep",
            volume_method="unavailable",
            volume_confidence="unavailable",
            geometry_status=INCOMPLETE,
            incomplete=IncompleteGeometry(
                reason="surfaces_do_not_enclose_a_solid",
                component_count=max(topology.shell_count, 1),
                closed_component_count=topology.closed_shell_count,
                open_component_count=max(topology.open_shell_count, 1),
                known_volume_mm3=0.0,
                known_polygon_share=0.0,
                unresolved=[
                    UnresolvedComponent(
                        index=0,
                        face_count=topology.face_count,
                        free_edge_count=topology.free_edge_count,
                    )
                ],
                explanation=explanation,
            ),
        )

    return GeometryProperties(
        source_format="iges",
        volume=topology.volume,
        surface_area=topology.surface_area,
        bounding_box=bounding_box,
        detected_unit=STEP_NORMALIZED_UNIT,
        units_note=(
            f"{unit_sentence} {' '.join(topology.notes)}"
        ),
        geometry_source="brep",
        volume_method="brep_mass_properties",
        volume_confidence="repaired" if topology.sewing_applied else "high",
        repair_applied=topology.sewing_applied,
        repair_method=SEWING_METHOD if topology.sewing_applied else None,
        repair_tolerance_mm=topology.sewing_tolerance_mm,
    )


def _faceted_step_geometry(data: bytes, source_format: str) -> GeometryProperties:
    """Measure a faceted STEP file from its polygons.

    An `OPEN_SHELL` in the STEP text says nothing about whether the polygons
    form a closed surface — in the real Creo exports the "open shells" are face
    groups, and welding them reveals far fewer, mostly watertight, components.
    So closure is decided from the topology, never from the entity name.

    A volume is only reported when **every** connected component is watertight.
    Summing the closed ones and ignoring the rest would quietly price a
    fraction of the model, which is worse than refusing.
    """
    from cad_costing_agent import limits
    from cad_costing_agent.cad_faceted import (
        REPAIR_METHOD,
        analyse_faceted_volume,
        detect_length_unit,
    )

    unit = detect_length_unit(data)
    if unit is None:
        raise ValueError(
            "This faceted STEP file does not declare a length unit that could be read, so its "
            "polygon coordinates cannot be converted to millimetres. Guessing would scale every "
            "mass by a factor of a thousand, so no geometry is reported."
        )

    topology = analyse_faceted_volume(data, max_facets=limits.MAX_GEOMETRY_FACETS)
    if not topology.components:
        raise ValueError(
            "No polygons could be read from this faceted STEP file, so it has no measurable "
            "geometry."
        )

    scale = unit.millimetres_per_unit
    closed = len(topology.closed)
    opened = len(topology.open)

    extents = tuple(
        (high - low) * scale for low, high in zip(topology.bbox_min, topology.bbox_max)
    )

    if not topology.is_watertight:
        # Not an error: a structured statement of what is known and what is
        # missing, so the caller can ask the user rather than give up. The
        # volume stays 0 — the watertight part alone is never presented as the
        # part's volume, and anything that ignores `geometry_status` fails loudly
        # downstream instead of costing a fraction of the model.
        share = topology.closed_triangle_count / max(topology.triangle_count, 1)
        worst = max(topology.open, key=lambda component: component.triangle_count)
        restored = (
            f"Restoring {topology.restored_facet_count:,} facet(s) whose absence left a "
            "three-edge hole was not enough. "
            if topology.restored_facet_count
            else ""
        )
        explanation = (
            restored
            + f"Welding this model's {topology.triangle_count:,} polygons gives "
            f"{len(topology.components):,} connected components: {closed:,} are watertight "
            f"(holding {share:.0%} of the polygons) and {opened:,} are not. The largest "
            f"unmeasurable component has {worst.triangle_count:,} polygons with "
            f"{worst.boundary_edge_count:,} boundary edges in {worst.boundary_chain_count:,} "
            "separate chains"
            + (
                f" and {worst.non_manifold_edge_count:,} edges shared by more than two polygons"
                if worst.non_manifold_edge_count
                else ""
            )
            + ". What is left are openings too large to be a single missing facet — filling "
            "those would mean inventing surface — together with edges where three or four "
            "polygons meet, which no fill can resolve. The part's volume is therefore not in "
            "this file. Supply it, or re-export as solid B-Rep geometry."
        )
        return GeometryProperties(
            source_format=source_format,
            volume=0.0,
            surface_area=topology.surface_area * (scale**2),
            bounding_box=BoundingBoxDimensions(x=extents[0], y=extents[1], z=extents[2]),
            detected_unit=STEP_NORMALIZED_UNIT,
            units_note=(
                f"Faceted STEP export, coordinates in {unit.name}s converted to millimetres. "
                "Its polygons do not enclose a solid, so no volume was derived from them."
            ),
            geometry_source="mesh",
            volume_method="unavailable",
            volume_confidence="unavailable",
            facet_count=topology.triangle_count,
            closed_component_count=closed,
            open_component_count=opened,
            repair_applied=bool(topology.restored_facet_count),
            repair_method=REPAIR_METHOD if topology.restored_facet_count else None,
            repaired_facet_count=topology.restored_facet_count,
            repair_tolerance_mm=topology.weld_tolerance * scale,
            geometry_status=INCOMPLETE,
            incomplete=IncompleteGeometry(
                reason="polygons_do_not_enclose_a_solid",
                component_count=len(topology.components),
                closed_component_count=closed,
                open_component_count=opened,
                known_volume_mm3=topology.volume * (scale**3),
                known_polygon_share=share,
                restored_facet_count=topology.restored_facet_count,
                unresolved=[
                    UnresolvedComponent(
                        index=component.index,
                        polygon_count=component.triangle_count,
                        boundary_edge_count=component.boundary_edge_count,
                        non_manifold_edge_count=component.non_manifold_edge_count,
                        boundary_chain_count=component.boundary_chain_count,
                    )
                    for component in sorted(
                        topology.open, key=lambda c: c.triangle_count, reverse=True
                    )[:20]
                ],
                explanation=explanation,
            ),
        )

    volume = topology.volume * (scale**3)
    surface_area = topology.surface_area * (scale**2)

    return GeometryProperties(
        source_format=source_format,
        volume=volume,
        surface_area=surface_area,
        bounding_box=BoundingBoxDimensions(x=extents[0], y=extents[1], z=extents[2]),
        detected_unit=STEP_NORMALIZED_UNIT,
        units_note=(
            f"This is a faceted STEP export with no exact surfaces. Its coordinates are in "
            f"{unit.name}s, declared by the file, and have been converted to millimetres. The "
            "volume is the signed volume of the polygon mesh, which is exact for the polygons "
            "given — but the polygons are themselves a tessellation of the original surfaces, "
            "so a curved part is very slightly under-measured."
        ),
        geometry_source="mesh",
        volume_method="mesh_signed_volume",
        volume_confidence="repaired" if topology.restored_facet_count else "high",
        facet_count=topology.triangle_count,
        closed_component_count=closed,
        open_component_count=opened,
        repair_applied=bool(topology.restored_facet_count),
        repair_method=REPAIR_METHOD if topology.restored_facet_count else None,
        repaired_facet_count=topology.restored_facet_count,
        repair_tolerance_mm=topology.weld_tolerance * scale,
    )


def complete_geometry(
    geometry: GeometryProperties, completion: GeometryCompletion
) -> GeometryProperties:
    """Fill in a volume the file does not contain, using the user's own figure.

    Nothing is inferred here. The user states either the whole part's volume or
    how much is missing from the watertight part that *was* measured, and that
    number is used verbatim. The result records which method was used and what
    was supplied, so an estimate made this way can never be mistaken later for
    one measured from the CAD.
    """
    completion = completion.validated()

    if geometry.geometry_status != INCOMPLETE or geometry.incomplete is None:
        raise ValueError(
            "This model's geometry is already complete, so there is nothing to supply. "
            "A geometry assumption is only accepted when the file itself cannot be measured."
        )

    known = geometry.incomplete.known_volume_mm3
    if completion.method == TOTAL_VOLUME:
        volume = completion.volume_mm3
        method = "user_supplied_total_volume"
        note = (
            f"The part's volume was not derivable from this file, so the user supplied a total "
            f"of {completion.volume_mm3:,.3f} mm³. This is a stated assumption, not a "
            "measurement of the CAD geometry."
        )
        supplied_total, supplied_missing = completion.volume_mm3, None
    else:
        volume = known + completion.volume_mm3
        method = "known_mesh_plus_user_supplied_missing_volume"
        note = (
            f"{known:,.3f} mm³ was measured from the watertight part of the mesh and the user "
            f"supplied a further {completion.volume_mm3:,.3f} mm³ for the "
            f"{geometry.incomplete.open_component_count} component(s) that could not be "
            "measured, giving a total of "
            f"{volume:,.3f} mm³. The added volume is a stated assumption, not a measurement."
        )
        supplied_total, supplied_missing = None, completion.volume_mm3

    return geometry.model_copy(
        update={
            "volume": volume,
            "geometry_source": "faceted_user_completed",
            "volume_method": method,
            "volume_confidence": "user_supplied",
            "geometry_status": COMPLETE,
            "user_supplied_total_volume_mm3": supplied_total,
            "user_supplied_missing_volume_mm3": supplied_missing,
            "units_note": f"{geometry.units_note} {note}",
        }
    )


def extract_geometry_from_file(path: str | Path) -> GeometryProperties:
    """Extract geometry properties from a CAD file on disk (STL or STEP).

    Raises FileNotFoundError if the path does not exist, and ValueError for
    the same parsing failures as `extract_geometry_from_bytes`.
    """
    resolved_path = Path(path)
    if not resolved_path.is_file():
        raise FileNotFoundError(f"CAD file not found: {resolved_path}")

    file_format = resolved_path.suffix.lstrip(".")
    return extract_geometry_from_bytes(resolved_path.read_bytes(), file_format)
