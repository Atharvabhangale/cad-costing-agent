"""Small geometric helpers shared by the recognizers.

Everything here works on the scanned descriptors, never on OpenCASCADE objects,
so a recognizer can be tested against a hand-built `TopologyModel` with no CAD
kernel involved.

Measuring in the plane of a face
-------------------------------
An axis-aligned bounding box only reports a face's real length and width when
the face happens to line up with the global axes. A 60 × 30 pocket rotated 30°
about Z would be reported as roughly 67 × 56 — wrong enough to matter. So
extents are measured along a basis derived from the face itself: the longest
straight boundary edge becomes the first axis, and the second is perpendicular
to it within the plane. For prismatic geometry, which is what these recognizers
target, that recovers the true dimensions at any orientation.
"""

from __future__ import annotations

from cad_costing_agent.features.topology import FaceDescriptor, TopologyModel, Vec3


def face_points(face: FaceDescriptor, topology: TopologyModel) -> list[Vec3]:
    """Sample points on a face's boundary: every edge's ends and midpoint.

    Enough to bound straight-edged geometry exactly. A *closed* circular edge
    needs more care: its start and end coincide and its midpoint is diametrically
    opposite, so those three points only span one direction and a circular face
    would be measured as having zero width. Circles therefore contribute their
    four quadrant points, which bound them exactly in both directions.
    """
    points: list[Vec3] = []
    for edge_index in face.edge_indices:
        edge = topology.edges.get(edge_index)
        if edge is None:
            continue
        points.extend((edge.start, edge.end, edge.midpoint))
        if edge.curve_type == "circle" and edge.centre is not None and edge.radius and edge.axis:
            points.extend(_circle_quadrants(edge.centre, edge.radius, edge.axis))
    return points


def _circle_quadrants(centre: Vec3, radius: float, axis: Vec3) -> list[Vec3]:
    """The four points at 90° intervals around a circle."""
    normal = axis.normalized()
    seed = Vec3(1.0, 0.0, 0.0) if abs(normal.x) < 0.9 else Vec3(0.0, 1.0, 0.0)
    first = normal.cross(seed).normalized()
    second = normal.cross(first).normalized()
    return [
        centre + first.scaled(radius),
        centre - first.scaled(radius),
        centre + second.scaled(radius),
        centre - second.scaled(radius),
    ]


def extent_along(points: list[Vec3], direction: Vec3) -> tuple[float, float]:
    """Minimum and maximum projection of the points onto a direction."""
    projections = [point.dot(direction) for point in points]
    return (min(projections), max(projections)) if projections else (0.0, 0.0)


def in_plane_basis(face: FaceDescriptor, topology: TopologyModel) -> tuple[Vec3, Vec3] | None:
    """Two orthonormal in-plane directions, aligned to the face's own edges."""
    if face.normal is None:
        return None
    normal = face.normal.normalized()

    longest: Vec3 | None = None
    longest_length = 0.0
    for edge_index in face.outer_edge_indices or face.edge_indices:
        edge = topology.edges.get(edge_index)
        if edge is None or edge.curve_type != "line" or edge.length <= longest_length:
            continue
        direction = edge.end - edge.start
        if direction.length() > 0:
            longest = direction.normalized()
            longest_length = edge.length

    if longest is None:
        # No straight boundary to align to; any in-plane basis will do.
        seed = Vec3(1.0, 0.0, 0.0) if abs(normal.x) < 0.9 else Vec3(0.0, 1.0, 0.0)
        longest = normal.cross(seed).normalized()

    second = normal.cross(longest).normalized()
    if second.length() == 0:
        return None
    return longest, second


def in_plane_extents(
    face: FaceDescriptor, topology: TopologyModel
) -> tuple[float, float, Vec3] | None:
    """`(length, width, centre)` measured in the face's own plane."""
    basis = in_plane_basis(face, topology)
    if basis is None or face.normal is None or face.origin is None:
        return None
    first, second = basis
    points = face_points(face, topology)
    if len(points) < 2:
        return None

    first_min, first_max = extent_along(points, first)
    second_min, second_max = extent_along(points, second)
    span_first = first_max - first_min
    span_second = second_max - second_min

    normal = face.normal.normalized()
    centre = (
        first.scaled((first_min + first_max) / 2.0)
        + second.scaled((second_min + second_max) / 2.0)
        + normal.scaled(face.origin.dot(normal))
    )
    return max(span_first, span_second), min(span_first, span_second), centre


def outward_normal_at(face: FaceDescriptor, point: Vec3) -> Vec3 | None:
    """The face's outward-from-material normal at a point on it.

    Constant for a plane. For a cylinder it is the radial direction at that
    point, flipped when the material lies outside the surface.
    """
    if face.surface_type == "plane":
        return face.normal
    if face.surface_type == "cylinder" and face.axis_direction is not None and face.axis_location is not None:
        axis = face.axis_direction.normalized()
        offset = point - face.axis_location
        radial = offset - axis.scaled(offset.dot(axis))
        if radial.length() == 0:
            return None
        radial = radial.normalized()
        return radial.scaled(-1.0) if face.is_internal else radial
    return None
