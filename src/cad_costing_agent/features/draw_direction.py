"""Pull direction, draft and undercuts of an exact B-Rep, for a two-half mould or die.

    CAD file -> B-Rep (the shape recognition already read) -> **pull axis, draft, undercuts**

Analysis only. This module measures; it decides nothing. It is read by the
`/cad/draw-direction` endpoint and, as a plain carrier, by the draft and
undercut DFM rules, and by nothing that reaches a route, a time or a cost. A
drafted wall is not evidence that a part is cast, moulded or forged — a
machined part can carry a taper — so nothing here proposes a process. It
imports `limits`, `features.model` and `features.rays` from this package.

`propose_cores` groups the undercut faces into regions that share edges, each
a proposed core or side action. The count is a proposal: it is shown to the
engineer and reaches a time only as a count the engineer enters on the forming
stage, never from here.

A pull axis and its two halves
------------------------------
A candidate axis `d` stands for a mould or die that opens along it: one half
withdraws along `+d`, the other along `-d`. For every face, points are sampled
on a fixed UV grid inside the face's own boundary (`features.rays`, the same
sampling and ray caster the tool-approach check uses), each with its outward
normal `n`.

- **Draft** at a point is the angle between the face and the pull axis,
  `asin(|n . d|)`: 90 deg for a face square to the pull, 0 deg for a wall
  parallel to it. A face's draft is exact where the geometry gives it — a plane
  (one normal), a cylinder whose axis is the pull (0 deg), a cone whose axis is
  the pull (its half-angle) — and otherwise the **minimum over the sampled
  points**, which can only miss a smaller value between samples, never report
  a larger one. A face at 0 deg (within `ZERO_DRAFT_DEG`) is a zero-draft wall.
- **Release**: a point releases into the `+d` half when it does not face away
  from `+d` and a straight ray from it (lifted `RAY_LIFT_MM` off the surface)
  along `+d` leaves the part without meeting material; likewise for `-d`. A
  face with a point that releases into neither half is an **undercut**: a
  straight two-half pull cannot free it, so it needs a side action, a core, a
  loose piece or later machining.

Candidate axes and the recommendation
-------------------------------------
Candidates are deterministic: the file's Z, X and Y axes; the principal axes
of inertia whose moment is distinct (a repeated moment leaves its axes
undefined within a plane, so those are not offered); and the normals of the
`LARGEST_PLANAR_FACES` largest planar faces. Axes that coincide (either sense)
are one candidate, carrying every reason it was offered. All are evaluated, in
that order, and all are reported. The **recommended** axis has the fewest
undercut faces, then the least zero-draft area, then the earliest place in that
order. That is a ranking by two counts, not a design decision: the axis a
toolmaker chooses also weighs parting, gating, appearance and tool cost, none of
which is seen here. An axis the engineer enters is evaluated the same way and
is the one reported, beside the recommendation.

No parting line is estimated. Where a wall releases into either half (a
zero-draft wall) the parting can lie anywhere along it, and a stepped parting is
a tooling decision, so a line drawn here would be a guess. `PARTING_NOTE` says so.

Deterministic: fixed grids, fixed candidate order, rounded outputs. Budget:
`limits.MAX_DRAW_RAYS` bounds the rays one measurement may cast; over it the
grid is coarsened for every face alike and the result says so, and if even the
coarsest grid is over it nothing is measured. Only this analysis degrades.
"""

from __future__ import annotations

import math
import time

from pydantic import BaseModel, Field

from cad_costing_agent import limits
from cad_costing_agent.features.model import OUTPUT_DECIMALS, Point3D
from cad_costing_agent.features.rays import ShapeRays

METHOD = "two_half_straight_pull_ray_grid"

GRIDS: tuple[tuple[int, ...], ...] = ((5, 9), (3, 7))
"""Grid sides tried per face, finest first: the first set is used unless the ray
budget forces the second. Within a set, a face too thin for the first side is
tried on the second (as the approach check does)."""

RAY_LIFT_MM = 0.01
"""How far a ray's origin is lifted off the face along its outward normal, so
the face itself is not the first material the ray meets."""

ZERO_DRAFT_DEG = 1e-6
"""A face whose draft is at or below this is a zero-draft wall."""

FACING_TOLERANCE = 1e-9
"""|n . d| at or below this is a point square to the pull: it faces neither half
and is tested against both."""

SAME_AXIS = 1e-9
"""Two candidate axes with |cos| within this of 1 are the same axis."""

DEGENERATE_MOMENT = 1e-6
"""Two principal moments within this fraction of the larger are equal, and the
axes in their plane are not defined by the part."""

LARGEST_PLANAR_FACES = 3
"""How many of the largest planar faces offer their normal as a candidate."""

RECOMMENDATION_BASIS = (
    "Recommended by two counts only: the fewest undercut faces, then the least zero-draft area, "
    "with ties going to the earliest candidate in the order evaluated. It is not a design "
    "decision: parting, gating, appearance and tool cost decide a real pull and are not seen here."
)

PARTING_NOTE = (
    "No parting line is estimated. Where a wall can release into either half (a zero-draft wall) "
    "the parting can lie anywhere along it, and a stepped parting is a tooling decision, so a "
    "line drawn here would be a guess."
)

METHOD_DESCRIPTION = (
    "Each candidate axis stands for a two-half mould or die opening along it. Every face is "
    "sampled on a fixed grid inside its own boundary. Draft at a point is the angle between the "
    "face and the axis; it is exact for planes and for cylinders and cones whose axis is the "
    "pull, and the minimum over the samples otherwise. A point releases into a half when it does "
    "not face away from that half and a straight ray from it along the pull leaves the part "
    "without meeting material; a face with a point that releases into neither half is an undercut."
)

LIMITATIONS: tuple[str, ...] = (
    "A ray is not mould steel: a straight line is tested, not a slide, a core or the tool's own "
    "shape. A face reported as releasing may still need a feature in the tool that this does not "
    "model.",
    "Only sampled points are tested. A small undercut, or a smaller draft, between samples is "
    "not seen: the minimum draft reported on a sampled face can be smaller on the part, never "
    "larger, and an undercut can be missed, never invented.",
    "Draft is the angle between a face and the pull axis. It says nothing about whether that "
    "draft is enough for a given material, texture or depth.",
    "No parting line is estimated, and the recommended axis is a ranking by undercut count and "
    "zero-draft area, not a toolmaker's choice.",
    "Only exact B-Rep geometry bounding a closed solid is measured. Faceted exports, STL meshes "
    "and IGES surface models are refused with the reason.",
)

ENGINEER_SOURCE = "entered by the engineer"

RELEASE_PLUS = "plus"
RELEASE_MINUS = "minus"
RELEASE_EITHER = "either"
RELEASE_SPLIT = "split"
RELEASE_UNDERCUT = "undercut"
RELEASE_NOT_SAMPLED = "not_sampled"


class FaceDraft(BaseModel):
    """One face against the pull axis that was used."""

    face_id: int = Field(description="1-based, as a feature's `source_faces` numbers faces.")
    surface_type: str
    area_mm2: float
    draft_deg: float | None = Field(description="Null when no point on the face could be sampled.")
    draft_method: str = Field(
        description="'exact_plane', 'exact_axial_cylinder', 'exact_axial_cone' or 'sampled_minimum'."
    )
    zero_draft: bool
    release: str = Field(
        description=(
            "'plus' / 'minus' (every sampled point releases into that half), 'either' (every point "
            "releases into both: a zero-draft wall), 'split' (points release into different "
            "halves), 'undercut' (a point releases into neither), or 'not_sampled'."
        )
    )
    samples: int
    samples_undercut: int
    marker: Point3D | None = Field(
        default=None,
        description=(
            "A sampled point on the face: the first undercut point for an undercut, else the "
            "point of least draft. A pointer for the viewer, not the face."
        ),
    )


class CandidateAxis(BaseModel):
    axis: Point3D = Field(description="Unit vector; the mould opens along it in both senses.")
    label: str
    sources: list[str] = Field(description="Every reason this axis was offered, in order.")
    order: int = Field(description="Position in the order evaluated, from 0.")
    engineer_entered: bool = False
    undercut_faces: int
    undercut_area_mm2: float
    zero_draft_faces: int
    zero_draft_area_mm2: float
    recommended: bool = False


class DrawDirectionResult(BaseModel):
    filename: str = ""
    measured: bool
    method: str = METHOD
    method_description: str = METHOD_DESCRIPTION
    limitations: list[str] = Field(default_factory=lambda: list(LIMITATIONS))
    not_measured_reason: str | None = None

    pull_axis: Point3D | None = Field(default=None, description="The axis the faces are reported against.")
    pull_axis_label: str | None = None
    axis_source: str | None = Field(
        default=None, description="'engineer_entered' or 'recommended'."
    )
    recommended_axis: Point3D | None = None
    recommended_axis_label: str | None = None
    recommendation_basis: str = RECOMMENDATION_BASIS
    candidates: list[CandidateAxis] = Field(default_factory=list)

    faces: list[FaceDraft] = Field(default_factory=list, description="In face-id order.")
    undercut_face_count: int = 0
    undercut_area_mm2: float = 0.0
    undercut_face_ids: list[int] = Field(default_factory=list)
    zero_draft_face_count: int = 0
    zero_draft_area_mm2: float = 0.0
    zero_draft_face_ids: list[int] = Field(default_factory=list)
    minimum_draft_deg: float | None = Field(
        default=None, description="Least draft over the faces that release (undercuts excluded)."
    )
    minimum_draft_face: int | None = None

    parting: None = None
    parting_note: str = PARTING_NOTE

    solid_count: int = 0
    face_count: int = 0
    faces_outside_a_solid: int = 0
    grid: list[int] = Field(default_factory=list)
    rays_cast: int = 0
    max_rays: int = 0
    budget_limited: bool = False
    budget_note: str | None = None
    warnings: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0.0


def refusal_reason(representation: str, has_shape: bool) -> str | None:
    """Why a recognized upload cannot be checked for draft, or None if it can.

    `representation` is the recognition result's own; nothing is re-read here.
    """
    if representation == "faceted" or not has_shape:
        return (
            "Pull direction, draft and undercuts are measured on exact B-Rep geometry only. This "
            "file is a faceted export: flat polygons, whose normals are the tessellation's and not "
            "the part's, so a draft read from them would report the facets. Re-export the part as "
            "solid/B-Rep STEP."
        )
    if representation == "surface_model":
        return (
            "Pull direction needs a closed solid as the file itself states it. This IGES file "
            "stores loose trimmed surfaces, so which side of a surface is material is not "
            "recorded. Re-export the IGES with solid/B-Rep geometry, or export STEP instead."
        )
    if representation != "brep":
        return f"Pull direction is not measured for '{representation}' geometry."
    return None


def not_measured(reason: str, *, filename: str = "", budget_limited: bool = False) -> DrawDirectionResult:
    return DrawDirectionResult(
        filename=filename,
        measured=False,
        not_measured_reason=reason,
        budget_limited=budget_limited,
        max_rays=limits.MAX_DRAW_RAYS,
    )


def normalise_axis(vector) -> tuple[float, float, float]:
    """A unit vector from three finite numbers, or ValueError saying why not."""
    try:
        x, y, z = (float(c) for c in vector)
    except (TypeError, ValueError) as exc:
        raise ValueError("A pull axis is three numbers: [x, y, z].") from exc
    if not all(math.isfinite(c) for c in (x, y, z)):
        raise ValueError("A pull axis must be three finite numbers.")
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-12:
        raise ValueError("A pull axis cannot be the zero vector.")
    return (x / length, y / length, z / length)


def _canonical(axis: tuple[float, float, float]) -> tuple[float, float, float]:
    """One sense per axis: the first of z, x, y that is not zero is made positive."""
    x, y, z = axis
    for component in (z, x, y):
        if abs(component) > 1e-12:
            if component < 0:
                return (-x, -y, -z)
            return axis
    return axis


def _clean(value: float) -> float:
    rounded = round(value, OUTPUT_DECIMALS)
    return 0.0 if rounded == 0 else rounded


def axis_label(axis: tuple[float, float, float]) -> str:
    """'±Z' for a file axis, otherwise '±(x, y, z)' rounded."""
    x, y, z = (_clean(c) for c in _canonical(axis))
    for name, unit in (("Z", (0.0, 0.0, 1.0)), ("X", (1.0, 0.0, 0.0)), ("Y", (0.0, 1.0, 0.0))):
        if abs(1.0 - abs(x * unit[0] + y * unit[1] + z * unit[2])) < 1e-9:
            return "±" + name
    return f"±({x:g}, {y:g}, {z:g})"


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


# ---------------------------------------------------------------------------
# The measurement. Everything below this line touches OpenCASCADE.
# ---------------------------------------------------------------------------

_SURFACE_NAMES = {
    "GeomAbs_Plane": "plane",
    "GeomAbs_Cylinder": "cylinder",
    "GeomAbs_Cone": "cone",
    "GeomAbs_Sphere": "sphere",
    "GeomAbs_Torus": "torus",
    "GeomAbs_BezierSurface": "bezier",
    "GeomAbs_BSplineSurface": "bspline",
    "GeomAbs_SurfaceOfRevolution": "revolution",
    "GeomAbs_SurfaceOfExtrusion": "extrusion",
    "GeomAbs_OffsetSurface": "offset",
}


class _Face:
    __slots__ = ("face_id", "surface_type", "area", "plane_normal", "axis", "half_angle", "samples")

    def __init__(self, face_id, surface_type, area, plane_normal, axis, half_angle, samples):
        self.face_id = face_id
        self.surface_type = surface_type
        self.area = area
        self.plane_normal = plane_normal
        self.axis = axis
        self.half_angle = half_angle
        self.samples = samples


def _faces(model: ShapeRays, solid_face_ids: list[int], grids: tuple[int, ...], sign: float) -> list[_Face]:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_Orientation

    faces: list[_Face] = []
    for face_id in solid_face_ids:
        face = model.face(face_id)
        surface = BRepAdaptor_Surface(face)
        surface_type = _SURFACE_NAMES.get(surface.GetType().name, "other")
        properties = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, properties)
        plane_normal = axis = half_angle = None
        if surface_type == "plane":
            d = surface.Plane().Axis().Direction()
            plane_normal = (d.X(), d.Y(), d.Z())
            if face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED:
                plane_normal = (-plane_normal[0], -plane_normal[1], -plane_normal[2])
            plane_normal = tuple(sign * c for c in plane_normal)
        elif surface_type == "cylinder":
            d = surface.Cylinder().Axis().Direction()
            axis = (d.X(), d.Y(), d.Z())
        elif surface_type == "cone":
            cone = surface.Cone()
            d = cone.Axis().Direction()
            axis = (d.X(), d.Y(), d.Z())
            half_angle = abs(float(cone.SemiAngle()))
        samples = [
            (point, tuple(sign * c for c in normal)) for point, normal in model.samples(face_id, grids)
        ]
        faces.append(
            _Face(face_id, surface_type, float(properties.Mass()), plane_normal, axis, half_angle, samples)
        )
    return faces


def _exact_draft(face: _Face, d) -> tuple[float, str] | None:
    if face.plane_normal is not None:
        return math.degrees(math.asin(min(1.0, abs(_dot(face.plane_normal, d))))), "exact_plane"
    if face.axis is not None and abs(1.0 - abs(_dot(face.axis, d))) < SAME_AXIS:
        if face.surface_type == "cylinder":
            return 0.0, "exact_axial_cylinder"
        if face.surface_type == "cone" and face.half_angle is not None:
            return math.degrees(face.half_angle), "exact_axial_cone"
    return None


def _origin(point, normal) -> tuple[float, float, float]:
    return (
        point[0] + normal[0] * RAY_LIFT_MM,
        point[1] + normal[1] * RAY_LIFT_MM,
        point[2] + normal[2] * RAY_LIFT_MM,
    )


def _released(model: ShapeRays, faces: list[_Face], d, counter: list[int]) -> dict:
    """(face id, sample index) -> (releases into +d, releases into -d), in two ray batches.

    A point that faces away from a half is not cast toward it: it cannot release there.
    """
    minus = (-d[0], -d[1], -d[2])
    keys = {"plus": [], "minus": []}
    origins = {"plus": [], "minus": []}
    for face in faces:
        for index, (point, normal) in enumerate(face.samples):
            facing = _dot(normal, d)
            origin = _origin(point, normal)
            if facing >= -FACING_TOLERANCE:
                keys["plus"].append((face.face_id, index))
                origins["plus"].append(origin)
            if facing <= FACING_TOLERANCE:
                keys["minus"].append((face.face_id, index))
                origins["minus"].append(origin)
    released: dict = {}
    for sense, direction in (("plus", d), ("minus", minus)):
        counter[0] += len(origins[sense])
        answers = model.blocked_along(origins[sense], direction)
        for key, (evaluated, met) in zip(keys[sense], answers):
            plus_ok, minus_ok = released.get(key, (False, False))
            ok = evaluated and not met
            released[key] = (ok, minus_ok) if sense == "plus" else (plus_ok, ok)
    return released


def _evaluate(model: ShapeRays, faces: list[_Face], d, counter: list[int]) -> list[FaceDraft]:
    released = _released(model, faces, d, counter)
    results: list[FaceDraft] = []
    for face in faces:
        if not face.samples:
            results.append(
                FaceDraft(
                    face_id=face.face_id,
                    surface_type=face.surface_type,
                    area_mm2=round(face.area, OUTPUT_DECIMALS),
                    draft_deg=None,
                    draft_method="sampled_minimum",
                    zero_draft=False,
                    release=RELEASE_NOT_SAMPLED,
                    samples=0,
                    samples_undercut=0,
                )
            )
            continue
        halves: list[tuple[bool, bool]] = []
        least: tuple[float, tuple] | None = None
        first_undercut = None
        for index, (point, normal) in enumerate(face.samples):
            draft = math.degrees(math.asin(min(1.0, abs(_dot(normal, d)))))
            if least is None or draft < least[0]:
                least = (draft, point)
            plus_ok, minus_ok = released.get((face.face_id, index), (False, False))
            halves.append((plus_ok, minus_ok))
            if not (plus_ok or minus_ok) and first_undercut is None:
                first_undercut = point
        undercut = sum(1 for plus_ok, minus_ok in halves if not (plus_ok or minus_ok))
        if undercut:
            release = RELEASE_UNDERCUT
        elif all(p and m for p, m in halves):
            release = RELEASE_EITHER
        elif all(p for p, _ in halves):
            release = RELEASE_PLUS
        elif all(m for _, m in halves):
            release = RELEASE_MINUS
        else:
            release = RELEASE_SPLIT
        exact = _exact_draft(face, d)
        draft, method = exact if exact is not None else (least[0], "sampled_minimum")
        draft = round(draft, OUTPUT_DECIMALS)
        marker = first_undercut if first_undercut is not None else least[1]
        results.append(
            FaceDraft(
                face_id=face.face_id,
                surface_type=face.surface_type,
                area_mm2=round(face.area, OUTPUT_DECIMALS),
                draft_deg=draft,
                draft_method=method,
                zero_draft=draft <= ZERO_DRAFT_DEG,
                release=release,
                samples=len(halves),
                samples_undercut=undercut,
                marker=Point3D.of(*marker),
            )
        )
    return results


def _summarise(axis, reasons, order, engineer, drafts: list[FaceDraft]) -> CandidateAxis:
    undercuts = [face for face in drafts if face.release == RELEASE_UNDERCUT]
    zero = [face for face in drafts if face.zero_draft and face.release != RELEASE_UNDERCUT]
    return CandidateAxis(
        axis=Point3D.of(*axis),
        label=axis_label(axis),
        sources=list(reasons),
        order=order,
        engineer_entered=engineer,
        undercut_faces=len(undercuts),
        undercut_area_mm2=round(sum(face.area_mm2 for face in undercuts), OUTPUT_DECIMALS),
        zero_draft_faces=len(zero),
        zero_draft_area_mm2=round(sum(face.area_mm2 for face in zero), OUTPUT_DECIMALS),
    )


def _candidates(shape, faces: list[_Face]) -> list[tuple[tuple, list[str]]]:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    offered: list[tuple[tuple, str]] = [
        ((0.0, 0.0, 1.0), "the file's Z axis"),
        ((1.0, 0.0, 0.0), "the file's X axis"),
        ((0.0, 1.0, 0.0), "the file's Y axis"),
    ]
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    principal = properties.PrincipalProperties()
    moments = principal.Moments()
    axes = (principal.FirstAxisOfInertia(), principal.SecondAxisOfInertia(), principal.ThirdAxisOfInertia())
    for index, axis in enumerate(axes):
        mine = moments[index]
        others = [moments[k] for k in range(3) if k != index]
        scale = max(abs(m) for m in moments) or 1.0
        if any(abs(mine - other) <= DEGENERATE_MOMENT * scale for other in others):
            continue
        vector = (axis.X(), axis.Y(), axis.Z())
        offered.append((vector, f"a principal axis of inertia (moment {mine:.6g} mm^5)"))
    planes = sorted(
        (face for face in faces if face.plane_normal is not None),
        key=lambda face: (-round(face.area, OUTPUT_DECIMALS), face.face_id),
    )[:LARGEST_PLANAR_FACES]
    for face in planes:
        offered.append(
            (face.plane_normal, f"the normal of face {face.face_id}, one of the largest planar faces "
             f"({face.area:.6g} mm²)")
        )

    merged: list[tuple[tuple, list[str]]] = []
    for vector, reason in offered:
        unit = _canonical(normalise_axis(vector))
        for existing, reasons in merged:
            if abs(1.0 - abs(_dot(existing, unit))) < SAME_AXIS:
                reasons.append(reason)
                break
        else:
            merged.append((unit, [reason]))
    return merged


def measure_draw_direction(
    shape,
    *,
    filename: str = "",
    pull_axis: tuple[float, float, float] | None = None,
    max_rays: int | None = None,
) -> DrawDirectionResult:
    """Evaluate every candidate pull axis of an already-read B-Rep shape.

    `shape` is the `TopoDS_Shape` feature recognition ran on; this reads no
    file. `pull_axis`, when given, is the engineer's axis: it is evaluated too
    and is the axis the faces are reported against.
    """
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    started = time.perf_counter()
    budget = limits.MAX_DRAW_RAYS if max_rays is None else max_rays
    model = ShapeRays(shape)
    face_count = model.faces.Extent()

    in_solid: set[int] = set()
    solids = 0
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    while explorer.More():
        solids += 1
        sub = TopExp_Explorer(explorer.Current(), TopAbs_ShapeEnum.TopAbs_FACE)
        while sub.More():
            index = model.faces.FindIndex(TopoDS.Face_s(sub.Current()))
            if index > 0:
                in_solid.add(index)
            sub.Next()
        explorer.Next()
    if not solids:
        result = not_measured(
            "The model contains no closed solid, so which side of a face is material is not known "
            "and no ray can be said to leave the part.",
            filename=filename,
        )
        return result.model_copy(update={"face_count": face_count, "max_rays": budget})

    # A solid stored inside out has every normal pointing into it; its signed volume says so.
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    sign = -1.0 if properties.Mass() < 0 else 1.0

    face_ids = sorted(in_solid)
    engineer = _canonical(normalise_axis(pull_axis)) if pull_axis is not None else None
    budget_note = None
    faces: list[_Face] = []
    grids: tuple[int, ...] = ()
    candidates: list[tuple[tuple, list[str]]] = []
    for position, option in enumerate(GRIDS):
        faces = _faces(model, face_ids, option, sign)
        candidates = _candidates(shape, faces)
        axes = len(candidates) + (
            1
            if engineer is not None
            and not any(abs(1.0 - abs(_dot(axis, engineer))) < SAME_AXIS for axis, _ in candidates)
            else 0
        )
        # At most two rays per sample per axis: the bound, not the expectation.
        worst = 2 * axes * sum(len(face.samples) for face in faces)
        if worst <= budget:
            grids = option
            if position:
                budget_note = (
                    f"Sampling at {GRIDS[0][0]} x {GRIDS[0][0]} could cast up to "
                    f"{2 * axes * sum(len(f.samples) for f in _faces(model, face_ids, GRIDS[0], sign)):,} "
                    f"rays, over the {budget:,}-ray budget (CAD_COSTING_MAX_DRAW_RAYS), so every face "
                    f"was sampled at {option[0]} x {option[0]} instead. Fewer samples make a missed "
                    "undercut or a missed low draft more likely."
                )
            break
    else:
        result = not_measured(
            f"Even at {GRIDS[-1][0]} x {GRIDS[-1][0]} samples per face, checking "
            f"{len(candidates)} candidate axes could cast more than the {budget:,}-ray budget "
            "(CAD_COSTING_MAX_DRAW_RAYS). Geometry, the 3D preview and costing are unaffected.",
            filename=filename,
            budget_limited=True,
        )
        return result.model_copy(
            update={"face_count": face_count, "solid_count": solids, "max_rays": budget}
        )

    counter = [0]
    evaluated: list[tuple[CandidateAxis, list[FaceDraft]]] = []
    engineer_index = None
    for order, (axis, reasons) in enumerate(candidates):
        is_engineer = engineer is not None and abs(1.0 - abs(_dot(axis, engineer))) < SAME_AXIS
        if is_engineer:
            engineer_index = order
            reasons = [*reasons, ENGINEER_SOURCE]
        drafts = _evaluate(model, faces, axis, counter)
        evaluated.append((_summarise(axis, reasons, order, is_engineer, drafts), drafts))
    if engineer is not None and engineer_index is None:
        engineer_index = len(evaluated)
        drafts = _evaluate(model, faces, engineer, counter)
        evaluated.append(
            (_summarise(engineer, [ENGINEER_SOURCE], engineer_index, True, drafts), drafts)
        )

    # Only axes the geometry offered compete; an axis offered by the engineer alone does not.
    system = [entry for entry in evaluated if entry[0].sources != [ENGINEER_SOURCE]]
    best = min(
        system,
        key=lambda entry: (entry[0].undercut_faces, entry[0].zero_draft_area_mm2, entry[0].order),
    )
    best[0].recommended = True
    used = evaluated[engineer_index] if engineer_index is not None else best
    summary, drafts = used

    undercuts = [face for face in drafts if face.release == RELEASE_UNDERCUT]
    zero = [face for face in drafts if face.zero_draft and face.release != RELEASE_UNDERCUT]
    released = [
        face
        for face in drafts
        if face.draft_deg is not None and face.release not in (RELEASE_UNDERCUT, RELEASE_NOT_SAMPLED)
    ]
    least = min(released, key=lambda face: (face.draft_deg, face.face_id)) if released else None
    warnings = []
    unsampled = [face.face_id for face in drafts if face.release == RELEASE_NOT_SAMPLED]
    if unsampled:
        warnings.append(
            f"{len(unsampled)} face(s) had no grid point inside them and were not checked: "
            + ", ".join(str(i) for i in unsampled)
            + "."
        )
    outside = face_count - len(face_ids)
    if outside:
        warnings.append(f"{outside} face(s) belong to no solid and were not checked.")

    return DrawDirectionResult(
        filename=filename,
        measured=True,
        pull_axis=summary.axis,
        pull_axis_label=summary.label,
        axis_source="engineer_entered" if engineer_index is not None else "recommended",
        recommended_axis=best[0].axis,
        recommended_axis_label=best[0].label,
        candidates=[entry[0] for entry in evaluated],
        faces=drafts,
        undercut_face_count=len(undercuts),
        undercut_area_mm2=round(sum(face.area_mm2 for face in undercuts), OUTPUT_DECIMALS),
        undercut_face_ids=[face.face_id for face in undercuts],
        zero_draft_face_count=len(zero),
        zero_draft_area_mm2=round(sum(face.area_mm2 for face in zero), OUTPUT_DECIMALS),
        zero_draft_face_ids=[face.face_id for face in zero],
        minimum_draft_deg=least.draft_deg if least else None,
        minimum_draft_face=least.face_id if least else None,
        solid_count=solids,
        face_count=face_count,
        faces_outside_a_solid=outside,
        grid=list(grids),
        rays_cast=counter[0],
        max_rays=budget,
        budget_limited=budget_note is not None,
        budget_note=budget_note,
        warnings=warnings,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
    )


__all__ = [
    "GRIDS",
    "LIMITATIONS",
    "METHOD",
    "PARTING_NOTE",
    "RECOMMENDATION_BASIS",
    "CandidateAxis",
    "DrawDirectionResult",
    "FaceDraft",
    "axis_label",
    "measure_draw_direction",
    "normalise_axis",
    "not_measured",
    "refusal_reason",
]


# ---------------------------------------------------------------------------
# Proposed cores: the undercut faces, grouped into connected regions.
# ---------------------------------------------------------------------------

CORE_METHOD = "undercut_faces_connected_by_shared_edges"

CORE_METHOD_DESCRIPTION = (
    "The faces found undercut against the pull axis used are grouped into regions: two undercut "
    "faces are in one region when they share an edge, directly or through other undercut faces. "
    "Each region is proposed as one core or side action."
)

CORE_LIMITATIONS: tuple[str, ...] = (
    "A proposal, not a tool design: the engineer confirms or changes the count. Nothing is "
    "charged for a core until a count is confirmed on the forming stage.",
    "One region is one core only when a single straight side pull frees it. A region may need "
    "two slides, or two regions one, and an undercut can also be met by a loose piece, a "
    "collapsible core or later machining.",
    "Only undercuts the pull-direction measurement found are grouped. An undercut it missed "
    "between sample points is not proposed, so the count can be too low, never invented.",
)


class CoreRegion(BaseModel):
    """One connected group of undercut faces: one proposed core or side action."""

    region: int = Field(description="1-based, in order of the region's smallest face id.")
    face_ids: list[int] = Field(description="The undercut faces in the region, ascending.")
    area_mm2: float
    marker: Point3D | None = Field(
        default=None, description="The first undercut point of the region's first face; a pointer."
    )


class CoreProposal(BaseModel):
    """Undercut regions against one pull axis, each a proposed core. Proposed, never applied."""

    filename: str = ""
    measured: bool
    not_measured_reason: str | None = None
    method: str = CORE_METHOD
    method_description: str = CORE_METHOD_DESCRIPTION
    limitations: list[str] = Field(default_factory=lambda: list(CORE_LIMITATIONS))
    pull_axis: Point3D | None = None
    pull_axis_label: str | None = None
    axis_source: str | None = Field(default=None, description="'engineer_entered' or 'recommended'.")
    regions: list[CoreRegion] = Field(default_factory=list)
    proposed_core_count: int | None = Field(
        default=None,
        description=(
            "The number of regions. A proposal: it reaches a time only once the engineer enters "
            "a confirmed count on the forming stage, and that count may differ."
        ),
    )
    undercut_face_count: int = 0
    budget_limited: bool = False
    budget_note: str | None = None


def core_regions_not_measured(reason: str, *, filename: str = "") -> CoreProposal:
    return CoreProposal(filename=filename, measured=False, not_measured_reason=reason)


def _connected(face_ids: list[int], neighbours: dict[int, set[int]]) -> list[list[int]]:
    """Connected components of the undercut faces, each sorted, ordered by smallest id."""
    remaining = set(face_ids)
    regions: list[list[int]] = []
    for start in sorted(face_ids):
        if start not in remaining:
            continue
        remaining.discard(start)
        stack, region = [start], [start]
        while stack:
            face = stack.pop()
            for other in sorted(neighbours.get(face, ())):
                if other in remaining:
                    remaining.discard(other)
                    region.append(other)
                    stack.append(other)
        regions.append(sorted(region))
    return regions


def _undercut_neighbours(shape, face_ids: list[int]) -> dict[int, set[int]]:
    """Which of these faces share an edge, numbered as recognition numbers faces."""
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, faces)
    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(
        shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE, edge_faces
    )
    wanted = set(face_ids)
    neighbours: dict[int, set[int]] = {face: set() for face in face_ids}
    for index in range(1, edge_faces.Extent() + 1):
        touching = {faces.FindIndex(face) for face in edge_faces.FindFromIndex(index)}
        touching &= wanted
        for face in touching:
            neighbours[face] |= touching - {face}
    return neighbours


def propose_cores(shape, result: DrawDirectionResult) -> CoreProposal:
    """Group a pull-direction measurement's undercut faces into proposed cores.

    `shape` is the shape `result` was measured on; only its face adjacency is
    read. Nothing is measured again and no ray is cast.
    """
    if not result.measured:
        return core_regions_not_measured(
            result.not_measured_reason or "The pull direction was not measured.",
            filename=result.filename,
        ).model_copy(update={"budget_limited": result.budget_limited, "budget_note": result.budget_note})
    by_id = {face.face_id: face for face in result.faces}
    undercut = list(result.undercut_face_ids)
    regions = []
    for number, face_ids in enumerate(
        _connected(undercut, _undercut_neighbours(shape, undercut)), start=1
    ):
        regions.append(
            CoreRegion(
                region=number,
                face_ids=face_ids,
                area_mm2=round(sum(by_id[face].area_mm2 for face in face_ids), OUTPUT_DECIMALS),
                marker=by_id[face_ids[0]].marker,
            )
        )
    return CoreProposal(
        filename=result.filename,
        measured=True,
        pull_axis=result.pull_axis,
        pull_axis_label=result.pull_axis_label,
        axis_source=result.axis_source,
        regions=regions,
        proposed_core_count=len(regions),
        undercut_face_count=len(undercut),
        budget_limited=result.budget_limited,
        budget_note=result.budget_note,
    )


__all__ += [
    "CORE_LIMITATIONS",
    "CoreProposal",
    "CoreRegion",
    "core_regions_not_measured",
    "propose_cores",
]
