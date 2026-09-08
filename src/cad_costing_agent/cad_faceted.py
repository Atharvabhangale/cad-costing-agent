"""Polygon geometry from faceted STEP: parsing, welding, and closure.

Why this sits beside `cad_geometry` rather than inside the feature layer
-----------------------------------------------------------------------
A faceted export has no exact surfaces, but it can still have a perfectly good
*volume* — and volume is what mass and therefore costing need. That makes this
a geometry concern, not a feature-recognition one. Feature recognition and mass
extraction are independent: a faceted model may yield limited features and a
completely valid mass, and that is fine.

Why OpenCASCADE is not used
---------------------------
The exports that motivated this declare `FACETED_BREP` over an `OPEN_SHELL`,
which ISO 10303-42 forbids, so the reader rejects every solid and transfers
nothing at all. Reading the polygons directly avoids the whole problem.

The `OPEN_SHELL` trap
---------------------
**A shell declared `OPEN_SHELL` in the STEP text says nothing about whether the
polygons form a closed surface.** In the real files, the 6,473 "open shells"
turned out to be face *groups*: welded together they form only 100 connected
components, 63 of which are watertight. The declaration and the topology are
separate things, so closure is decided by examining the polygons — never by
reading the entity name.

How closure is decided
----------------------
Vertices are welded by position (a faceted exporter typically writes a separate
`CARTESIAN_POINT` for every corner of every facet, so without welding nothing
is adjacent to anything). The welded triangles are split into connected
components, and a component is closed when **every edge is shared by exactly
two triangles**. Its volume is then the signed volume of its triangles, which
is exact for a closed surface.

Only real topology counts. There is no bounding box, no convex hull, no guessed
thickness, and no repair of open geometry: a component that is not closed
contributes nothing and is reported as excluded.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

# The entity forms a facet model is made of. Matched against bytes so a 200 MB
# file never has to be decoded to text first.
_POINT = re.compile(rb"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)")
_LOOP = re.compile(rb"#(\d+)\s*=\s*POLY_LOOP\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)")
_BOUND = re.compile(rb"#(\d+)\s*=\s*FACE_(?:OUTER_)?BOUND\s*\(\s*'[^']*'\s*,\s*#(\d+)")
_FACE = re.compile(rb"#(\d+)\s*=\s*(?:ADVANCED_FACE|FACE_SURFACE)\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)")
_SHELL = re.compile(rb"#(\d+)\s*=\s*(?:CLOSED_SHELL|OPEN_SHELL)\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)")
_REF = re.compile(rb"#(\d+)")

# Vertices closer together than this are the same vertex. Measured on the real
# files: closure is identical anywhere from 1e-9 to 1e-6 of the file's unit,
# and only degrades above that as genuinely distinct vertices start merging.
# The value is relative to the model's size, so it works in metres or
# millimetres without being told which.
RELATIVE_WELD_TOLERANCE = 1e-9

# STEP length units, as millimetres per unit. A faceted file's coordinates are
# taken verbatim, so getting this wrong is a factor-of-1000 error in mass.
_SI_PREFIXES = {
    b"": 1000.0,  # metre
    b".MILLI.": 1.0,
    b".CENTI.": 10.0,
    b".DECI.": 100.0,
    b".KILO.": 1_000_000.0,
    b".MICRO.": 0.001,
}
_LENGTH_UNIT = re.compile(rb"LENGTH_UNIT\s*\(\s*\)\s*NAMED_UNIT\s*\([^)]*\)\s*SI_UNIT\s*\(([^,]*),\s*\.METRE\.\s*\)")
_INCH = re.compile(rb"CONVERSION_BASED_UNIT\s*\(\s*'(INCH|inch)'", re.IGNORECASE)


@dataclass(frozen=True)
class LengthUnit:
    name: str
    millimetres_per_unit: float


def detect_length_unit(data: bytes) -> LengthUnit | None:
    """The file's declared length unit, or None if it cannot be determined.

    Returning None rather than assuming millimetres is deliberate: guessing
    wrong here scales every mass by a thousand, and a silently wrong quotation
    is worse than a refused one.
    """
    match = _LENGTH_UNIT.search(data)
    if match is not None:
        prefix = match.group(1).strip()
        if prefix in (b"$", b""):
            return LengthUnit("metre", _SI_PREFIXES[b""])
        if prefix in _SI_PREFIXES:
            return LengthUnit(prefix.strip(b".").decode().lower() + "metre", _SI_PREFIXES[prefix])
        return None
    if _INCH.search(data) is not None:
        return LengthUnit("inch", 25.4)
    return None


@dataclass
class FacetShell:
    """One shell's triangles, as vertices plus index triples."""

    index: int
    vertices: np.ndarray
    triangles: np.ndarray

    @property
    def triangle_count(self) -> int:
        return int(self.triangles.shape[0])

    @property
    def vertex_count(self) -> int:
        return int(self.vertices.shape[0])


@dataclass
class FacetedModel:
    """Bodies as the file groups them, which feature recognition works from."""

    shells: list[FacetShell] = field(default_factory=list)
    point_count: int = 0
    facet_count: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def triangle_count(self) -> int:
        return sum(shell.triangle_count for shell in self.shells)


@dataclass
class MeshComponent:
    """One connected piece of the welded mesh, and whether it holds water."""

    index: int
    triangle_count: int
    boundary_edge_count: int
    non_manifold_edge_count: int
    boundary_chain_count: int
    signed_volume: float
    winding_consistent: bool = True

    @property
    def is_closed(self) -> bool:
        """Watertight *and* consistently wound.

        Counting undirected edges alone is not enough: flip one triangle and
        every edge is still shared by exactly two triangles, but the signed
        volume is wrong by twice that triangle's contribution. Consistency is
        therefore checked on *directed* edges — in a correctly wound closed
        surface every directed edge appears exactly once, and so does its
        reverse.
        """
        return (
            self.boundary_edge_count == 0
            and self.non_manifold_edge_count == 0
            and self.winding_consistent
        )

    @property
    def volume(self) -> float:
        return abs(self.signed_volume)


# The one repair this module performs, and the only one the evidence supports.
REPAIR_METHOD = "restore_uniquely_determined_missing_facets"


def restore_missing_facets(triangles: np.ndarray) -> tuple[np.ndarray, int]:
    """Put back facets the exporter left out, where only one facet can fit.

    A triangulated surface with one triangle missing leaves a boundary of
    **exactly three edges**, each used once, forming a closed loop over three
    vertices that are already in the mesh. Exactly one triangle can fill that,
    spanning three existing points, and the three unmatched directed edges fix
    its orientation. Nothing is chosen and no tolerance is involved: this is
    topology, not fitting.

    Anything larger is left alone. A four-edge loop already offers a choice of
    diagonal, and a loop of a hundred edges around a missing face would mean
    inventing a surface — which for a system that produces quotations is worse
    than refusing.

    Returns the triangles (with any restored facets appended) and how many were
    restored.
    """
    if triangles.size == 0:
        return triangles, 0

    first = triangles[:, [0, 1, 2]].reshape(-1).tolist()
    second = triangles[:, [1, 2, 0]].reshape(-1).tolist()
    owner = np.repeat(np.arange(len(triangles)), 3).tolist()
    directed = Counter(zip(first, second))
    undirected = Counter((min(a, b), max(a, b)) for a, b in zip(first, second))

    # Which triangle each edge came from, so a hole can be told from a lone
    # triangle that merely happens to have three free edges.
    carried_by: dict[tuple[int, int], set[int]] = defaultdict(set)
    for a, b, triangle in zip(first, second, owner):
        carried_by[(min(a, b), max(a, b))].add(triangle)

    # Only edges used exactly once can be filled: filling an edge already used
    # twice would create non-manifold topology.
    boundary = [edge for edge, count in undirected.items() if count == 1]
    if not boundary:
        return triangles, 0

    neighbours: dict[int, set[int]] = defaultdict(set)
    for a, b in boundary:
        neighbours[a].add(b)
        neighbours[b].add(a)

    restored: list[tuple[int, int, int]] = []
    seen: set[int] = set()
    for vertex in sorted(neighbours):
        if vertex in seen:
            continue
        stack = [vertex]
        members: list[int] = []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            members.append(current)
            stack.extend(neighbours[current])

        # Exactly three vertices, each joined to the other two: one triangle.
        if len(members) != 3 or any(len(neighbours[m]) != 2 for m in members):
            continue

        # The hole's three edges must come from three different triangles. A
        # lone triangle floating in space also has three free edges forming a
        # closed loop, and "filling" it would fold a second copy back onto it —
        # a shell of zero volume that would then read as watertight.
        edges = [
            (min(a, b), max(a, b))
            for a, b in ((members[0], members[1]), (members[1], members[2]), (members[2], members[0]))
        ]
        if any(edge not in carried_by for edge in edges):
            continue
        if len({triangle for edge in edges for triangle in carried_by[edge]}) < 3:
            continue

        # Orientation comes from the direction each edge is missing.
        oriented: list[tuple[int, int]] = []
        for a, b in ((members[0], members[1]), (members[1], members[2]), (members[2], members[0])):
            if directed.get((a, b), 0) == 0 and directed.get((b, a), 0) == 1:
                oriented.append((a, b))
            elif directed.get((b, a), 0) == 0 and directed.get((a, b), 0) == 1:
                oriented.append((b, a))
            else:
                oriented = []
                break
        if len(oriented) != 3:
            continue

        # The three directed edges must chain head-to-tail into one loop.
        ordered = {start: end for start, end in oriented}
        walk = [oriented[0][0]]
        while len(walk) < 3:
            nxt = ordered.get(walk[-1])
            if nxt is None or nxt in walk:
                break
            walk.append(nxt)
        if len(walk) != 3 or ordered.get(walk[-1]) != walk[0]:
            continue

        restored.append((walk[0], walk[1], walk[2]))

    if not restored:
        return triangles, 0
    return np.vstack([triangles, np.asarray(restored, dtype=np.int64)]), len(restored)


@dataclass
class MeshTopology:
    """The welded mesh's closure, in the file's own length unit."""

    components: list[MeshComponent]
    vertex_count: int
    triangle_count: int
    restored_facet_count: int = 0
    weld_tolerance: float = 0.0
    surface_area: float = 0.0
    bbox_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    bbox_max: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def closed(self) -> list[MeshComponent]:
        return [component for component in self.components if component.is_closed]

    @property
    def open(self) -> list[MeshComponent]:
        return [component for component in self.components if not component.is_closed]

    @property
    def closed_triangle_count(self) -> int:
        return sum(component.triangle_count for component in self.closed)

    @property
    def is_watertight(self) -> bool:
        """Every component closed. Only then is the total volume trustworthy."""
        return bool(self.components) and not self.open

    @property
    def volume(self) -> float:
        return sum(component.volume for component in self.closed)


@dataclass
class _Entities:
    points: dict[int, tuple[float, float, float]]
    loops: dict[int, list[int]]
    bounds: dict[int, int]
    faces: dict[int, list[int]]
    shells: dict[int, list[int]]


def _coordinates(blob: bytes) -> tuple[float, float, float] | None:
    parts = blob.split(b",")
    if len(parts) < 3:
        return None
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return None


def _read_entities(data: bytes, max_facets: int | None) -> _Entities:
    """One pass over the bytes, cheapest structures first.

    Polygons are counted before points are collected, so an oversized model is
    refused before the million-entry dictionary is built rather than after.
    """
    loops = {
        int(match.group(1)): [int(ref) for ref in _REF.findall(match.group(2))]
        for match in _LOOP.finditer(data)
    }
    if max_facets is not None and len(loops) > max_facets:
        raise ValueError(
            f"This faceted model has {len(loops):,} polygons, above the {max_facets:,} that "
            "will be analysed. Nothing was loaded."
        )

    points: dict[int, tuple[float, float, float]] = {}
    for match in _POINT.finditer(data):
        coordinates = _coordinates(match.group(2))
        if coordinates is not None:
            points[int(match.group(1))] = coordinates

    return _Entities(
        points=points,
        loops=loops,
        bounds={int(m.group(1)): int(m.group(2)) for m in _BOUND.finditer(data)},
        faces={
            int(m.group(1)): [int(ref) for ref in _REF.findall(m.group(2))]
            for m in _FACE.finditer(data)
        },
        shells={
            int(m.group(1)): [int(ref) for ref in _REF.findall(m.group(2))]
            for m in _SHELL.finditer(data)
        },
    )


def _polygons_of_face(entities: _Entities, face_id: int) -> list[list[int]]:
    collected: list[list[int]] = []
    for bound_id in entities.faces.get(face_id, ()):
        loop_id = entities.bounds.get(bound_id)
        if loop_id is None and bound_id in entities.loops:
            loop_id = bound_id  # some writers point a face straight at its loop
        if loop_id is not None and loop_id in entities.loops:
            collected.append(entities.loops[loop_id])
    return collected


def _weld_tolerance(points) -> float:
    """Absolute weld tolerance, scaled to the model so units do not matter."""
    if not points:
        return RELATIVE_WELD_TOLERANCE
    array = np.asarray(list(points), dtype=np.float64)
    extent = float(np.max(array.max(axis=0) - array.min(axis=0)))
    return max(extent * RELATIVE_WELD_TOLERANCE, 1e-12)


def _build(polygons, points, tolerance) -> tuple[np.ndarray, np.ndarray, int]:
    """Weld polygon corners by position and fan-triangulate."""
    index: dict[tuple[int, int, int], int] = {}
    coordinates: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    facets = 0

    for polygon in polygons:
        local: list[int] = []
        for point_id in polygon:
            position = points.get(point_id)
            if position is None:
                continue
            key = (
                int(round(position[0] / tolerance)),
                int(round(position[1] / tolerance)),
                int(round(position[2] / tolerance)),
            )
            if key not in index:
                index[key] = len(coordinates)
                coordinates.append(position)
            local.append(index[key])
        if len(local) < 3:
            continue
        facets += 1
        for corner in range(1, len(local) - 1):
            triangles.append((local[0], local[corner], local[corner + 1]))

    return (
        np.asarray(coordinates, dtype=np.float64).reshape(-1, 3),
        np.asarray(triangles, dtype=np.int64).reshape(-1, 3),
        facets,
    )


def parse_faceted_step(data: bytes, *, max_facets: int | None = None) -> FacetedModel:
    """Bodies as the file groups them, for feature recognition."""
    entities = _read_entities(data, max_facets)
    if not entities.loops:
        return FacetedModel(warnings=["No POLY_LOOP entities were found in this file."])

    warnings: list[str] = []
    grouped: list[list[list[int]]] = []
    if entities.shells:
        for shell_id in sorted(entities.shells):
            polygons: list[list[int]] = []
            for face_id in entities.shells[shell_id]:
                polygons.extend(_polygons_of_face(entities, face_id))
            if polygons:
                grouped.append(polygons)
    else:
        warnings.append(
            "The file has no shell entities, so every polygon is reported as one body."
        )
        grouped.append([entities.loops[key] for key in sorted(entities.loops)])

    tolerance = _weld_tolerance(entities.points.values())
    built: list[FacetShell] = []
    total_facets = 0
    for polygons in grouped:
        vertices, triangles, facets = _build(polygons, entities.points, tolerance)
        total_facets += facets
        if triangles.size:
            built.append(FacetShell(index=len(built), vertices=vertices, triangles=triangles))

    if not built:
        warnings.append("No polygon referenced points that could be read, so no body was built.")

    return FacetedModel(
        shells=built,
        point_count=len(entities.points),
        facet_count=total_facets,
        warnings=warnings,
    )


def analyse_faceted_volume(data: bytes, *, max_facets: int | None = None) -> MeshTopology:
    """Weld every polygon in the file together and measure closure and volume.

    Shell grouping is ignored deliberately. A faceted exporter may split one
    solid across thousands of shells, so grouping by shell would report a
    watertight part as thousands of open fragments. Connectivity decides what a
    body is; the entity names do not.
    """
    entities = _read_entities(data, max_facets)
    if not entities.loops:
        return MeshTopology(components=[], vertex_count=0, triangle_count=0)

    tolerance = _weld_tolerance(entities.points.values())
    vertices, triangles, _ = _build(
        [entities.loops[key] for key in sorted(entities.loops)], entities.points, tolerance
    )
    if triangles.size == 0:
        return MeshTopology(components=[], vertex_count=0, triangle_count=0)

    # Put back facets the exporter omitted, before closure is judged. This can
    # only ever reduce the boundary, never extend it, and every restored facet
    # is reported so a repaired volume is never mistaken for a pristine one.
    triangles, restored = restore_missing_facets(triangles)

    # Connected components over shared vertices.
    parent = list(range(len(vertices)))

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for first, second, third in triangles:
        for a, b in ((int(first), int(second)), (int(second), int(third))):
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[max(root_a, root_b)] = min(root_a, root_b)

    grouped: dict[int, list[int]] = defaultdict(list)
    for position, triangle in enumerate(triangles):
        grouped[find(int(triangle[0]))].append(position)

    components: list[MeshComponent] = []
    for index, (_, members) in enumerate(sorted(grouped.items())):
        selected = triangles[members]
        first = selected[:, [0, 1, 2]].reshape(-1).tolist()
        second = selected[:, [1, 2, 0]].reshape(-1).tolist()
        directed = Counter(zip(first, second))
        edges = Counter(
            (min(a, b), max(a, b)) for a, b in zip(first, second)
        )
        boundary = [edge for edge, count in edges.items() if count < 2]
        non_manifold = sum(1 for count in edges.values() if count > 2)
        consistent = all(
            count == 1 and directed.get((b, a), 0) == 1 for (a, b), count in directed.items()
        )

        # How the boundary is shaped says what kind of opening it is: a few
        # long chains is a genuine open sheet, many short ones are cracks.
        neighbours: dict[int, set[int]] = defaultdict(set)
        for a, b in boundary:
            neighbours[a].add(b)
            neighbours[b].add(a)
        seen: set[int] = set()
        chains = 0
        for vertex in neighbours:
            if vertex in seen:
                continue
            chains += 1
            stack = [vertex]
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(neighbours[current])

        corners = vertices[selected]
        signed = float(
            np.einsum("ij,ij->i", corners[:, 0], np.cross(corners[:, 1], corners[:, 2])).sum() / 6.0
        )
        components.append(
            MeshComponent(
                index=index,
                triangle_count=len(members),
                boundary_edge_count=len(boundary),
                non_manifold_edge_count=non_manifold,
                boundary_chain_count=chains,
                signed_volume=signed,
                winding_consistent=consistent,
            )
        )

    corners = vertices[triangles]
    areas = 0.5 * np.linalg.norm(
        np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1
    )
    return MeshTopology(
        components=components,
        vertex_count=int(vertices.shape[0]),
        triangle_count=int(triangles.shape[0]),
        surface_area=float(areas.sum()),
        bbox_min=tuple(float(value) for value in vertices.min(axis=0)),
        bbox_max=tuple(float(value) for value in vertices.max(axis=0)),
        restored_facet_count=restored,
        weld_tolerance=tolerance,
    )
