"""Measured wall thickness of an exact B-Rep solid.

    CAD file -> B-Rep (the shape recognition already read) -> **wall thickness**

Analysis only. This module measures; it decides nothing. It is read by the
`/cad/wall-thickness` endpoint and by the minimum-wall DFM rule, and by nothing
that reaches a mass, a time, a route or a cost. It imports `features.model`,
`features.topology`'s tolerance and `limits` from this package and nothing
else; a structural test holds that.

The method: rays cast inward from a fixed grid
-----------------------------------------------
Every face of every solid is sampled on a fixed `n x n` grid over its parameter
range (sample centres at `(i + 0.5) / n`, so no sample sits on a boundary edge),
keeping only the points the face's own trimming classifies as inside it. From
each sample a ray is cast along the face's inward normal (into the material)
and the first face of the same solid it leaves through is the far side of the
wall. The distance between the two points is the wall at that sample.

It was chosen over pairing opposite planar faces because it measures every
surface type the same way — a tube's wall (two coaxial cylinders) and a
freeform wall come out of the same code as a plate — and because each value is
an actual distance through material from an actual point, not an inference
from two faces' projections. Two parallel planes and two coaxial cylinders give
the separation exactly (an analytic line/plane or line/cylinder intersection),
which is what the fixtures test.

What it misses, and the response says so (`LIMITATIONS`):

- A thin spot between sample points is not seen. The reported minimum is the
  thinnest wall *found*: the part's true minimum can be thinner, never thicker,
  because every value is a real distance through material.
- The distance is taken along one face's normal. Where the far face is not
  parallel to it (a draft, a taper, a fillet) that reads longer than the
  narrowest gap, so tapering walls read thicker than they are.
- Only an exact B-Rep with a closed solid is measured. Faceted exports, STL and
  IGES surface models are refused with a reason (`refusal_reason`).

Deterministic: the grid is fixed, faces are numbered by `TopExp::MapShapes`
exactly as `topology.build_topology` numbers them (so a face id here is the id
in a feature's `source_faces`), and every output is rounded to
`OUTPUT_DECIMALS`. No randomness anywhere.

Budget: `limits.MAX_WALL_RAYS` bounds the rays one measurement casts. Over it,
the grid is coarsened for every face alike and the result says so
(`budget_limited`); if even one ray per face would exceed it, nothing is
measured. Either way only this analysis degrades — never an estimate.
"""

from __future__ import annotations

import math
import time

import numpy as np
from pydantic import BaseModel, Field

from cad_costing_agent import limits
from cad_costing_agent.features.model import OUTPUT_DECIMALS, Point3D
from cad_costing_agent.features.topology import LINEAR_TOLERANCE_MM

METHOD = "inward_normal_ray_grid"

DEFAULT_GRID_SIDE = 5
"""Samples per face along each parameter direction, so 25 rays per face."""

HISTOGRAM_EDGES_MM: tuple[float, ...] = (0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0)
"""Fixed bin edges for the distribution of sampled walls, in millimetres. Fixed
rather than fitted to the part, so two parts' histograms can be compared."""

RAY_START_OFFSET_MM = LINEAR_TOLERANCE_MM
"""Intersections closer than this to the ray's start are the start face itself.
A wall thinner than 0.1 µm is not measured, which is no loss."""

BOX_MARGIN_MM = 1e-3
"""Each face's bounding box is grown by this before the ray filter, so a ray
grazing a box's edge is still handed to the exact intersection."""

TANGENT_DOT = 1e-9
"""A hit whose face normal is this close to perpendicular to the ray is a graze,
not a passage through the surface, and is passed over."""

METHOD_DESCRIPTION = (
    "Each face of each solid is sampled on a fixed grid over its parameter range, keeping "
    "only points inside the face's own boundary. From each point a ray is cast along the "
    "face's inward normal, into the material, and the wall there is the distance to the "
    "first face of the same solid the ray leaves through. The minimum is the shortest such "
    "distance found."
)

LIMITATIONS: tuple[str, ...] = (
    "A thin spot that falls between sample points is not seen. The minimum reported is the "
    "thinnest wall found at the sampled points: the part's true minimum can be thinner than "
    "this, never thicker, because every value is a real distance through material.",
    "The distance is measured along one face's normal. Where the opposite face is not "
    "parallel to it (a draft, a taper, a fillet or a cone) that reads longer than the "
    "narrowest gap, so a tapering wall reads thicker than it is.",
    "Only exact B-Rep geometry bounding a closed solid is measured. Faceted exports, STL "
    "meshes and IGES surface models are not measured, and faces outside a solid are skipped.",
)


class WallSpan(BaseModel):
    """One measured wall: from a sampled point, through material, to the far face."""

    thickness_mm: float
    from_face: int = Field(description="The sampled face (1-based, as `source_faces` numbers faces).")
    to_face: int = Field(description="The face the ray left the material through.")
    start: Point3D = Field(description="The sample point on `from_face`.")
    end: Point3D = Field(description="Where the ray left the material, on `to_face`.")
    midpoint: Point3D = Field(description="Halfway through the wall: where a marker is drawn.")
    direction: Point3D = Field(description="Unit direction of the ray, from `start` into the material.")


class FaceWall(BaseModel):
    face_id: int
    solid_index: int
    surface_type: str
    samples: int = Field(description="Grid points inside the face that produced a wall.")
    minimum_mm: float | None = Field(description="Null when no sample on the face produced a wall.")
    opposite_face: int | None = None


class HistogramBin(BaseModel):
    lower_mm: float
    upper_mm: float | None = Field(description="Exclusive; null for the open top bin.")
    samples: int


class WallThicknessResult(BaseModel):
    filename: str = ""
    measured: bool
    method: str = METHOD
    method_description: str = METHOD_DESCRIPTION
    limitations: list[str] = Field(default_factory=lambda: list(LIMITATIONS))
    not_measured_reason: str | None = None

    minimum: WallSpan | None = Field(
        default=None, description="The thinnest wall found. Null when nothing was measured."
    )
    faces: list[FaceWall] = Field(
        default_factory=list, description="Per-face minimum, in face-id order."
    )
    histogram: list[HistogramBin] = Field(
        default_factory=list,
        description="Every sampled wall, counted into fixed bins (`HISTOGRAM_EDGES_MM`).",
    )

    solid_count: int = 0
    face_count: int = 0
    faces_measured: int = 0
    faces_without_samples: list[int] = Field(
        default_factory=list,
        description="Faces where no grid point fell inside the face or no ray found a wall.",
    )
    faces_outside_a_solid: int = 0
    unmeasured_surface_types: dict[str, int] = Field(
        default_factory=dict,
        description="How many faces of each surface type produced no wall.",
    )
    grid_side: int = 0
    rays_cast: int = 0
    rays_without_exit: int = Field(
        default=0,
        description=(
            "Rays that left no face of their solid, or met one from outside first. Either means "
            "the solid's faces do not enclose material consistently at that point."
        ),
    )
    max_rays: int = 0
    budget_limited: bool = False
    budget_note: str | None = None
    warnings: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0.0


def _p(x: float, y: float, z: float) -> Point3D:
    return Point3D.of(x, y, z)


def _histogram(values: list[float]) -> list[HistogramBin]:
    edges = (0.0,) + HISTOGRAM_EDGES_MM
    counts = [0] * len(edges)
    for value in values:
        slot = 0
        for index, lower in enumerate(edges):
            if value >= lower:
                slot = index
        counts[slot] += 1
    return [
        HistogramBin(
            lower_mm=edges[i],
            upper_mm=edges[i + 1] if i + 1 < len(edges) else None,
            samples=counts[i],
        )
        for i in range(len(edges))
    ]


def refusal_reason(representation: str, has_shape: bool) -> str | None:
    """Why a recognized upload cannot have its walls measured, or None if it can.

    `representation` is the recognition result's own (`brep`, `faceted`,
    `surface_model`); nothing about the file is re-read or guessed here.
    """
    if representation == "faceted" or not has_shape:
        return (
            "Wall thickness is measured on exact B-Rep geometry only. This file is a faceted "
            "export: flat polygons reconstructed into features by fitting, which is weaker "
            "evidence, and a wall measured between polygons would report the tessellation as "
            "much as the part. Re-export the part as solid/B-Rep STEP to measure its walls."
        )
    if representation == "surface_model":
        return (
            "Wall thickness needs a closed solid as the file itself states it. This IGES file "
            "stores loose trimmed surfaces, so which side of a surface is material is not "
            "recorded. Re-export the IGES with solid/B-Rep geometry, or export STEP instead."
        )
    if representation != "brep":
        return f"Wall thickness is not measured for '{representation}' geometry."
    return None


def not_measured(reason: str, *, filename: str = "", budget_limited: bool = False) -> WallThicknessResult:
    return WallThicknessResult(
        filename=filename,
        measured=False,
        not_measured_reason=reason,
        budget_limited=budget_limited,
        max_rays=limits.MAX_WALL_RAYS,
    )


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


def measure_wall_thickness(
    shape,
    *,
    filename: str = "",
    max_rays: int | None = None,
    grid_side: int = DEFAULT_GRID_SIDE,
) -> WallThicknessResult:
    """Measure the walls of every solid in an already-read B-Rep shape.

    `shape` is the `TopoDS_Shape` feature recognition ran on; this reads no
    file. Pure in the sense that matters: the same shape gives the same result.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp, BRepGProp_Face
    from OCP.BRepTools import BRepTools
    from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt, gp_Pnt2d, gp_Vec
    from OCP.GProp import GProp_GProps
    from OCP.IntCurvesFace import IntCurvesFace_Intersector
    from OCP.Precision import Precision
    from OCP.TopAbs import TopAbs_ShapeEnum, TopAbs_State
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    started = time.perf_counter()
    budget = limits.MAX_WALL_RAYS if max_rays is None else max_rays

    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, face_map)
    face_count = face_map.Extent()

    solids = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    while explorer.More():
        solids.append(explorer.Current())
        explorer.Next()

    if not solids:
        result = not_measured(
            "The model contains no closed solid, so no side of any face is known to be material "
            "and no wall can be measured.",
            filename=filename,
        )
        return result.model_copy(update={"face_count": face_count, "max_rays": budget})

    # Which faces belong to a solid, attributed to the first solid that has them.
    owner: dict[int, int] = {}
    solid_faces: list[list[tuple[int, object]]] = []
    for solid_index, solid in enumerate(solids):
        mine: list[tuple[int, object]] = []
        sub = TopExp_Explorer(solid, TopAbs_ShapeEnum.TopAbs_FACE)
        while sub.More():
            face = TopoDS.Face_s(sub.Current())
            index = face_map.FindIndex(face)
            if index > 0 and index not in owner:
                owner[index] = solid_index
                mine.append((index, face))
            sub.Next()
        solid_faces.append(mine)
    measurable = len(owner)
    outside = face_count - measurable

    # The budget coarsens every face's grid alike, so no face is favoured.
    side = grid_side
    budget_note = None
    if measurable * side * side > budget:
        side = int(math.isqrt(budget // measurable)) if measurable else 0
        if side < 1:
            result = not_measured(
                f"This model has {measurable:,} faces in solids, and even one ray per face would "
                f"exceed the {budget:,}-ray wall-thickness budget (CAD_COSTING_MAX_WALL_RAYS). "
                "Geometry, the 3D preview and costing are unaffected.",
                filename=filename,
                budget_limited=True,
            )
            return result.model_copy(
                update={
                    "face_count": face_count,
                    "solid_count": len(solids),
                    "faces_outside_a_solid": outside,
                    "max_rays": budget,
                }
            )
        budget_note = (
            f"{measurable:,} faces at {grid_side} x {grid_side} samples would exceed the "
            f"{budget:,}-ray budget (CAD_COSTING_MAX_WALL_RAYS), so every face was sampled at "
            f"{side} x {side} instead. Fewer samples make a missed thin spot more likely."
        )

    tolerance = Precision.Confusion_s()

    def oriented_normal(face, u: float, v: float, sign: float):
        point = gp_Pnt()
        normal = gp_Vec()
        BRepGProp_Face(face).Normal(u, v, point, normal)
        magnitude = normal.Magnitude()
        if magnitude <= 1e-12:
            return point, None
        return point, normal.Divided(magnitude * sign)

    per_face: dict[int, FaceWall] = {}
    all_values: list[float] = []
    best: tuple[float, int, int, object, object, object] | None = None
    rays_cast = 0
    no_exit = 0
    warnings: list[str] = []

    for solid_index, solid in enumerate(solids):
        # A solid stored inside out has every face normal pointing into it. Its
        # signed volume says so once, rather than every ray discovering it.
        properties = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, properties)
        sign = -1.0 if properties.Mass() < 0 else 1.0

        # Each ray is intersected exactly, but only with the faces whose bounding
        # box it passes through: a box test is a filter that can only admit too
        # many faces, never drop one the ray meets, so the answer is unchanged
        # and the cost stops growing with faces x rays.
        members = sorted(solid_faces[solid_index], key=lambda item: item[0])
        member_faces = [face for _, face in members]
        member_ids = [face_id for face_id, _ in members]
        low = np.empty((len(members), 3))
        high = np.empty((len(members), 3))
        for slot, face in enumerate(member_faces):
            box = Bnd_Box()
            BRepBndLib.Add_s(face, box)
            box.Enlarge(BOX_MARGIN_MM)
            x0, y0, z0, x1, y1, z1 = box.Get()
            low[slot] = (x0, y0, z0)
            high[slot] = (x1, y1, z1)
        intersectors: dict[int, object] = {}

        def candidates(origins, directions):
            """Per ray: which faces' boxes it passes through, and how far along it enters each."""
            safe = np.where(directions == 0.0, 1e-300, directions)[:, None, :]
            with np.errstate(over="ignore", divide="ignore"):
                t1 = (low[None, :, :] - origins[:, None, :]) / safe
                t2 = (high[None, :, :] - origins[:, None, :]) / safe
            near = np.minimum(t1, t2).max(axis=2)
            far = np.maximum(t1, t2).min(axis=2)
            entry = np.maximum(near, 0.0)
            return far >= entry, entry

        for face_id, face in members:
            surface_type = _SURFACE_NAMES.get(
                BRepAdaptor_Surface(face).GetType().name, "other"
            )
            u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
            classifier = BRepTopAdaptor_FClass2d(face, tolerance)
            face_min: tuple[float, int] | None = None
            samples = 0

            rays = []
            for i in range(side):
                u = u0 + (i + 0.5) / side * (u1 - u0)
                for j in range(side):
                    v = v0 + (j + 0.5) / side * (v1 - v0)
                    if classifier.Perform(gp_Pnt2d(u, v)) != TopAbs_State.TopAbs_IN:
                        continue
                    start, normal = oriented_normal(face, u, v, sign)
                    if normal is None:
                        continue
                    # Outward normal reversed: into the material.
                    rays.append((start, gp_Dir(-normal.X(), -normal.Y(), -normal.Z())))
            if not rays:
                per_face[face_id] = FaceWall(
                    face_id=face_id,
                    solid_index=solid_index,
                    surface_type=surface_type,
                    samples=0,
                    minimum_mm=None,
                )
                continue

            passes, entries = candidates(
                np.array([(p.X(), p.Y(), p.Z()) for p, _ in rays]),
                np.array([(d.X(), d.Y(), d.Z()) for _, d in rays]),
            )
            for row, (start, inward) in enumerate(rays):
                ray = gp_Lin(start, inward)
                rays_cast += 1
                # Faces in the order the ray reaches their boxes. Once the nearest
                # resolved crossing is closer than the next box's entry, no face
                # still unvisited can be crossed sooner, so the answer is final:
                # the same one intersecting every candidate would give.
                slots = np.nonzero(passes[row])[0].tolist()
                slots.sort(key=lambda slot: (entries[row][slot], slot))
                hits: list[tuple] = []
                resolved: dict[int, float] = {}
                exit_hit = None
                decided = False
                for position, slot in enumerate(slots):
                    crossing = intersectors.get(slot)
                    if crossing is None:
                        crossing = IntCurvesFace_Intersector(member_faces[slot], tolerance)
                        intersectors[slot] = crossing
                    crossing.Perform(ray, RAY_START_OFFSET_MM, math.inf)
                    for k in range(1, crossing.NbPnt() + 1):
                        hits.append(
                            (
                                crossing.WParameter(k),
                                member_ids[slot],
                                crossing.UParameter(k),
                                crossing.VParameter(k),
                                crossing.Pnt(k),
                                slot,
                            )
                        )
                    following = (
                        float(entries[row][slots[position + 1]])
                        if position + 1 < len(slots)
                        else math.inf
                    )
                    hits.sort(key=lambda hit: (hit[0], hit[1]))
                    for index, (distance, hit_id, hu, hv, hit_point, hit_slot) in enumerate(hits):
                        if distance >= following:
                            break
                        alignment = resolved.get(index)
                        if alignment is None:
                            _, hit_normal = oriented_normal(member_faces[hit_slot], hu, hv, sign)
                            alignment = (
                                0.0
                                if hit_normal is None
                                else hit_normal.X() * inward.X()
                                + hit_normal.Y() * inward.Y()
                                + hit_normal.Z() * inward.Z()
                            )
                        if abs(alignment) <= TANGENT_DOT:
                            continue
                        if alignment > 0:
                            exit_hit = (distance, hit_id, hit_point)
                        decided = True
                        break
                    if decided:
                        break
                if exit_hit is None:
                    no_exit += 1
                    continue
                distance, to_face, end = exit_hit
                samples += 1
                all_values.append(distance)
                if face_min is None or distance < face_min[0]:
                    face_min = (distance, to_face)
                # Ties keep the first found, in face-id then grid order.
                if best is None or distance < best[0]:
                    best = (distance, face_id, to_face, start, end, inward)
            per_face[face_id] = FaceWall(
                face_id=face_id,
                solid_index=solid_index,
                surface_type=surface_type,
                samples=samples,
                minimum_mm=round(face_min[0], OUTPUT_DECIMALS) if face_min else None,
                opposite_face=face_min[1] if face_min else None,
            )

    faces = [per_face[face_id] for face_id in sorted(per_face)]
    without = [face.face_id for face in faces if face.minimum_mm is None]
    unmeasured_types: dict[str, int] = {}
    for face in faces:
        if face.minimum_mm is None:
            unmeasured_types[face.surface_type] = unmeasured_types.get(face.surface_type, 0) + 1

    if no_exit:
        warnings.append(
            f"{no_exit} of {rays_cast} rays found no face to leave the material through, or met "
            "a face from outside first. The solid's faces may not enclose material consistently "
            "there; those samples are left out rather than guessed."
        )
    if outside:
        warnings.append(f"{outside} face(s) belong to no solid and were not measured.")

    minimum = None
    if best is not None:
        distance, from_face, to_face, start, end, inward = best
        minimum = WallSpan(
            thickness_mm=round(distance, OUTPUT_DECIMALS),
            from_face=from_face,
            to_face=to_face,
            start=_p(start.X(), start.Y(), start.Z()),
            end=_p(end.X(), end.Y(), end.Z()),
            midpoint=_p(
                (start.X() + end.X()) / 2.0,
                (start.Y() + end.Y()) / 2.0,
                (start.Z() + end.Z()) / 2.0,
            ),
            direction=_p(inward.X(), inward.Y(), inward.Z()),
        )

    return WallThicknessResult(
        filename=filename,
        measured=minimum is not None,
        not_measured_reason=(
            None
            if minimum is not None
            else "No sample on any face produced a wall: no grid point fell inside a face, or "
            "no ray found a face to leave the material through."
        ),
        minimum=minimum,
        faces=faces,
        histogram=_histogram(all_values),
        solid_count=len(solids),
        face_count=face_count,
        faces_measured=len(faces) - len(without),
        faces_without_samples=without,
        faces_outside_a_solid=outside,
        unmeasured_surface_types=unmeasured_types,
        grid_side=side,
        rays_cast=rays_cast,
        rays_without_exit=no_exit,
        max_rays=budget,
        budget_limited=budget_note is not None,
        budget_note=budget_note,
        warnings=warnings,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
    )


__all__ = [
    "DEFAULT_GRID_SIDE",
    "HISTOGRAM_EDGES_MM",
    "LIMITATIONS",
    "METHOD",
    "FaceWall",
    "HistogramBin",
    "WallSpan",
    "WallThicknessResult",
    "measure_wall_thickness",
    "not_measured",
    "refusal_reason",
]
