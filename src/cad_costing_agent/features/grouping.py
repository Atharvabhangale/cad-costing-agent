"""Grouping coaxial cylindrical faces into single physical cylinders.

Why grouping is needed
----------------------
One bore is not always one face. OpenCASCADE keeps a full cylinder as a single
face with a 2π parameter range, but plenty of CAD systems export a bore as two
half-cylinder faces split along a seam, and a stepped bore is several coaxial
cylinders of different radii. A recognizer that looked at faces individually
would report two half-holes, or would miss that a bore continues past a
diameter change.

So cylindrical faces are first grouped by **the line they revolve about, their
radius, and their overlap along that line**. A group whose angular extents add
up to a full turn is a complete cylinder; anything less is a partial sweep,
which is the signature of a blend rather than a bore.

Axial bookkeeping
-----------------
Each face reports its extent as parameters measured from *its own* axis
location, and two faces of the same bore can have different axis locations. So
every face's start and end are converted to a common parameter measured along
one canonical line, which is what makes overlap comparable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from cad_costing_agent.features.topology import (
    FULL_SWEEP_TOLERANCE_RAD,
    LINEAR_TOLERANCE_MM,
    FaceDescriptor,
    Vec3,
    canonical_direction,
    is_parallel,
    point_on_axis_nearest_origin,
)

_TWO_PI = 2.0 * math.pi

# Faces of one bore may be separated by a hairline gap in parameter space; a
# gap larger than this means two distinct cylinders on the same axis.
AXIAL_GAP_TOLERANCE_MM = 1e-3


@dataclass
class CylinderGroup:
    """One physical cylindrical surface, however many faces represent it."""

    faces: list[FaceDescriptor]
    radius: float
    axis_direction: Vec3
    axis_origin: Vec3  # canonical point on the axis line
    axial_start: float  # parameter along axis_direction from axis_origin
    axial_end: float
    is_internal: bool
    solid_index: int
    total_sweep: float

    @property
    def face_indices(self) -> list[int]:
        return sorted(face.index for face in self.faces)

    @property
    def edge_indices(self) -> list[int]:
        return sorted({index for face in self.faces for index in face.edge_indices})

    @property
    def length(self) -> float:
        return self.axial_end - self.axial_start

    @property
    def diameter(self) -> float:
        return 2.0 * self.radius

    @property
    def is_complete(self) -> bool:
        """The faces together sweep a full turn, so this is a closed cylinder."""
        return abs(self.total_sweep - _TWO_PI) < FULL_SWEEP_TOLERANCE_RAD

    def point_at(self, parameter: float) -> Vec3:
        return self.axis_origin + self.axis_direction.scaled(parameter)

    @property
    def start_point(self) -> Vec3:
        return self.point_at(self.axial_start)

    @property
    def end_point(self) -> Vec3:
        return self.point_at(self.axial_end)

    def parameter_of(self, point: Vec3) -> float:
        """Where a point projects onto this group's axis."""
        return (point - self.axis_origin).dot(self.axis_direction)

    def is_coaxial_with(self, other: "CylinderGroup") -> bool:
        if not is_parallel(self.axis_direction, other.axis_direction):
            return False
        return (self.axis_origin - other.axis_origin).length() < LINEAR_TOLERANCE_MM


def _axis_key(face: FaceDescriptor) -> tuple[Vec3, Vec3]:
    direction = canonical_direction(face.axis_direction)
    origin = point_on_axis_nearest_origin(face.axis_location, direction)
    return direction, origin


def _axial_span(face: FaceDescriptor, direction: Vec3, origin: Vec3) -> tuple[float, float]:
    """The face's extent expressed along the canonical axis."""
    own = face.axis_direction.normalized()
    start_point = face.axis_location + own.scaled(face.axial_start)
    end_point = face.axis_location + own.scaled(face.axial_end)
    first = (start_point - origin).dot(direction)
    second = (end_point - origin).dot(direction)
    return (first, second) if first <= second else (second, first)


def group_cylindrical_faces(faces: list[FaceDescriptor]) -> list[CylinderGroup]:
    """Group cylindrical faces into physical cylinders.

    Faces join the same group when they share an axis line, a radius, the same
    material side, the same solid, and overlapping or touching axial extents.
    Ordering of the returned groups is deterministic.
    """
    candidates = [
        face
        for face in faces
        if face.surface_type == "cylinder"
        and face.radius is not None
        and face.axis_direction is not None
        and face.axis_location is not None
        and face.angular_extent is not None
    ]

    # Bucketing by rounded radius keeps this near-linear on large models; the
    # neighbouring buckets are also consulted so a radius sitting on a bucket
    # boundary still finds its partners.
    buckets: dict[int, list[FaceDescriptor]] = {}
    for face in candidates:
        buckets.setdefault(int(round(face.radius / LINEAR_TOLERANCE_MM)), []).append(face)

    groups: list[CylinderGroup] = []
    assigned: set[int] = set()

    for face in sorted(candidates, key=lambda f: f.index):
        if face.index in assigned:
            continue
        direction, origin = _axis_key(face)
        start, end = _axial_span(face, direction, origin)
        members = [face]
        assigned.add(face.index)
        sweep = face.angular_extent or 0.0

        bucket = int(round(face.radius / LINEAR_TOLERANCE_MM))
        nearby = [
            other
            for offset in (-1, 0, 1)
            for other in buckets.get(bucket + offset, [])
            if other.index not in assigned
        ]

        # Repeat until nothing more joins: a third face may only overlap the
        # span after a second one has extended it.
        changed = True
        while changed:
            changed = False
            for other in sorted(nearby, key=lambda f: f.index):
                if other.index in assigned:
                    continue
                if other.solid_index != face.solid_index:
                    continue
                if other.is_internal != face.is_internal:
                    continue
                if abs(other.radius - face.radius) > LINEAR_TOLERANCE_MM:
                    continue
                other_direction, other_origin = _axis_key(other)
                if not is_parallel(direction, other_direction):
                    continue
                if (origin - other_origin).length() > LINEAR_TOLERANCE_MM:
                    continue
                other_start, other_end = _axial_span(other, direction, origin)
                # Overlapping or touching along the axis.
                if other_start > end + AXIAL_GAP_TOLERANCE_MM or other_end < start - AXIAL_GAP_TOLERANCE_MM:
                    continue
                members.append(other)
                assigned.add(other.index)
                start = min(start, other_start)
                end = max(end, other_end)
                sweep += other.angular_extent or 0.0
                changed = True

        groups.append(
            CylinderGroup(
                faces=sorted(members, key=lambda f: f.index),
                radius=face.radius,
                axis_direction=direction,
                axis_origin=origin,
                axial_start=start,
                axial_end=end,
                is_internal=bool(face.is_internal),
                solid_index=face.solid_index,
                total_sweep=sweep,
            )
        )

    groups.sort(key=lambda g: (g.solid_index, g.face_indices[0]))
    return groups
