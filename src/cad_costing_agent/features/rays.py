"""Straight rays against an exact B-Rep, and points sampled on its faces.

    the shape recognition read -> sampled face points -> first material along a ray

Shared by the two analyses that ask "does a straight line from this face leave
the part?": tool approach directions (`features.approach`) and the pull
direction of a mould or die (`features.draw_direction`). It was moved here from
`approach.py` unchanged, so both cast the same rays from the same points and
there is one ray caster, not two.

It measures nothing on its own and decides nothing. It imports no module of
this package; everything it reads is the `TopoDS_Shape` it is handed. Faces are
numbered by `TopExp::MapShapes` exactly as `topology.build_topology` numbers
them, so a face index here is the index in a feature's `source_faces`.

Deterministic: fixed UV grids, no randomness.
"""

from __future__ import annotations

import math

# Hits closer than this to the (already lifted) origin are noise.
RAY_START_MM = 1e-6
# Grid points per face edge. 3 x 3 is nine samples per face.
FACE_GRID = 3
FINE_FACE_GRID = 7
# Each face's bounding box is grown by this before the ray filter in
# `blocked_along`, so a ray grazing a box's edge is still handed to the exact test.
BOX_MARGIN_MM = 1e-3


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(c * c for c in vector))
    if length == 0:
        return vector
    return tuple(c / length for c in vector)  # type: ignore[return-value]


class ShapeRays:
    """The already-read shape, indexed the way recognition indexed it."""

    def __init__(self, shape):
        from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
        from OCP.TopAbs import TopAbs_ShapeEnum
        from OCP.TopExp import TopExp
        from OCP.TopTools import (
            TopTools_IndexedDataMapOfShapeListOfShape,
            TopTools_IndexedMapOfShape,
        )

        self.shape = shape
        self.faces = TopTools_IndexedMapOfShape()
        # The same deterministic numbering `topology.build_topology` uses, so a
        # feature's `source_faces` index straight into this map.
        TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, self.faces)
        self.edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(
            shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE, self.edge_faces
        )
        self.intersector = IntCurvesFace_ShapeIntersector()
        self.intersector.Load(shape, 1e-7)
        self._surface_cache: dict[int, dict] = {}

    def face(self, index: int):
        from OCP.TopoDS import TopoDS

        if index < 1 or index > self.faces.Extent():
            return None
        return TopoDS.Face_s(self.faces.FindKey(index))

    def describe(self, index: int) -> dict:
        """Surface type and the parameters an approach rule needs, for one face."""
        if index in self._surface_cache:
            return self._surface_cache[index]
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepTools import BRepTools
        from OCP.GeomAbs import GeomAbs_SurfaceType
        from OCP.TopAbs import TopAbs_Orientation

        info: dict = {"type": "other"}
        face = self.face(index)
        if face is not None:
            surface = BRepAdaptor_Surface(face)
            kind = surface.GetType()
            reversed_face = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
            if kind == GeomAbs_SurfaceType.GeomAbs_Plane:
                plane = surface.Plane()
                d = plane.Axis().Direction()
                normal = (d.X(), d.Y(), d.Z())
                if reversed_face:
                    normal = (-normal[0], -normal[1], -normal[2])
                loc = plane.Location()
                info = {"type": "plane", "normal": normal, "origin": (loc.X(), loc.Y(), loc.Z())}
            elif kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
                cylinder = surface.Cylinder()
                d = cylinder.Axis().Direction()
                loc = cylinder.Axis().Location()
                _, _, v_min, v_max = BRepTools.UVBounds_s(face)
                info = {
                    "type": "cylinder",
                    "axis": (d.X(), d.Y(), d.Z()),
                    "location": (loc.X(), loc.Y(), loc.Z()),
                    "radius": float(cylinder.Radius()),
                    "v": (float(v_min), float(v_max)),
                }
            elif kind == GeomAbs_SurfaceType.GeomAbs_Cone:
                info = {"type": "cone"}
        self._surface_cache[index] = info
        return info

    def face_centre(self, index: int):
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        box = Bnd_Box()
        BRepBndLib.Add_s(self.face(index), box)
        x0, y0, z0, x1, y1, z1 = box.Get()
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0)

    def samples(
        self, index: int, grids: tuple[int, ...] = (FACE_GRID, FINE_FACE_GRID)
    ) -> list[tuple[tuple, tuple]]:
        """Points on a face with the outward normal there, from a fixed UV grid.

        Only grid points the face's own boundary classifies as inside are kept,
        so a face with a hole in it is not sampled in the hole. A face too thin
        for the coarse grid is tried on a finer one before giving up. `grids`
        is the sequence of grid sides tried; the default is the approach check's.
        """
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepLProp import BRepLProp_SLProps
        from OCP.BRepTools import BRepTools
        from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
        from OCP.gp import gp_Pnt2d
        from OCP.TopAbs import TopAbs_Orientation, TopAbs_State

        face = self.face(index)
        if face is None:
            return []
        surface = BRepAdaptor_Surface(face)
        reversed_face = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        classifier = BRepTopAdaptor_FClass2d(face, 1e-7)

        for grid in grids:
            found: list[tuple[tuple, tuple]] = []
            for i in range(grid):
                for j in range(grid):
                    u = u0 + (u1 - u0) * (i + 0.5) / grid
                    v = v0 + (v1 - v0) * (j + 0.5) / grid
                    if classifier.Perform(gp_Pnt2d(u, v)) != TopAbs_State.TopAbs_IN:
                        continue
                    props = BRepLProp_SLProps(surface, u, v, 1, 1e-7)
                    if not props.IsNormalDefined():
                        continue
                    point = props.Value()
                    n = props.Normal()
                    normal = (n.X(), n.Y(), n.Z())
                    if reversed_face:
                        normal = (-normal[0], -normal[1], -normal[2])
                    found.append(((point.X(), point.Y(), point.Z()), _unit(normal)))
            if found:
                return found
        return []

    def planar_neighbour_normals(self, face_indices: list[int]) -> list[tuple[int, tuple]]:
        """(face index, outward normal) of each plane sharing an edge with these faces."""
        from OCP.TopAbs import TopAbs_ShapeEnum
        from OCP.TopExp import TopExp_Explorer

        own = set(face_indices)
        found: dict[int, tuple] = {}
        for index in sorted(own):
            face = self.face(index)
            if face is None:
                continue
            explorer = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
            while explorer.More():
                edge = explorer.Current()
                if self.edge_faces.Contains(edge):
                    for neighbour in self.edge_faces.FindFromKey(edge):
                        other = self.faces.FindIndex(neighbour)
                        if other <= 0 or other in own or other in found:
                            continue
                        info = self.describe(other)
                        if info["type"] == "plane":
                            found[other] = _unit(info["normal"])
                explorer.Next()
        return sorted(found.items())

    def first_hit(self, origin, direction) -> tuple[bool, float | None]:
        """(evaluated, distance to the nearest material along the ray or None)."""
        from OCP.gp import gp_Dir, gp_Lin, gp_Pnt

        self.intersector.Perform(
            gp_Lin(gp_Pnt(*origin), gp_Dir(*direction)), RAY_START_MM, 1e100
        )
        if not self.intersector.IsDone():
            return False, None
        hits = [
            self.intersector.WParameter(i)
            for i in range(1, self.intersector.NbPnt() + 1)
            if self.intersector.WParameter(i) > RAY_START_MM
        ]
        return True, (min(hits) if hits else None)

    def blocked_along(self, origins: list[tuple], direction) -> list[tuple[bool, bool]]:
        """For many rays along one direction: (evaluated, met material) per origin.

        The same question as `first_hit(...)[1] is not None`, answered for a
        batch. Each face is intersected exactly (the same intersector and
        tolerance the whole-shape intersector uses per face), but only for a
        ray that passes through that face's bounding box, grown by
        `BOX_MARGIN_MM`. A box test can only admit too many faces, never drop
        one the ray meets, so the answer is unchanged; it stops as soon as one
        face is met, because only *whether* material is met is asked. Faces are
        tried in the order the ray enters their boxes. A test holds it equal to
        `first_hit` on every fixture.
        """
        import numpy as np
        from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
        from OCP.IntCurvesFace import IntCurvesFace_Intersector

        if not origins:
            return []
        low, high = self._boxes()
        start = np.array(origins, dtype=float)
        step = np.array(direction, dtype=float)
        safe = np.where(step == 0.0, 1e-300, step)[None, None, :]
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            t1 = (low[None, :, :] - start[:, None, :]) / safe
            t2 = (high[None, :, :] - start[:, None, :]) / safe
        near = np.minimum(t1, t2).max(axis=2)
        far = np.maximum(t1, t2).min(axis=2)
        passes = far >= np.maximum(near, 0.0)

        answers: list[tuple[bool, bool]] = []
        for row, origin in enumerate(origins):
            slots = np.nonzero(passes[row])[0].tolist()
            slots.sort(key=lambda slot: (float(near[row][slot]), slot))
            line = gp_Lin(gp_Pnt(*origin), gp_Dir(*direction))
            evaluated, met = True, False
            for slot in slots:
                crossing = self._face_intersectors.get(slot)
                if crossing is None:
                    crossing = IntCurvesFace_Intersector(self.face(slot + 1), 1e-7)
                    self._face_intersectors[slot] = crossing
                crossing.Perform(line, RAY_START_MM, 1e100)
                if not crossing.IsDone():
                    evaluated = False
                    break
                if any(crossing.WParameter(k) > RAY_START_MM for k in range(1, crossing.NbPnt() + 1)):
                    met = True
                    break
            answers.append((evaluated, met))
        return answers

    def _boxes(self):
        """Every face's bounding box grown by `BOX_MARGIN_MM`, indexed from 0."""
        if getattr(self, "_box_cache", None) is None:
            import numpy as np
            from OCP.Bnd import Bnd_Box
            from OCP.BRepBndLib import BRepBndLib

            count = self.faces.Extent()
            low = np.empty((count, 3))
            high = np.empty((count, 3))
            for slot in range(count):
                box = Bnd_Box()
                BRepBndLib.Add_s(self.face(slot + 1), box)
                box.Enlarge(BOX_MARGIN_MM)
                x0, y0, z0, x1, y1, z1 = box.Get()
                low[slot] = (x0, y0, z0)
                high[slot] = (x1, y1, z1)
            self._box_cache = (low, high)
            self._face_intersectors: dict[int, object] = {}
        return self._box_cache


__all__ = ["BOX_MARGIN_MM", "FACE_GRID", "FINE_FACE_GRID", "RAY_START_MM", "ShapeRays"]
