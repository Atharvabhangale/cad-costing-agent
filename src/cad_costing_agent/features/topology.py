"""One B-Rep traversal, producing OCP-free descriptors for the recognizers.

Why this exists
---------------
Every recognizer needs the same things: what kind of surface each face is, its
axis and radius, which edges bound it, which faces sit across those edges. If
each recognizer queried the shape itself, the model would be traversed five or
six times and every recognizer would carry its own OpenCASCADE imports.

So the shape is scanned **once** into plain dataclasses, and the recognizers are
pure functions over those. This is the only module the *recognizers* get their
geometry from, and no recognizer imports OCP, which is what keeps `model.py` — and
therefore the API and the UI — free of B-Rep concepts. (The measurements that cast
rays through the solid — which a descriptor cannot do — also import OCP:
`rays.py`, the one ray caster, and `wall_thickness.py` and `draw_direction.py`.
Each returns plain models too.)

Stable identity
---------------
Faces and edges are numbered by `TopExp::MapShapes`, which is deterministic for
a given shape: the same STEP file always produces the same numbering. Those
1-based indices are what `Feature.source_faces` reports, and they are what a
later step will use to highlight a feature in the 3D viewer.

Orientation and material side
-----------------------------
For a `gp_Cylinder` (and `gp_Cone`) OpenCASCADE's natural surface normal points
radially *away* from the axis. A face flagged `REVERSED` therefore has its
outward-from-material normal pointing *toward* the axis, which means the
material lies outside the surface — a bore. A `FORWARD` cylindrical face has
material inside it — a shaft or boss. That single flag is what separates a hole
from a boss, and it is verified by tests on fixtures containing both.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Tolerances. STEP is normalized to millimetres by the reader and OpenCASCADE
# keeps analytic surfaces exact, so these can be tight; they are deliberately
# named and gathered here so a real-world file that needs looser matching can be
# accommodated in one place.
LINEAR_TOLERANCE_MM = 1e-4
"""Two lengths, radii or positions within this are treated as equal (0.1 µm)."""

PARALLEL_TOLERANCE = 1e-6
"""Two unit directions are parallel when 1 - |dot| is below this (~0.08°)."""

FULL_SWEEP_TOLERANCE_RAD = 1e-4
"""How close to 2π a cylindrical face's angular extent must be to be a full bore."""

_TWO_PI = 2.0 * math.pi


@dataclass(frozen=True)
class Vec3:
    """A plain 3-vector. Deliberately not a pydantic model — this is internal."""

    x: float
    y: float
    z: float

    def __add__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def scaled(self, factor: float) -> "Vec3":
        return Vec3(self.x * factor, self.y * factor, self.z * factor)

    def dot(self, other: "Vec3") -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z

    def cross(self, other: "Vec3") -> "Vec3":
        return Vec3(
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x,
        )

    def length(self) -> float:
        return math.sqrt(self.dot(self))

    def normalized(self) -> "Vec3":
        magnitude = self.length()
        return self if magnitude == 0 else self.scaled(1.0 / magnitude)

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


def is_parallel(a: Vec3, b: Vec3) -> bool:
    """Same or opposite direction, within `PARALLEL_TOLERANCE`."""
    return abs(1.0 - abs(a.normalized().dot(b.normalized()))) < PARALLEL_TOLERANCE


def is_perpendicular(a: Vec3, b: Vec3) -> bool:
    return abs(a.normalized().dot(b.normalized())) < PARALLEL_TOLERANCE


def canonical_direction(direction: Vec3) -> Vec3:
    """Pick one of the two directions along an axis, so ±d compare equal.

    An axis has no inherent sign, but grouping coaxial faces needs a stable
    representative. The first significantly non-zero component is forced
    positive.
    """
    unit = direction.normalized()
    for component in (unit.z, unit.y, unit.x):
        if abs(component) > PARALLEL_TOLERANCE:
            return unit if component > 0 else unit.scaled(-1.0)
    return unit


def point_on_axis_nearest_origin(location: Vec3, direction: Vec3) -> Vec3:
    """Canonical point identifying an infinite line, independent of `location`.

    Two coaxial faces can report different axis locations along the same line;
    projecting the origin onto the line gives both the same representative.
    """
    unit = direction.normalized()
    return location - unit.scaled(location.dot(unit))


@dataclass
class EdgeDescriptor:
    index: int
    curve_type: str  # "line" | "circle" | "ellipse" | "bspline" | "other"
    length: float
    start: Vec3
    end: Vec3
    midpoint: Vec3
    face_indices: tuple[int, ...] = ()
    radius: float | None = None
    centre: Vec3 | None = None
    axis: Vec3 | None = None


@dataclass
class FaceDescriptor:
    index: int
    solid_index: int
    surface_type: str  # "plane" | "cylinder" | "cone" | "sphere" | "torus" | "other"
    area: float
    bbox_min: Vec3
    bbox_max: Vec3
    edge_indices: tuple[int, ...] = ()
    outer_edge_indices: tuple[int, ...] = ()
    inner_edge_indices: tuple[int, ...] = ()
    # Planes
    normal: Vec3 | None = None  # outward from material
    origin: Vec3 | None = None
    # Cylinders and cones
    axis_direction: Vec3 | None = None
    axis_location: Vec3 | None = None
    radius: float | None = None
    semi_angle: float | None = None
    angular_extent: float | None = None
    axial_start: float | None = None  # parameter along the axis from axis_location
    axial_end: float | None = None
    is_internal: bool | None = None  # material lies outside the surface

    @property
    def axial_extent(self) -> float | None:
        if self.axial_start is None or self.axial_end is None:
            return None
        return self.axial_end - self.axial_start

    @property
    def is_full_sweep(self) -> bool:
        return (
            self.angular_extent is not None
            and abs(self.angular_extent - _TWO_PI) < FULL_SWEEP_TOLERANCE_RAD
        )

    def axis_point(self, parameter: float) -> Vec3:
        """Absolute point on the axis at the given axial parameter."""
        assert self.axis_location is not None and self.axis_direction is not None
        return self.axis_location + self.axis_direction.normalized().scaled(parameter)


@dataclass
class SolidDescriptor:
    index: int
    face_indices: tuple[int, ...]
    edge_indices: tuple[int, ...]
    volume: float
    bbox_min: Vec3
    bbox_max: Vec3


@dataclass
class TopologyModel:
    """The whole scanned model. Recognizers read this and nothing else."""

    faces: dict[int, FaceDescriptor]
    edges: dict[int, EdgeDescriptor]
    solids: list[SolidDescriptor]
    warnings: list[str] = field(default_factory=list)

    def faces_of_solid(self, solid_index: int) -> list[FaceDescriptor]:
        return [self.faces[i] for i in self.solids[solid_index].face_indices if i in self.faces]

    def faces_across(self, edge_index: int, from_face: int) -> list[FaceDescriptor]:
        """The other face(s) sharing this edge."""
        edge = self.edges.get(edge_index)
        if edge is None:
            return []
        return [self.faces[i] for i in edge.face_indices if i != from_face and i in self.faces]

    def neighbours(self, face: FaceDescriptor) -> dict[int, list[int]]:
        """Adjacent face index -> the edges shared with it."""
        shared: dict[int, list[int]] = {}
        for edge_index in face.edge_indices:
            for other in self.faces_across(edge_index, face.index):
                shared.setdefault(other.index, []).append(edge_index)
        return shared

    def model_diagonal(self) -> float:
        if not self.solids:
            return 0.0
        low = self.solids[0].bbox_min
        high = self.solids[0].bbox_max
        for solid in self.solids[1:]:
            low = Vec3(min(low.x, solid.bbox_min.x), min(low.y, solid.bbox_min.y), min(low.z, solid.bbox_min.z))
            high = Vec3(max(high.x, solid.bbox_max.x), max(high.y, solid.bbox_max.y), max(high.z, solid.bbox_max.z))
        return (high - low).length()


# ---------------------------------------------------------------------------
# The scan itself. Everything below this line touches OpenCASCADE.
# ---------------------------------------------------------------------------


def build_topology(shape) -> TopologyModel:
    """Scan a `TopoDS_Shape` once into descriptors.

    Per-face and per-edge extraction is individually guarded: a single face
    that OpenCASCADE cannot describe degrades into a warning and an "other"
    surface type rather than aborting recognition of the whole model.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import (
        TopTools_IndexedDataMapOfShapeListOfShape,
        TopTools_IndexedMapOfShape,
    )

    warnings: list[str] = []

    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, face_map)
    edge_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, edge_map)

    edge_to_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(
        shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE, edge_to_faces
    )

    def to_vec(point) -> Vec3:
        return Vec3(point.X(), point.Y(), point.Z())

    def bounds_of(sub_shape) -> tuple[Vec3, Vec3]:
        box = Bnd_Box()
        BRepBndLib.Add_s(sub_shape, box)
        if box.IsVoid():
            return Vec3(0.0, 0.0, 0.0), Vec3(0.0, 0.0, 0.0)
        x0, y0, z0, x1, y1, z1 = box.Get()
        return Vec3(x0, y0, z0), Vec3(x1, y1, z1)

    # --- solids, and the face -> solid attribution ------------------------
    solid_of_face: dict[int, int] = {}
    solids: list[SolidDescriptor] = []
    solid_shapes = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    while explorer.More():
        solid_shapes.append(explorer.Current())
        explorer.Next()

    if not solid_shapes:
        # A shell or a loose set of faces still has recognizable geometry; it
        # just cannot be attributed to a solid, and volume is meaningless.
        warnings.append(
            "The model contains no closed solid, so all features are reported under a "
            "single pseudo-solid and no volume is available."
        )
        solid_shapes = [shape]

    for solid_index, solid_shape in enumerate(solid_shapes):
        face_indices: list[int] = []
        sub = TopExp_Explorer(solid_shape, TopAbs_ShapeEnum.TopAbs_FACE)
        while sub.More():
            index = face_map.FindIndex(sub.Current())
            if index > 0:
                face_indices.append(index)
                solid_of_face.setdefault(index, solid_index)
            sub.Next()

        edge_indices: list[int] = []
        sub = TopExp_Explorer(solid_shape, TopAbs_ShapeEnum.TopAbs_EDGE)
        while sub.More():
            index = edge_map.FindIndex(sub.Current())
            if index > 0:
                edge_indices.append(index)
            sub.Next()

        volume = 0.0
        try:
            properties = GProp_GProps()
            BRepGProp.VolumeProperties_s(solid_shape, properties)
            volume = abs(float(properties.Mass()))
        except Exception:  # noqa: BLE001 - an open shell has no meaningful volume
            warnings.append(f"Solid {solid_index} has no computable volume.")

        low, high = bounds_of(solid_shape)
        solids.append(
            SolidDescriptor(
                index=solid_index,
                face_indices=tuple(sorted(set(face_indices))),
                edge_indices=tuple(sorted(set(edge_indices))),
                volume=volume,
                bbox_min=low,
                bbox_max=high,
            )
        )

    # --- edges -------------------------------------------------------------
    edges: dict[int, EdgeDescriptor] = {}
    for index in range(1, edge_map.Extent() + 1):
        edge_shape = TopoDS.Edge_s(edge_map.FindKey(index))
        faces_here: tuple[int, ...] = ()
        if edge_to_faces.Contains(edge_shape):
            # OCP exposes TopTools_ListOfShape as a plain Python iterable.
            collected = [face_map.FindIndex(adjacent) for adjacent in edge_to_faces.FindFromKey(edge_shape)]
            faces_here = tuple(sorted({index for index in collected if index > 0}))

        try:
            curve = BRepAdaptor_Curve(edge_shape)
            first, last = curve.FirstParameter(), curve.LastParameter()
            start = to_vec(curve.Value(first))
            end = to_vec(curve.Value(last))
            midpoint = to_vec(curve.Value((first + last) / 2.0))

            properties = GProp_GProps()
            BRepGProp.LinearProperties_s(edge_shape, properties)
            length = float(properties.Mass())

            curve_type = curve.GetType()
            if curve_type == GeomAbs_CurveType.GeomAbs_Line:
                kind, radius, centre, axis = "line", None, None, None
            elif curve_type == GeomAbs_CurveType.GeomAbs_Circle:
                circle = curve.Circle()
                kind = "circle"
                radius = float(circle.Radius())
                centre = to_vec(circle.Location())
                axis = to_vec(circle.Axis().Direction())
            elif curve_type == GeomAbs_CurveType.GeomAbs_Ellipse:
                kind, radius, centre, axis = "ellipse", None, to_vec(curve.Ellipse().Location()), None
            elif curve_type == GeomAbs_CurveType.GeomAbs_BSplineCurve:
                kind, radius, centre, axis = "bspline", None, None, None
            else:
                kind, radius, centre, axis = "other", None, None, None

            edges[index] = EdgeDescriptor(
                index=index,
                curve_type=kind,
                length=length,
                start=start,
                end=end,
                midpoint=midpoint,
                face_indices=faces_here,
                radius=radius,
                centre=centre,
                axis=axis,
            )
        except Exception as exc:  # noqa: BLE001 - one unreadable edge must not stop the scan
            warnings.append(f"Edge {index} could not be described ({type(exc).__name__}).")
            zero = Vec3(0.0, 0.0, 0.0)
            edges[index] = EdgeDescriptor(
                index=index,
                curve_type="other",
                length=0.0,
                start=zero,
                end=zero,
                midpoint=zero,
                face_indices=faces_here,
            )

    # --- faces -------------------------------------------------------------
    faces: dict[int, FaceDescriptor] = {}
    for index in range(1, face_map.Extent() + 1):
        face_shape = TopoDS.Face_s(face_map.FindKey(index))
        solid_index = solid_of_face.get(index, 0)

        all_edges: list[int] = []
        sub = TopExp_Explorer(face_shape, TopAbs_ShapeEnum.TopAbs_EDGE)
        while sub.More():
            edge_index = edge_map.FindIndex(sub.Current())
            if edge_index > 0:
                all_edges.append(edge_index)
            sub.Next()
        # A full cylinder's seam edge is visited twice; dedupe.
        unique_edges = tuple(sorted(set(all_edges)))

        outer_edges: tuple[int, ...] = ()
        try:
            outer_wire = BRepTools.OuterWire_s(face_shape)
            collected = []
            sub = TopExp_Explorer(outer_wire, TopAbs_ShapeEnum.TopAbs_EDGE)
            while sub.More():
                edge_index = edge_map.FindIndex(sub.Current())
                if edge_index > 0:
                    collected.append(edge_index)
                sub.Next()
            outer_edges = tuple(sorted(set(collected)))
        except Exception:  # noqa: BLE001 - without an outer wire, treat every edge as outer
            outer_edges = unique_edges
        inner_edges = tuple(sorted(set(unique_edges) - set(outer_edges)))

        area = 0.0
        try:
            properties = GProp_GProps()
            BRepGProp.SurfaceProperties_s(face_shape, properties)
            area = float(properties.Mass())
        except Exception:  # noqa: BLE001
            warnings.append(f"Face {index} has no computable area.")

        low, high = bounds_of(face_shape)
        descriptor = FaceDescriptor(
            index=index,
            solid_index=solid_index,
            surface_type="other",
            area=area,
            bbox_min=low,
            bbox_max=high,
            edge_indices=unique_edges,
            outer_edge_indices=outer_edges,
            inner_edge_indices=inner_edges,
        )

        try:
            surface = BRepAdaptor_Surface(face_shape)
            surface_type = surface.GetType()
            reversed_face = face_shape.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
            u_min, u_max, v_min, v_max = BRepTools.UVBounds_s(face_shape)

            if surface_type == GeomAbs_SurfaceType.GeomAbs_Plane:
                plane = surface.Plane()
                normal = to_vec(plane.Axis().Direction()).normalized()
                if reversed_face:
                    normal = normal.scaled(-1.0)
                descriptor.surface_type = "plane"
                descriptor.normal = normal
                descriptor.origin = to_vec(plane.Location())

            elif surface_type == GeomAbs_SurfaceType.GeomAbs_Cylinder:
                cylinder = surface.Cylinder()
                descriptor.surface_type = "cylinder"
                descriptor.axis_direction = to_vec(cylinder.Axis().Direction()).normalized()
                descriptor.axis_location = to_vec(cylinder.Axis().Location())
                descriptor.radius = float(cylinder.Radius())
                descriptor.angular_extent = float(u_max - u_min)
                descriptor.axial_start = float(v_min)
                descriptor.axial_end = float(v_max)
                # See the module docstring: the natural normal points away from
                # the axis, so a reversed face has material outside it.
                descriptor.is_internal = reversed_face

            elif surface_type == GeomAbs_SurfaceType.GeomAbs_Cone:
                cone = surface.Cone()
                descriptor.surface_type = "cone"
                descriptor.axis_direction = to_vec(cone.Axis().Direction()).normalized()
                descriptor.axis_location = to_vec(cone.Axis().Location())
                descriptor.radius = float(cone.RefRadius())
                descriptor.semi_angle = float(cone.SemiAngle())
                descriptor.angular_extent = float(u_max - u_min)
                descriptor.axial_start = float(v_min)
                descriptor.axial_end = float(v_max)
                descriptor.is_internal = reversed_face

            elif surface_type == GeomAbs_SurfaceType.GeomAbs_Sphere:
                descriptor.surface_type = "sphere"
                descriptor.radius = float(surface.Sphere().Radius())
                descriptor.origin = to_vec(surface.Sphere().Location())

            elif surface_type == GeomAbs_SurfaceType.GeomAbs_Torus:
                descriptor.surface_type = "torus"
                descriptor.axis_direction = to_vec(surface.Torus().Axis().Direction()).normalized()
                descriptor.axis_location = to_vec(surface.Torus().Axis().Location())
                descriptor.radius = float(surface.Torus().MinorRadius())
        except Exception as exc:  # noqa: BLE001 - keep the face, lose only its classification
            warnings.append(f"Face {index} could not be classified ({type(exc).__name__}).")

        faces[index] = descriptor

    return TopologyModel(faces=faces, edges=edges, solids=solids, warnings=warnings)
