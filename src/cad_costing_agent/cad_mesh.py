"""CAD → browser-renderable triangle mesh. Visualization only.

This module exists so the browser never has to parse STEP/B-Rep. The server
already understands both supported formats, so it converts them here into one
common representation that a WebGL viewer can upload directly.

**It contains no costing logic and nothing here feeds costing.** The costing
path is unchanged and independent:

    CAD file → cad_geometry → cad_mass → deterministic costing
    CAD file → cad_geometry → cad_mesh  → browser viewer   (this module)

Both branches start from the same `extract_geometry_from_bytes`, so the numbers
shown beside the viewer are the same numbers costing uses. The mesh is only
ever used to draw pixels.

The common representation
-------------------------
One shape regardless of source format: an indexed triangle list of
`positions` (flat x, y, z triples) and `indices` (flat triples of vertex
numbers). No normals are transmitted — the viewer derives them, which is both
smaller on the wire and correct, *provided* vertices are not merged across
surface boundaries. So they are deliberately not merged:

- **STEP** vertices come from each B-Rep face's own triangulation, concatenated
  with an offset. Vertices are therefore shared *within* a face but never
  *between* faces, so derived normals come out smooth across a curved surface
  and hard at a real edge — which is how a machined part should look.
- **IGES** vertices come from the same per-face triangulation as STEP, through
  the same code, so the two formats cannot drift into looking different.
- **STL** is a triangle soup with no concept of a smooth surface, so its
  triangles are emitted unmerged and shade flat. Merging them would round off
  a cube's corners, which would be a lie about the file's contents.

Tessellation settings (STEP and IGES)
------------------------------------
OpenCASCADE is asked for a linear deflection of
`RELATIVE_LINEAR_DEFLECTION` (0.1%) of the model's bounding-box diagonal, so
fidelity scales with the part rather than being fixed in millimetres, plus an
angular deflection of `ANGULAR_DEFLECTION_RADIANS` (0.5 rad ≈ 28.6°) to keep
small holes and fillets round. If that overshoots `MAX_TRIANGLES`, the shape is
re-meshed once at `COARSE_RELATIVE_LINEAR_DEFLECTION` (0.5%) rather than
sending an enormous payload.

Budgets, and what happens at them
---------------------------------
`MAX_UPLOAD_BYTES` bounds the file, `MAX_INPUT_TRIANGLES` bounds what the viewer
will read, and `MAX_TRIANGLES` bounds what it will send.

Being over `MAX_TRIANGLES` is **not** a refusal. The full-resolution mesh is
built as always, every measurement is taken from it, and only then is a separate
*display* mesh decimated down to the budget by `mesh_simplify` — deterministic
vertex clustering, server-side, so the browser receives the finished small mesh
rather than hundreds of thousands of triangles to reduce itself. The response
says so, in `display_mesh_simplified`, `original_triangle_count`,
`display_triangle_count`, `simplification_method` and `simplification_ratio`.

    full mesh ──→ geometry / mass / costing        (never simplified)
              └──→ display mesh ──→ 3D viewer      (never measured)

Only when even that cannot produce a usable mesh — nothing survives the
decimation, or the model is past `MAX_INPUT_TRIANGLES` — is the preview reported
as unavailable via `mesh_error`, and never by raising to the caller: geometry
extraction and costing do not depend on the mesh, so a part that cannot be drawn
must still be costable.
"""

from __future__ import annotations

import io
import math
import os
import tempfile
from pathlib import Path

import numpy as np
import trimesh
from pydantic import BaseModel, Field

from cad_costing_agent import limits
from cad_costing_agent.cad_geometry import (
    IGES_FORMATS,
    STEP_FORMATS,
    GeometryProperties,
    extract_geometry_from_bytes,
    read_step_shape,
)
from cad_costing_agent.mesh_simplify import METHOD as SIMPLIFY_METHOD
from cad_costing_agent.mesh_simplify import simplify_for_display

# Both budgets come from `limits.py`, where they are configurable and, more
# importantly, kept separate: how much file the server accepts is a different
# question from how big a mesh a browser can draw. Re-exported under the names
# this module has always used.
MAX_UPLOAD_BYTES = limits.MAX_UPLOAD_BYTES

# Above this the JSON payload stops being something a browser should be asked
# to download and parse on a whim, so the display mesh is decimated down to it.
# A model over this limit is still costable and still viewable — what it loses
# is preview detail, nothing else.
MAX_TRIANGLES = limits.MAX_VIEWER_TRIANGLES

# The other end of the same question: how many triangles the viewer will read
# before concluding it cannot build a preview at all. This is what bounds the
# decimation's memory.
MAX_INPUT_TRIANGLES = limits.MAX_VIEWER_INPUT_TRIANGLES

PREVIEW_ONLY_NOTE = (
    "The geometry and costing above are unaffected — only the 3D preview is unavailable."
)

# Fractions of the bounding-box diagonal, so fidelity scales with the part.
RELATIVE_LINEAR_DEFLECTION = 0.001
COARSE_RELATIVE_LINEAR_DEFLECTION = 0.005
ANGULAR_DEFLECTION_RADIANS = 0.5

# Guards for degenerate models: a diagonal of zero would give a deflection of
# zero, which OCCT treats as "mesh infinitely finely".
MIN_LINEAR_DEFLECTION = 1e-6

# No definition is decimated below this, however small its share of the budget:
# a component reduced to nothing would vanish from the assembly rather than
# merely look coarse, and a missing component reads as a missing part.
MIN_DEFINITION_TRIANGLES = 12

# Positions are for drawing, not for measuring, so they are rounded to roughly
# this many significant digits relative to the model's own size. At six digits
# the error on a 100 mm part is 0.0001 mm — invisible — and the payload is
# meaningfully smaller.
POSITION_SIGNIFICANT_DIGITS = 6


class MeshBounds(BaseModel):
    """Axis-aligned bounds of the emitted mesh, in the mesh's own coordinates.

    Unlike `GeometryProperties.bounding_box`, which carries only extents, this
    keeps the actual corners. The viewer needs them to centre the model and to
    frame the camera.
    """

    min: list[float] = Field(min_length=3, max_length=3)
    max: list[float] = Field(min_length=3, max_length=3)


class TessellationSettings(BaseModel):
    """What was actually done to produce the mesh, for transparency."""

    method: str
    linear_deflection: float | None = None
    angular_deflection: float | None = None
    relative_linear_deflection: float | None = None
    coarsened: bool = False
    note: str


class RenderableMesh(BaseModel):
    """An indexed triangle list, ready to upload to a WebGL buffer.

    `triangle_count` and `vertex_count` describe the mesh actually carried here,
    which for a dense model is a decimated *display* mesh. The simplification
    fields say so explicitly rather than leaving a reader to infer that the
    numbers beside the viewer differ from the CAD file's own polygon count.
    None of these values is an engineering measurement, and none of them feeds
    one: volume, surface area and mass come from `geometry`, which is computed
    from the full-resolution data before any of this happens.
    """

    source_format: str
    positions: list[float]
    indices: list[int]
    vertex_count: int
    triangle_count: int
    bounds: MeshBounds
    tessellation: TessellationSettings

    display_mesh_simplified: bool = False
    original_triangle_count: int = 0
    display_triangle_count: int = 0
    simplification_method: str | None = None
    simplification_ratio: float | None = None


class ComponentMeshDefinition(BaseModel):
    """One component definition's geometry, in its own local space.

    Sent **once** however many times the assembly places it. Twelve identical
    bolts are one of these and twelve instances, which is both what the file
    says and the only thing that makes a large assembly's payload reasonable.
    """

    definition_id: str
    name: str
    positions: list[float] = Field(description="Flat x, y, z triples in the definition's own space.")
    indices: list[int] = Field(description="Flat triples of vertex numbers.")
    vertex_count: int = 0
    triangle_count: int = 0
    simplified: bool = Field(
        default=False, description="Whether this definition's preview was decimated."
    )


class ComponentInstance(BaseModel):
    """One occurrence: which definition to draw, and where the CAD puts it.

    `transform` is the **CAD placement**, unchanged. Any exploded arrangement is
    a display offset applied on top of it and is never written here, so what the
    file says and what the screen shows can always be told apart.
    """

    occurrence_id: str
    definition_id: str
    name: str
    depth: int = 0
    parent_occurrence_id: str | None = None
    transform: list[float] = Field(
        min_length=12,
        max_length=12,
        description="Row-major 3x4 world placement: nine rotation terms then three translation.",
    )


class AssemblyMesh(BaseModel):
    """An assembly drawn as definitions plus placements, not as one baked lump.

    Keeping the two apart is what makes the rest of this step possible. A baked
    buffer has every component's world position welded into its vertices, so
    moving one means rewriting geometry; here a component is a shared geometry
    and a transform, so an exploded arrangement is a change of transform and
    nothing else. It is also why a definition used twelve times is transmitted
    once rather than twelve times.
    """

    source_format: str = "step"
    definitions: list[ComponentMeshDefinition] = Field(default_factory=list)
    instances: list[ComponentInstance] = Field(default_factory=list)
    bounds: MeshBounds
    tessellation: TessellationSettings

    unique_triangle_count: int = Field(
        default=0, description="Triangles actually tessellated, counting each definition once."
    )
    drawn_triangle_count: int = Field(
        default=0, description="Triangles the viewer draws, counting every instance."
    )
    display_mesh_simplified: bool = False
    simplification_method: str | None = None

    @property
    def instance_count(self) -> int:
        return len(self.instances)


class CadViewerModel(BaseModel):
    """Everything the viewer panel needs from one upload.

    `geometry` is the *same* value `/cad/geometry` returns for these bytes —
    it comes from the same extractor — so the metadata shown beside the model
    is the metadata costing uses, not a second opinion computed from the mesh.

    `mesh` is `None` when the geometry parsed but no usable preview could be
    built; `mesh_error` then says why. Unparseable input raises instead.
    """

    filename: str
    geometry: GeometryProperties
    mesh: RenderableMesh | None = None
    mesh_error: str | None = None


def _rounding_decimals(largest_magnitude: float) -> int:
    """Decimal places giving ~`POSITION_SIGNIFICANT_DIGITS` at this scale.

    Derived from the model's overall size rather than per-coordinate, so it is
    one cheap vectorised rounding instead of per-value formatting.
    """
    if not math.isfinite(largest_magnitude) or largest_magnitude <= 0:
        return POSITION_SIGNIFICANT_DIGITS
    exponent = math.floor(math.log10(largest_magnitude))
    return max(0, min(12, POSITION_SIGNIFICANT_DIGITS - 1 - exponent))


def _finish(
    vertices: np.ndarray,
    faces: np.ndarray,
    *,
    source_format: str,
    tessellation: TessellationSettings,
) -> RenderableMesh:
    """Decimate if needed, then round, flatten and package for the wire.

    This is the one place every source format converges, and therefore the one
    place the display budget is applied. It runs *after* the caller has built
    the full-resolution mesh, and the geometry the response carries alongside
    was measured before this function was ever called — so decimating here
    cannot reach any engineering number.
    """
    vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)

    if faces.shape[0] == 0:
        raise ValueError("CAD file does not contain a valid mesh.")

    if faces.shape[0] > MAX_INPUT_TRIANGLES:
        raise MeshTooLargeError(
            f"This model has {faces.shape[0]:,} triangles, above the "
            f"{MAX_INPUT_TRIANGLES:,} the preview will read. {PREVIEW_ONLY_NOTE}"
        )

    simplified = simplify_for_display(vertices, faces, MAX_TRIANGLES)
    if simplified.triangle_count == 0:
        raise MeshTooLargeError(
            f"This model has {simplified.original_triangle_count:,} triangles and nothing "
            f"recognisable survived reducing it to the {MAX_TRIANGLES:,} a browser is sent, "
            f"so it cannot be previewed. {PREVIEW_ONLY_NOTE}"
        )
    vertices, faces = simplified.vertices, simplified.faces

    if simplified.simplified:
        tessellation = tessellation.model_copy(
            update={
                "note": (
                    f"{tessellation.note} The preview was then reduced from "
                    f"{simplified.original_triangle_count:,} to {simplified.triangle_count:,} "
                    "triangles for browser performance; engineering geometry and costing use "
                    "the full-resolution CAD data."
                )
            }
        )

    minimum = vertices.min(axis=0)
    maximum = vertices.max(axis=0)
    decimals = _rounding_decimals(float(np.abs(vertices).max()))
    rounded = np.round(vertices, decimals)

    return RenderableMesh(
        source_format=source_format,
        positions=rounded.reshape(-1).tolist(),
        indices=faces.reshape(-1).tolist(),
        vertex_count=int(vertices.shape[0]),
        triangle_count=int(faces.shape[0]),
        bounds=MeshBounds(
            min=np.round(minimum, decimals).tolist(),
            max=np.round(maximum, decimals).tolist(),
        ),
        tessellation=tessellation,
        display_mesh_simplified=simplified.simplified,
        original_triangle_count=simplified.original_triangle_count,
        display_triangle_count=simplified.triangle_count,
        simplification_method=simplified.method,
        simplification_ratio=(
            None if simplified.ratio is None else round(simplified.ratio, 6)
        ),
    )


class MeshTooLargeError(Exception):
    """The model parsed, but is too dense to preview within the budget."""


def _stl_mesh(data: bytes) -> RenderableMesh:
    """Build the renderable mesh for an STL.

    Triangles are emitted unmerged so the viewer's derived normals are
    per-triangle. STL describes a faceted surface and nothing more, so flat
    shading is the truthful rendering of it.
    """
    try:
        mesh = trimesh.load(io.BytesIO(data), file_type="stl")
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as ValueError
        raise ValueError(f"Could not parse CAD file as 'stl': {exc}") from exc

    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise ValueError("CAD file does not contain a valid mesh.")

    # A dense STL is not refused here: the full triangle list is handed on and
    # `_finish` decimates a display copy of it if it is over the wire budget.
    # (T, 3, 3) corner coordinates -> (3T, 3) unmerged vertices.
    vertices = np.asarray(mesh.triangles, dtype=np.float64).reshape(-1, 3)
    faces = np.arange(vertices.shape[0], dtype=np.int64).reshape(-1, 3)

    return _finish(
        vertices,
        faces,
        source_format="stl",
        tessellation=TessellationSettings(
            method="none",
            note=(
                "STL is already triangulated, so it is passed through as-is with no "
                "re-tessellation. Triangles are kept unmerged so the surface shades "
                "flat, matching what the file actually describes."
            ),
        ),
    )


def _step_mesh(data: bytes) -> RenderableMesh:
    """Tessellate a STEP file into triangles.

    A faceted export is drawn from its own polygons; anything else is read and
    tessellated by the shared B-Rep path.
    """
    # A faceted export cannot be read by OpenCASCADE at all, but it needs no
    # tessellation either — it is already polygons. Without this branch the
    # viewer would report "no transferable geometry" for a file whose geometry,
    # mass and cost all work perfectly.
    from cad_costing_agent.features.step_text import classify_step_bytes

    if classify_step_bytes(data).is_faceted:
        return _faceted_step_mesh(data)

    # Parsing lives in cad_geometry, so there is one reader and one set of
    # failure messages for every consumer of STEP.
    return _brep_shape_mesh(read_step_shape(data), source_format="step", label="STEP")


def _iges_mesh(data: bytes) -> RenderableMesh:
    """Tessellate an IGES file into triangles.

    IGES needs no separate tessellation path: once its surfaces are read they
    are exact surfaces like any other, and the same deflection settings apply.
    The faces are tessellated as they come, whether or not they enclose a
    solid — a surface model that cannot be measured can still be looked at,
    which is the same separation the viewer already keeps for a model too
    dense to draw but perfectly costable.
    """
    from cad_costing_agent.cad_iges import read_iges_shape

    return _brep_shape_mesh(read_iges_shape(data), source_format="iges", label="IGES")


def _brep_shape_mesh(shape, *, source_format: str, label: str) -> RenderableMesh:
    """Tessellate an already-parsed B-Rep shape, whatever format it came from.

    Shared by STEP and IGES deliberately: the tessellation settings, the
    coarsening fallback, the per-face vertex handling and the reversed-face
    winding fix are properties of OpenCASCADE B-Rep geometry, not of a file
    format, and having two copies of them would let the two formats drift into
    looking different for no reason.
    """
    try:
        from OCP.Bnd import Bnd_Box
        from OCP.BRep import BRep_Tool
        from OCP.BRepBndLib import BRepBndLib
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.BRepTools import BRepTools
        from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopoDS import TopoDS
    except ImportError as exc:
        raise ValueError(
            f"{label} support requires the 'cadquery-ocp' package, which is not installed."
        ) from exc

    # Deflection is a fraction of the diagonal, so a 10 mm bracket and a 2 m
    # frame get comparable visual fidelity rather than wildly different
    # triangle counts.
    bounding_box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, bounding_box)
    x_min, y_min, z_min, x_max, y_max, z_max = bounding_box.Get()
    diagonal = math.dist((x_min, y_min, z_min), (x_max, y_max, z_max))

    def deflection_for(fraction: float) -> float:
        return max(diagonal * fraction, MIN_LINEAR_DEFLECTION)

    def tessellate(fraction: float) -> tuple[np.ndarray, np.ndarray]:
        BRepTools.Clean_s(shape)
        BRepMesh_IncrementalMesh(
            shape,
            deflection_for(fraction),
            False,  # deflection is absolute; the scaling is done above
            ANGULAR_DEFLECTION_RADIANS,
            True,  # tessellate faces in parallel
        ).Perform()

        vertex_blocks: list[np.ndarray] = []
        face_blocks: list[np.ndarray] = []
        offset = 0

        explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            location = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation_s(face, location)
            explorer.Next()
            if triangulation is None:
                continue

            node_count = triangulation.NbNodes()
            triangle_count = triangulation.NbTriangles()
            if node_count == 0 or triangle_count == 0:
                continue

            transformation = None if location.IsIdentity() else location.Transformation()
            nodes = np.empty((node_count, 3), dtype=np.float64)
            for index in range(1, node_count + 1):
                point = triangulation.Node(index)
                if transformation is not None:
                    point = point.Transformed(transformation)
                nodes[index - 1] = (point.X(), point.Y(), point.Z())

            # A REVERSED face's triangles wind the wrong way round, which would
            # leave it facing inwards and lit from behind.
            reversed_face = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
            triangles = np.empty((triangle_count, 3), dtype=np.int64)
            for index in range(1, triangle_count + 1):
                first, second, third = triangulation.Triangle(index).Get()
                # OCCT node numbering is 1-based.
                if reversed_face:
                    triangles[index - 1] = (first - 1, third - 1, second - 1)
                else:
                    triangles[index - 1] = (first - 1, second - 1, third - 1)

            vertex_blocks.append(nodes)
            face_blocks.append(triangles + offset)
            offset += node_count

        if not vertex_blocks:
            raise MeshTooLargeError(
                f"This {label} model produced no triangulated faces, so it cannot be "
                "previewed. The geometry and costing above are unaffected."
            )
        return np.vstack(vertex_blocks), np.vstack(face_blocks)

    vertices, faces = tessellate(RELATIVE_LINEAR_DEFLECTION)
    fraction = RELATIVE_LINEAR_DEFLECTION
    coarsened = False

    # One coarsening pass, rather than sending a huge payload or giving up.
    if len(faces) > MAX_TRIANGLES:
        vertices, faces = tessellate(COARSE_RELATIVE_LINEAR_DEFLECTION)
        fraction = COARSE_RELATIVE_LINEAR_DEFLECTION
        coarsened = True

    # Anything still over budget after coarsening is decimated in `_finish`.
    # Re-tessellating is tried first because asking OpenCASCADE for fewer
    # triangles gives a better-looking result than reducing them afterwards.
    note = (
        f"Tessellated with OpenCASCADE at a linear deflection of {fraction:.1%} of the "
        f"bounding-box diagonal ({deflection_for(fraction):.4g} mm) and an angular "
        f"deflection of {ANGULAR_DEFLECTION_RADIANS} rad."
    )
    if coarsened:
        note += (
            " The finer setting exceeded the triangle budget, so this model was "
            "re-tessellated more coarsely; the preview is less detailed than usual."
        )

    return _finish(
        vertices,
        faces,
        source_format=source_format,
        tessellation=TessellationSettings(
            method="opencascade-brepmesh",
            linear_deflection=deflection_for(fraction),
            angular_deflection=ANGULAR_DEFLECTION_RADIANS,
            relative_linear_deflection=fraction,
            coarsened=coarsened,
            note=note,
        ),
    )


def _faceted_step_mesh(data: bytes) -> RenderableMesh:
    """Draw a faceted STEP straight from its polygons.

    Nothing is tessellated: the file already is a mesh. Bodies are concatenated
    with an index offset and left unmerged between them, so the viewer's derived
    normals stay per-face exactly as they do for an STL.
    """
    from cad_costing_agent.cad_faceted import parse_faceted_step

    # Real Creo faceted exports run to hundreds of thousands of polygons. They
    # are read in full and drawn — decimated for display by `_finish` — rather
    # than refused. `MAX_INPUT_TRIANGLES` is what stops an unbounded read.
    try:
        model = parse_faceted_step(data, max_facets=MAX_INPUT_TRIANGLES)
    except ValueError as exc:
        raise MeshTooLargeError(f"{exc} {PREVIEW_ONLY_NOTE}") from exc

    if not model.shells:
        raise ValueError(
            "This faceted STEP file contains no polygons that could be read, so it cannot be "
            "previewed."
        )

    vertex_blocks = []
    face_blocks = []
    offset = 0
    for shell in model.shells:
        vertex_blocks.append(shell.vertices)
        face_blocks.append(shell.triangles + offset)
        offset += shell.vertex_count

    return _finish(
        np.vstack(vertex_blocks),
        np.vstack(face_blocks),
        source_format="step",
        tessellation=TessellationSettings(
            method="none",
            note=(
                "This is a faceted STEP export, so its polygons are drawn as written with no "
                "re-tessellation. The mesh is for display only — volume and mass come from the "
                "same polygons but are measured, not drawn."
            ),
        ),
    )


def _tessellate_definition(shape, deflection: float):
    """Triangulate one component's shape into local-space vertices and faces."""
    import numpy as np
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    BRepTools.Clean_s(shape)
    BRepMesh_IncrementalMesh(shape, deflection, False, ANGULAR_DEFLECTION_RADIANS, True).Perform()

    vertex_blocks: list = []
    face_blocks: list = []
    offset = 0

    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        explorer.Next()
        if triangulation is None:
            continue
        node_count = triangulation.NbNodes()
        triangle_count = triangulation.NbTriangles()
        if node_count == 0 or triangle_count == 0:
            continue

        transformation = None if location.IsIdentity() else location.Transformation()
        nodes = np.empty((node_count, 3), dtype=np.float64)
        for index in range(1, node_count + 1):
            point = triangulation.Node(index)
            if transformation is not None:
                point = point.Transformed(transformation)
            nodes[index - 1] = (point.X(), point.Y(), point.Z())

        reversed_face = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
        triangles = np.empty((triangle_count, 3), dtype=np.int64)
        for index in range(1, triangle_count + 1):
            first, second, third = triangulation.Triangle(index).Get()
            if reversed_face:
                triangles[index - 1] = (first - 1, third - 1, second - 1)
            else:
                triangles[index - 1] = (first - 1, second - 1, third - 1)

        vertex_blocks.append(nodes)
        face_blocks.append(triangles + offset)
        offset += node_count

    if not vertex_blocks:
        return None, None
    return np.vstack(vertex_blocks), np.vstack(face_blocks)


def _apply(transform, points):
    """Place a component's local vertices into assembly space.

    The transform arrives as twelve plain floats — nine rotation terms then
    three translation terms — because nothing outside the assembly reader holds
    an OpenCASCADE handle. Losing it here would draw every component at the
    origin, on top of each other.
    """
    import numpy as np

    rotation = np.array(transform[:9], dtype=np.float64).reshape(3, 3)
    translation = np.array(transform[9:], dtype=np.float64)
    return points @ rotation.T + translation


def build_assembly_mesh(structure) -> AssemblyMesh:
    """Mesh each definition once, and place it at each of its occurrences.

    Only leaf parts are drawn. A sub-assembly's own shape is a compound of its
    children's, so drawing both would send every nested component's triangles
    twice and stack a duplicate on top of each one.

    The display budget applies to the triangles actually *drawn* — a definition
    placed twelve times costs twelve times its triangles on screen — so when it
    is exceeded each definition is decimated in proportion to its share. Because
    each definition keeps its own geometry, decimation cannot invalidate
    component identity the way it did when everything shared one index buffer.

    Raises `MeshTooLargeError` when nothing could be drawn, which the caller
    must not treat as fatal: an assembly whose components carry no geometry
    still has a structure worth showing.
    """
    import numpy as np

    drawable = [
        occurrence
        for occurrence in structure.occurrences
        if not structure.definitions[occurrence.definition_id].is_assembly
        and structure.definitions[occurrence.definition_id].has_geometry
    ]
    if not drawable:
        raise MeshTooLargeError(
            "None of this assembly's components carries geometry that could be drawn, so "
            "there is no preview. The component structure, names, quantities and placements "
            "are unaffected and are still reported."
        )

    # One deflection for the whole assembly, taken from how far apart its
    # components sit, so a small component is not tessellated a hundred times
    # more finely than a large one.
    positions = np.array(
        [occurrence.placement.world[9:] for occurrence in drawable], dtype=np.float64
    )
    spread = float(np.linalg.norm(positions.max(axis=0) - positions.min(axis=0)))
    deflection = max(max(spread, 1.0) * RELATIVE_LINEAR_DEFLECTION, MIN_LINEAR_DEFLECTION)

    used = sorted({occurrence.definition_id for occurrence in drawable})
    placements = {definition_id: 0 for definition_id in used}
    for occurrence in drawable:
        placements[occurrence.definition_id] += 1

    meshed: dict[str, tuple] = {}
    for definition_id in used:
        vertices, faces = _tessellate_definition(
            structure.definitions[definition_id].shape, deflection
        )
        if vertices is not None:
            meshed[definition_id] = (vertices, faces)

    if not meshed:
        raise MeshTooLargeError(
            "This assembly produced no triangulated faces, so it cannot be previewed. The "
            "component structure and costing are unaffected."
        )

    drawn = sum(len(meshed[key][1]) * placements[key] for key in meshed)
    if drawn > MAX_INPUT_TRIANGLES:
        raise MeshTooLargeError(
            f"This assembly would draw {drawn:,} triangles, above the "
            f"{MAX_INPUT_TRIANGLES:,} the preview will read. {PREVIEW_ONLY_NOTE}"
        )

    ratio = min(1.0, MAX_TRIANGLES / drawn) if drawn else 1.0
    any_simplified = False

    definitions: list[ComponentMeshDefinition] = []
    for definition_id in sorted(meshed):
        vertices, faces = meshed[definition_id]
        simplified_flag = False
        if ratio < 1.0:
            budget = max(MIN_DEFINITION_TRIANGLES, int(len(faces) * ratio))
            reduced = simplify_for_display(vertices, faces, budget)
            if reduced.triangle_count:
                simplified_flag = reduced.simplified
                vertices, faces = reduced.vertices, reduced.faces
                any_simplified = any_simplified or reduced.simplified

        decimals = _rounding_decimals(float(np.abs(vertices).max()) if len(vertices) else 1.0)
        definitions.append(
            ComponentMeshDefinition(
                definition_id=definition_id,
                name=structure.definitions[definition_id].name,
                positions=np.round(vertices, decimals).ravel().tolist(),
                indices=faces.ravel().tolist(),
                vertex_count=int(len(vertices)),
                triangle_count=int(len(faces)),
                simplified=simplified_flag,
            )
        )

    by_id = {definition.definition_id: definition for definition in definitions}
    instances = [
        ComponentInstance(
            occurrence_id=occurrence.occurrence_id,
            definition_id=occurrence.definition_id,
            name=occurrence.name or structure.definitions[occurrence.definition_id].name,
            depth=occurrence.depth,
            parent_occurrence_id=occurrence.parent_occurrence_id,
            transform=list(occurrence.placement.world),
        )
        for occurrence in drawable
        if occurrence.definition_id in by_id
    ]

    # Bounds are computed over the placed components, because that is what the
    # camera has to frame. Nothing here is a measurement: volume, area and mass
    # were taken from the CAD long before any of this ran.
    corners = []
    for instance in instances:
        definition = by_id[instance.definition_id]
        if not definition.positions:
            continue
        local = np.asarray(definition.positions, dtype=np.float64).reshape(-1, 3)
        placed = _apply(instance.transform, local)
        corners.append(placed.min(axis=0))
        corners.append(placed.max(axis=0))

    stacked = np.vstack(corners) if corners else np.zeros((1, 3))
    bounds = MeshBounds(
        min=stacked.min(axis=0).tolist(), max=stacked.max(axis=0).tolist()
    )

    unique = sum(definition.triangle_count for definition in definitions)
    drawn_now = sum(by_id[instance.definition_id].triangle_count for instance in instances)

    note = (
        f"{len(definitions)} component definition(s) tessellated once each and placed at "
        f"{len(instances)} occurrence(s), at a linear deflection of {deflection:.4g} mm."
    )
    if any_simplified:
        note += (
            f" The definitions were then decimated to fit the {MAX_TRIANGLES:,}-triangle "
            "preview budget; engineering geometry and costing use the full-resolution CAD "
            "data. Each definition keeps its own geometry, so components remain individually "
            "selectable."
        )

    return AssemblyMesh(
        source_format="step",
        definitions=definitions,
        instances=instances,
        bounds=bounds,
        tessellation=TessellationSettings(
            method="opencascade-brepmesh",
            linear_deflection=deflection,
            angular_deflection=ANGULAR_DEFLECTION_RADIANS,
            relative_linear_deflection=RELATIVE_LINEAR_DEFLECTION,
            note=note,
        ),
        unique_triangle_count=unique,
        drawn_triangle_count=drawn_now,
        display_mesh_simplified=any_simplified,
        simplification_method=SIMPLIFY_METHOD if any_simplified else None,
    )


def build_renderable_mesh(data: bytes, file_format: str) -> RenderableMesh:
    """Convert CAD bytes into the common renderable mesh.

    Raises `ValueError` for unsupported or unparseable input and
    `MeshTooLargeError` when the model parses but exceeds the preview budget.
    """
    normalized_format = file_format.strip().lower().lstrip(".")
    if normalized_format in STEP_FORMATS:
        return _step_mesh(data)
    if normalized_format in IGES_FORMATS:
        return _iges_mesh(data)
    return _stl_mesh(data)


def build_viewer_model(
    data: bytes, filename: str, geometry: GeometryProperties | None = None
) -> CadViewerModel:
    """Geometry metadata plus a renderable mesh for one uploaded CAD file.

    Geometry comes from the existing extractor, so it matches `/cad/geometry`
    exactly. A model that parses but cannot be previewed still returns its
    geometry, with `mesh_error` explaining the missing preview — costing does
    not need the mesh and must not be blocked by it.

    `geometry` may be passed in by a caller that has already measured these
    bytes — the import layer does — so a request does not parse the file for
    measurement twice. Passing it changes no output: it is the same value this
    function would have computed from the same bytes.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"CAD file is {len(data) / 1024 / 1024:.1f} MB, above the "
            f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB limit for this POC."
        )

    file_format = Path(filename or "").suffix
    if geometry is None:
        # Raises ValueError for an unsupported format, empty input, or bad content.
        geometry = extract_geometry_from_bytes(data, file_format)

    try:
        mesh = build_renderable_mesh(data, file_format)
    except MeshTooLargeError as exc:
        return CadViewerModel(filename=filename, geometry=geometry, mesh=None, mesh_error=str(exc))

    return CadViewerModel(filename=filename, geometry=geometry, mesh=mesh)
