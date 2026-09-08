"""Reconstructing features from polygons, when the file has no exact surfaces.

A faceted export has thrown away the thing the analytic recognizers rely on. A
hole is no longer a cylinder with a radius written in the file; it is a ring of
flat strips, and its diameter has to be recovered by fitting. So the rules here
are different from the B-Rep ones, and so is what they can honestly claim.

What is reconstructed
---------------------
1. **Planar regions** — adjacent facets that lie in the same plane are merged
   back into one face. This is exact: coplanarity is a property of the
   polygons themselves, not an estimate.
2. **Cylindrical bands** — a connected run of facets whose normals are all
   perpendicular to one direction is a tessellated cylinder. The axis is the
   direction that all those normals are perpendicular to, and the radius comes
   from fitting a circle to the vertices.
3. **Holes and shafts** — a band that closes a full turn is a cylinder; which
   side the material is on makes it a bore or a shaft, and what caps its ends
   makes it through or blind.

Why the fitted radius is the real radius
----------------------------------------
A tessellator places its vertices *on* the true surface and joins them with
chords, so the polygons sit slightly inside the real cylinder but the vertices
do not. Fitting a circle to the vertices therefore recovers the true radius,
not the inscribed-polygon radius. The residual of that fit is also the quality
measure: vertices that genuinely came off a cylinder lie on the fitted circle
to within rounding, and anything that does not is reported at lower confidence
or not at all.

What is not attempted
---------------------
Pockets, slots, fillets and chamfers are not reconstructed from facets. On a
faceted model a fillet is a run of narrow strips that is very hard to tell from
a chamfered edge or from a coarsely tessellated curve, and a pocket floor is
indistinguishable from any other planar region without the wire structure that
a B-Rep has and a facet soup does not. Rather than emit plausible guesses, the
faceted path reports what it can defend and leaves the rest unclassified.
"""

from __future__ import annotations

import math

import numpy as np

from cad_costing_agent.cad_faceted import FacetShell
from cad_costing_agent.features.model import Confidence, Feature, FeatureType, Point3D

# Two facet normals count as the same direction when their dot product is
# within this of 1. Facets written by a tessellator agree to full double
# precision, so this is generous.
NORMAL_TOLERANCE = 1e-6

# A facet is in the same plane as its neighbour when its centroid is no
# further than this fraction of the model's size off that neighbour's plane.
RELATIVE_PLANE_TOLERANCE = 1e-9

# How closely fitted vertices must sit on the circle for a band to be reported
# as a cylinder at all, and to be reported at high confidence.
CIRCLE_FIT_ACCEPT = 0.02  # 2% of the radius
CIRCLE_FIT_HIGH = 0.002  # 0.2% of the radius

# A band closes into a full cylinder when no gap between consecutive vertex
# angles is more than this many times the typical spacing. Comparing against
# the typical spacing rather than against a fixed fraction of a turn is what
# makes the test independent of how finely the circle was tessellated.
OPEN_ARC_GAP_RATIO = 3.0

# Two flat patches belong to the same curved surface when they meet at no more
# than about 40°. Strips of a tessellated bore meet at 360/N degrees; a bore
# meets the face it breaks through at 90°.
SMOOTH_JOIN_COSINE = math.cos(math.radians(40.0))

# A patch count below this cannot describe a curve.
MIN_BAND_PATCHES = 6

# The deciding test between "a circle that was tessellated" and "a polygon that
# was designed". A chord's plane sits a sagitta short of the true radius:
# 1 - cos(pi/N). At 6% this admits a circle cut into ten or more segments
# (4.9%) and rejects an octagon (7.6%) and a hexagon (13.4%), so a hexagonal
# hole is never reported as a round one.
MAX_SAGITTA_RATIO = 0.06


class _Union:
    """Union-find over facets, for merging coplanar and connected runs."""

    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first: int, second: int) -> None:
        a, b = self.find(first), self.find(second)
        if a != b:
            self.parent[max(a, b)] = min(a, b)

    def groups(self) -> dict[int, list[int]]:
        found: dict[int, list[int]] = {}
        for item in range(len(self.parent)):
            found.setdefault(self.find(item), []).append(item)
        return found


def _geometry(shell: FacetShell):
    """Per-facet normal, area and centroid."""
    corners = shell.vertices[shell.triangles]
    edge1 = corners[:, 1] - corners[:, 0]
    edge2 = corners[:, 2] - corners[:, 0]
    cross = np.cross(edge1, edge2)
    lengths = np.linalg.norm(cross, axis=1)
    areas = 0.5 * lengths
    safe = np.where(lengths > 0, lengths, 1.0)
    normals = cross / safe[:, None]
    centroids = corners.mean(axis=1)
    return normals, areas, centroids


def _adjacency(shell: FacetShell) -> dict[tuple[int, int], list[int]]:
    """Edge (as a sorted vertex pair) -> the facets that share it."""
    shared: dict[tuple[int, int], list[int]] = {}
    for index, triangle in enumerate(shell.triangles):
        a, b, c = int(triangle[0]), int(triangle[1]), int(triangle[2])
        for first, second in ((a, b), (b, c), (c, a)):
            shared.setdefault((min(first, second), max(first, second)), []).append(index)
    return shared


def _fit_circle(points: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Least-squares circle through 2-D points.

    Closed form, so it is deterministic and has no iteration to converge.
    Returns the centre, the radius, and the largest radial deviation.
    """
    x = points[:, 0]
    y = points[:, 1]
    design = np.column_stack([x, y, np.ones(len(points))])
    target = x * x + y * y
    solution, *_ = np.linalg.lstsq(design, target, rcond=None)
    centre = np.array([solution[0] / 2.0, solution[1] / 2.0])
    radius_squared = solution[2] + centre[0] ** 2 + centre[1] ** 2
    if radius_squared <= 0:
        return centre, 0.0, math.inf
    radius = math.sqrt(radius_squared)
    distances = np.linalg.norm(points - centre, axis=1)
    return centre, radius, float(np.max(np.abs(distances - radius)))


def _basis(axis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two directions perpendicular to `axis`, forming a right-handed frame."""
    seed = np.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    first = np.cross(axis, seed)
    first /= np.linalg.norm(first)
    second = np.cross(axis, first)
    return first, second


def _canonical(direction: np.ndarray) -> np.ndarray:
    """One of the two directions along an axis, so ±a compare equal."""
    for component in (direction[2], direction[1], direction[0]):
        if abs(component) > 1e-9:
            return direction if component > 0 else -direction
    return direction


class FacetedShellAnalysis:
    """Everything reconstructed from one shell's polygons.

    Two passes, because a tessellated cylinder is *made of* flat pieces and the
    two must not be confused. First adjacent coplanar facets are merged into
    **patches** — on a tessellated cylinder each strip is a patch of two
    triangles, and a real flat face is one large patch. Then patches are joined
    into **bands** only across edges where the surface turns gently: strips of a
    tessellated bore meet at a few degrees, while a bore meets the face it
    breaks through at ninety. That single angle is what separates a curved
    surface from a corner.
    """

    def __init__(self, shell: FacetShell, solid_index: int):
        self.shell = shell
        self.solid_index = solid_index
        self.normals, self.areas, self.centroids = _geometry(shell)
        self.adjacency = _adjacency(shell)
        self.scale = float(np.linalg.norm(shell.vertices.max(axis=0) - shell.vertices.min(axis=0)))
        self.plane_tolerance = max(self.scale * RELATIVE_PLANE_TOLERANCE, 1e-9)
        self.patches: list[list[int]] = []
        self.patch_normals: np.ndarray = np.empty((0, 3))
        self.band_groups: dict[int, list[int]] = {}

    def separate(self) -> None:
        count = len(self.shell.triangles)

        # --- coplanar facets -> patches ---------------------------------
        planar = _Union(count)
        for facets in self.adjacency.values():
            if len(facets) != 2:
                continue
            first, second = facets
            if abs(float(np.dot(self.normals[first], self.normals[second]))) < 1.0 - NORMAL_TOLERANCE:
                continue
            offset = float(
                np.dot(self.centroids[second] - self.centroids[first], self.normals[first])
            )
            if abs(offset) <= self.plane_tolerance:
                planar.union(first, second)

        groups = planar.groups()
        self.patches = [sorted(members) for _, members in sorted(groups.items())]
        patch_of = {facet: index for index, members in enumerate(self.patches) for facet in members}
        self.patch_normals = np.asarray(
            [self.normals[members[0]] for members in self.patches], dtype=np.float64
        )

        # --- patches -> smoothly connected bands -------------------------
        bands = _Union(len(self.patches))
        for facets in self.adjacency.values():
            if len(facets) != 2:
                continue
            first, second = patch_of[facets[0]], patch_of[facets[1]]
            if first == second:
                continue
            cosine = abs(float(np.dot(self.patch_normals[first], self.patch_normals[second])))
            if cosine >= SMOOTH_JOIN_COSINE:
                bands.union(first, second)

        self.band_groups = {
            key: members for key, members in bands.groups().items() if len(members) > 1
        }


def _planar_features(analysis: FacetedShellAnalysis, claimed: set[int]) -> list[Feature]:
    features: list[Feature] = []
    shell = analysis.shell
    for patch_index, members in enumerate(analysis.patches):
        if patch_index in claimed:
            continue
        indices = np.asarray(members)
        area = float(analysis.areas[indices].sum())
        normal = analysis.normals[indices[0]]
        vertices = shell.vertices[shell.triangles[indices].reshape(-1)]

        first, second = _basis(normal)
        along_first = vertices @ first
        along_second = vertices @ second
        spans = sorted(
            [float(along_first.max() - along_first.min()), float(along_second.max() - along_second.min())]
        )
        centre = (
            first * float((along_first.max() + along_first.min()) / 2.0)
            + second * float((along_second.max() + along_second.min()) / 2.0)
            + normal * float(np.dot(vertices[0], normal))
        )

        features.append(
            Feature(
                feature_id="",
                feature_type=FeatureType.PLANE,
                subtype=None,
                solid_index=analysis.solid_index,
                source_type="faceted",
                dimensions={
                    "area_mm2": round(area, 6),
                    "length_mm": round(spans[1], 6),
                    "width_mm": round(spans[0], 6),
                },
                location=Point3D.of(*centre.tolist()),
                axis=Point3D.of(*normal.tolist()),
                source_faces=[],
                source_facets=sorted(int(index) for index in indices),
                confidence=Confidence.HIGH,
                detection_method="coplanar_facets_merged_into_one_region",
                evidence=[
                    f"{len(members)} adjacent facets share one plane to within "
                    f"{analysis.plane_tolerance:.2g} mm, so they are one flat region",
                    f"total area {area:.3f} mm² is the sum of the facet areas, which is exact",
                    "the region's extent is measured in its own plane",
                ],
            )
        )
    return features


def _cylinder_features(analysis: FacetedShellAnalysis) -> tuple[list[Feature], set[int]]:
    features: list[Feature] = []
    claimed_patches: set[int] = set()
    shell = analysis.shell

    for patch_group in analysis.band_groups.values():
        if len(patch_group) < MIN_BAND_PATCHES:
            continue
        members = sorted(facet for patch in patch_group for facet in analysis.patches[patch])
        indices = np.asarray(members)
        # One normal per flat strip, so a finely split strip does not outvote a
        # coarse one when the axis is fitted.
        normals = analysis.patch_normals[np.asarray(sorted(patch_group))]

        # For a tessellated cylinder every strip normal is perpendicular to the
        # axis, so the axis is the direction of least variance of the normals.
        _, eigenvectors = np.linalg.eigh(normals.T @ normals)
        axis = _canonical(eigenvectors[:, 0] / np.linalg.norm(eigenvectors[:, 0]))
        if float(np.max(np.abs(normals @ axis))) > 1e-3:
            continue  # not a surface of revolution about a single axis

        vertex_ids = np.unique(shell.triangles[indices].reshape(-1))
        vertices = shell.vertices[vertex_ids]
        first, second = _basis(axis)
        flattened = np.column_stack([vertices @ first, vertices @ second])
        centre2d, radius, deviation = _fit_circle(flattened)
        if radius <= 0 or not math.isfinite(deviation):
            continue
        relative = deviation / radius
        if relative > CIRCLE_FIT_ACCEPT:
            continue  # the facets do not lie on a cylinder

        origin = first * float(centre2d[0]) + second * float(centre2d[1])

        # A chord's plane stands a sagitta short of the true radius. A finely
        # tessellated circle has a tiny one; a designed polygon has a large one.
        strip_offsets = []
        for patch in sorted(patch_group):
            point = analysis.centroids[analysis.patches[patch][0]]
            radial = point - origin
            radial = radial - axis * float(np.dot(radial, axis))
            strip_offsets.append(abs(float(np.dot(radial, analysis.patch_normals[patch]))))
        sagitta_ratio = 1.0 - (min(strip_offsets) / radius) if strip_offsets else 1.0
        if sagitta_ratio > MAX_SAGITTA_RATIO:
            continue  # a designed polygon, not a tessellated circle

        # Does the band close? Not "is the largest gap small" — on a ring of N
        # vertices the largest gap *is* the normal spacing of 2π/N. What marks
        # an open arc is one gap much larger than the rest, so the largest gap
        # is judged against the typical one.
        angles = np.arctan2(flattened[:, 1] - centre2d[1], flattened[:, 0] - centre2d[0])
        distinct = np.unique(np.round(angles, 6))
        if len(distinct) < MIN_BAND_PATCHES:
            continue
        gaps = np.diff(np.concatenate([distinct, [distinct[0] + 2 * math.pi]]))
        largest = float(np.max(gaps))
        typical = float(np.median(gaps))
        complete = typical > 0 and largest <= OPEN_ARC_GAP_RATIO * typical
        coverage = 1.0 - max(0.0, largest - typical) / (2 * math.pi)

        along = vertices @ axis
        axial_start, axial_end = float(along.min()), float(along.max())
        start_point = origin + axis * axial_start

        # Material side: an outward normal pointing back toward the axis means
        # the material is outside the surface, which is a bore.
        centroids = analysis.centroids[indices]
        radial = centroids - (origin + np.outer(centroids @ axis, axis))
        lengths = np.linalg.norm(radial, axis=1)
        lengths = np.where(lengths > 0, lengths, 1.0)
        outward = float(
            np.mean(np.einsum("ij,ij->i", analysis.normals[indices], radial / lengths[:, None]))
        )
        internal = outward < 0

        if not complete:
            # A partial sweep on a faceted model is as likely to be a coarse
            # curve or a blend as anything else, so it is not claimed.
            continue

        ends, end_evidence = _classify_ends(analysis, indices, axis, origin, radius, axial_start, axial_end)

        if relative <= CIRCLE_FIT_HIGH and ends != "unknown":
            confidence = Confidence.HIGH
        elif relative <= CIRCLE_FIT_HIGH or ends != "unknown":
            confidence = Confidence.MEDIUM
        else:
            confidence = Confidence.LOW

        evidence = [
            f"{len(members)} adjacent facets have normals perpendicular to a common axis "
            f"(largest deviation {float(np.max(np.abs(normals @ axis))):.2e}), so they form a "
            "tessellated surface of revolution",
            f"a circle fitted to their {len(vertex_ids)} vertices gives radius {radius:.4f} mm "
            f"with a largest deviation of {deviation:.2e} mm ({relative:.3%} of the radius)",
            f"the vertex angles are evenly spaced with no gap wider than "
            f"{OPEN_ARC_GAP_RATIO:g}× the typical one, so the band closes into a full turn "
            f"({coverage:.1%} covered)",
            f"the flat strips stand {sagitta_ratio:.2%} of the radius short of it, below the "
            f"{MAX_SAGITTA_RATIO:.0%} that separates a tessellated circle from a designed "
            "polygon, so this is a round hole and not a many-sided one",
            "tessellation places vertices on the true surface, so the fitted radius is the real "
            "radius rather than the inscribed-polygon radius",
            f"facet normals point {'toward' if internal else 'away from'} the axis, so the "
            f"material lies {'outside' if internal else 'inside'} the surface",
            *end_evidence,
        ]

        length = axial_end - axial_start
        if internal:
            subtype = ends if ends != "unknown" else "ambiguous"
            feature_type = FeatureType.HOLE
            dimensions = {"diameter_mm": round(2 * radius, 6), "depth_mm": round(length, 6)}
        else:
            subtype = "shaft"
            feature_type = FeatureType.CYLINDER
            dimensions = {
                "diameter_mm": round(2 * radius, 6),
                "radius_mm": round(radius, 6),
                "length_mm": round(length, 6),
            }

        features.append(
            Feature(
                feature_id="",
                feature_type=feature_type,
                subtype=subtype,
                solid_index=analysis.solid_index,
                source_type="faceted",
                dimensions=dimensions,
                location=Point3D.of(*start_point.tolist()),
                axis=Point3D.of(*axis.tolist()),
                source_faces=[],
                source_facets=sorted(int(index) for index in indices),
                confidence=confidence,
                detection_method="facet_band_fitted_to_a_cylinder",
                evidence=evidence,
            )
        )
        claimed_patches.update(patch_group)
    return features, claimed_patches


def _classify_ends(analysis, indices, axis, origin, radius, axial_start, axial_end):
    """Decide whether a band's ends are open or capped by a flat floor.

    Mirrors the B-Rep rule without wires: the facets meeting the band at an end
    are inspected, and a cap that stays inside the bore radius is a floor,
    while one that spreads beyond it is a wall the bore breaks through.
    """
    shell = analysis.shell
    band = set(int(index) for index in indices)
    states: list[str] = []
    evidence: list[str] = []

    for parameter, label in ((axial_start, "start"), (axial_end, "end")):
        neighbours: set[int] = set()
        for edge, facets in analysis.adjacency.items():
            if len(facets) != 2:
                continue
            inside = [facet for facet in facets if facet in band]
            outside = [facet for facet in facets if facet not in band]
            if len(inside) != 1 or not outside:
                continue
            edge_points = shell.vertices[list(edge)]
            if float(np.max(np.abs(edge_points @ axis - parameter))) > max(radius * 1e-3, 1e-6):
                continue
            neighbours.update(outside)

        if not neighbours:
            states.append("open")
            evidence.append(f"nothing adjoins the {label} of the band, so that end is open")
            continue

        cap = np.asarray(sorted(neighbours))
        cap_normals = analysis.normals[cap]
        if float(np.min(np.abs(cap_normals @ axis))) < 1.0 - 1e-3:
            states.append("unknown")
            evidence.append(
                f"the facets adjoining the {label} of the band are not perpendicular to the "
                "axis, so that end could not be classified"
            )
            continue

        cap_vertices = shell.vertices[shell.triangles[cap].reshape(-1)]
        offsets = cap_vertices - (origin + np.outer(cap_vertices @ axis, axis))
        reach = float(np.max(np.linalg.norm(offsets, axis=1)))
        if reach > radius * 1.001:
            states.append("open")
            evidence.append(
                f"the flat region at the {label} extends {reach:.3f} mm from the axis, beyond the "
                f"{radius:.3f} mm bore, so the bore breaks through it"
            )
        else:
            states.append("closed")
            evidence.append(
                f"the flat region at the {label} stays within the {radius:.3f} mm bore, so it is "
                "a floor"
            )

    if "unknown" in states:
        return "unknown", evidence
    if states.count("open") == 2:
        return "through", evidence
    if states.count("closed") == 1:
        return "blind", evidence
    return "unknown", evidence


def recognize_mesh_features(shells: list[FacetShell]) -> list[Feature]:
    """Reconstruct features from faceted bodies. Deterministic and order-stable."""
    features: list[Feature] = []
    for solid_index, shell in enumerate(shells):
        analysis = FacetedShellAnalysis(shell, solid_index)
        analysis.separate()
        # Cylinders first: a bore's strips are flat, and reporting them as
        # dozens of tiny planar regions instead of one hole would be true and
        # useless.
        cylinders, claimed = _cylinder_features(analysis)
        features.extend(cylinders)
        features.extend(_planar_features(analysis, claimed))
    return features
