"""Deterministic mesh decimation for the 3D preview. Display only.

Nothing in this module may ever touch an engineering number. It exists so a
model that is too dense to send to a browser can still be *looked at*, and it
achieves that by throwing detail away — which is exactly what a volume, a
surface area, a mass or a feature must never be derived from.

    full-resolution mesh ──→ geometry / mass / costing   (never simplified)
                        └──→ simplify_for_display ──→ 3D viewer

The costing path does not import this module and never sees its output. The
caller (`cad_mesh`) applies it as the last step before packaging the wire
representation, after every measurement has already been taken from the full
mesh.

Why vertex clustering
---------------------
The algorithm is Rossignac & Borrel vertex clustering: overlay a uniform grid
on the model's bounding box, collapse every vertex in a cell to that cell's
centroid, then drop the triangles that collapsed to a line or a point. It was
chosen over the alternatives for four reasons, in this order:

- **Deterministic.** No randomness, no sampling, no iteration order that
  depends on a hash seed. The same bytes give the same display mesh every
  time, which matters because a quotation screenshot should be reproducible.
- **Linear.** One pass per grid resolution over the vertices and one over the
  triangles. Quadric edge collapse, the usual higher-quality choice, needs a
  priority queue over every edge and either a third-party extension or an
  O(n log n) Python loop over hundreds of thousands of edges.
- **Bounded.** Peak memory is a couple of int64 arrays the size of the mesh,
  regardless of how aggressive the reduction is.
- **Shape-preserving where it counts.** Because the grid is anchored to the
  bounding box, silhouettes, holes and overall proportions survive; what is
  lost is small detail below the cell size. Corners can be pulled inwards by
  up to half a cell. For a preview that is acceptable. For a measurement it
  would not be, which is why this is never used for one.

Hitting the budget
------------------
Triangle count grows roughly with the square of the grid resolution but not
predictably enough to solve for, so the resolution is found by bracketing and
bisection: at most `MAX_EVALUATIONS` clustering passes, each O(n), keeping the
finest grid whose result fits the budget. The search never returns a mesh over
budget — if no resolution fits, it returns an empty mesh and the caller reports
the preview as unavailable rather than sending something oversized.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

METHOD = "deterministic_vertex_clustering"
"""Recorded on the response so the viewer can say how the mesh was reduced."""

MAX_DIVISIONS = 4096
"""Finest grid the search will try, along the model's longest axis. Beyond this
the grid stops reducing anything and only costs time."""

MAX_EVALUATIONS = 18
"""Clustering passes the search may spend. Bracketing takes a few and bisection
converges in log2(MAX_DIVISIONS) more, so this is not normally reached."""


@dataclass(frozen=True)
class SimplifiedMesh:
    """A display mesh, plus an honest record of what was done to it."""

    vertices: np.ndarray
    faces: np.ndarray
    simplified: bool
    original_triangle_count: int
    method: str | None = None
    grid_divisions: int | None = None

    @property
    def triangle_count(self) -> int:
        return int(self.faces.shape[0])

    @property
    def ratio(self) -> float | None:
        """Display triangles as a fraction of the original, or None if untouched."""
        if not self.simplified or self.original_triangle_count == 0:
            return None
        return self.triangle_count / self.original_triangle_count


def _cluster(
    vertices: np.ndarray, faces: np.ndarray, divisions: int
) -> tuple[np.ndarray, np.ndarray]:
    """Collapse vertices onto a `divisions`-per-longest-axis grid.

    Cells are cubic — the division count applies to the longest axis and the
    other two get however many cells of that size they need — so a long thin
    part is not squashed along its short axes.
    """
    minimum = vertices.min(axis=0)
    extent = vertices.max(axis=0) - minimum
    longest = float(extent.max())
    if not math.isfinite(longest) or longest <= 0:
        return vertices, faces

    cell = longest / divisions
    counts = np.maximum(np.ceil(extent / cell), 1).astype(np.int64)
    located = np.clip(np.floor((vertices - minimum) / cell).astype(np.int64), 0, counts - 1)
    code = located[:, 0] + counts[0] * (located[:, 1] + counts[1] * located[:, 2])

    _, inverse = np.unique(code, return_inverse=True)
    inverse = inverse.reshape(-1)
    cluster_count = int(inverse.max()) + 1

    # The representative is the cell's centroid: deterministic, and it keeps a
    # flat face flat instead of snapping it to a grid plane.
    weight = np.bincount(inverse, minlength=cluster_count).astype(np.float64)
    representative = np.empty((cluster_count, 3), dtype=np.float64)
    for axis in range(3):
        representative[:, axis] = (
            np.bincount(inverse, weights=vertices[:, axis], minlength=cluster_count) / weight
        )

    remapped = inverse[faces]

    # A triangle whose corners landed in fewer than three cells has collapsed to
    # an edge or a point and would render as nothing.
    keep = (
        (remapped[:, 0] != remapped[:, 1])
        & (remapped[:, 1] != remapped[:, 2])
        & (remapped[:, 0] != remapped[:, 2])
    )
    remapped = remapped[keep]
    if remapped.size == 0:
        return representative[:0], np.zeros((0, 3), dtype=np.int64)

    # Distinct triangles regularly collapse onto the same three cells. Keeping
    # one of each avoids drawing coincident faces that z-fight.
    _, first = np.unique(np.sort(remapped, axis=1), axis=0, return_index=True)
    remapped = remapped[np.sort(first)]

    # Drop clusters no surviving triangle refers to, so the payload carries only
    # vertices that are actually drawn.
    used, compacted = np.unique(remapped, return_inverse=True)
    return representative[used], compacted.reshape(-1, 3).astype(np.int64)


def simplify_for_display(
    vertices: np.ndarray, faces: np.ndarray, max_triangles: int
) -> SimplifiedMesh:
    """Reduce a mesh to at most `max_triangles`, for drawing only.

    A mesh already within budget is returned untouched, with `simplified`
    False — the common case, and it must stay free.

    The result is never over budget. If no grid resolution produces a usable
    mesh within it, the faces come back empty and the caller decides what to
    tell the user; nothing oversized is ever returned.
    """
    vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    original = int(faces.shape[0])

    if original <= max_triangles or original == 0:
        return SimplifiedMesh(vertices, faces, simplified=False, original_triangle_count=original)

    best: tuple[int, np.ndarray, np.ndarray, int] | None = None
    evaluations = 0

    def evaluate(divisions: int) -> int:
        nonlocal best, evaluations
        evaluations += 1
        clustered_vertices, clustered_faces = _cluster(vertices, faces, divisions)
        count = int(clustered_faces.shape[0])
        if count <= max_triangles and (best is None or count > best[0]):
            best = (count, clustered_vertices, clustered_faces, divisions)
        return count

    # A closed surface's triangle count grows about with the square of the grid
    # resolution, so this lands close and the bracketing below is short.
    guess = min(max(int(math.sqrt(max_triangles)), 1), MAX_DIVISIONS)
    low, high = 1, MAX_DIVISIONS + 1

    if evaluate(guess) <= max_triangles:
        low = guess
        divisions = guess
        while divisions < MAX_DIVISIONS and evaluations < MAX_EVALUATIONS:
            divisions = min(divisions * 2, MAX_DIVISIONS)
            if evaluate(divisions) > max_triangles:
                high = divisions
                break
            low = divisions
    else:
        high = guess
        divisions = guess
        while divisions > 1 and evaluations < MAX_EVALUATIONS:
            divisions = max(divisions // 2, 1)
            if evaluate(divisions) <= max_triangles:
                low = divisions
                break
            high = divisions

    # Bisect for the finest grid that still fits, keeping the best seen.
    while high - low > 1 and evaluations < MAX_EVALUATIONS:
        middle = (low + high) // 2
        if evaluate(middle) <= max_triangles:
            low = middle
        else:
            high = middle

    if best is None:
        return SimplifiedMesh(
            vertices[:0],
            np.zeros((0, 3), dtype=np.int64),
            simplified=True,
            original_triangle_count=original,
            method=METHOD,
        )

    _, clustered_vertices, clustered_faces, divisions = best
    return SimplifiedMesh(
        clustered_vertices,
        clustered_faces,
        simplified=True,
        original_triangle_count=original,
        method=METHOD,
        grid_divisions=divisions,
    )
