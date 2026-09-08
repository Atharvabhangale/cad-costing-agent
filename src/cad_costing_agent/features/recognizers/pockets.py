"""Prismatic recesses: pockets and slots.

The rule
--------
A pocket is a planar **floor** completely surrounded by **walls that rise from
it**:

1. Take a planar face and its outward normal `n`.
2. Every face across its outer boundary must be a wall — either a plane
   perpendicular to `n`, or a cylinder whose axis is parallel to `n`, which is
   what a rounded corner or a rounded slot end looks like.
3. Every wall must start at the floor and extend in the `+n` direction. This
   single condition is what separates a pocket floor from the top face of a
   plain block: a block's top face is also surrounded by perpendicular planes,
   but those walls hang *below* it, not above.
4. The depth is how far the walls rise. When they all rise by the same amount
   the recess is a simple prismatic pocket; when they differ, the shallowest
   bounds the recess and the feature is reported at medium confidence because
   the depth had to be chosen rather than measured once.

Pocket or slot
--------------
The two are the same geometry with different proportions, so they share the
evidence and are separated by a documented rule: a recess whose floor is at
least `SLOT_ASPECT_RATIO` times longer than it is wide is reported as a
**slot**, otherwise as a **pocket**. The threshold is a naming convention, not
a measurement, so it is stated in the evidence of every feature it decides.

Known limitation
----------------
A recess with no floor — a window or a slot cut clean through the part — has no
planar face to anchor this rule and is not detected. It is not guessed at
either: a through-slot is left as unclaimed faces rather than reported as
something the geometry does not support.
"""

from __future__ import annotations

from cad_costing_agent.features.model import Confidence, Feature, FeatureType, Point3D
from cad_costing_agent.features.recognizers.base import RecognitionContext
from cad_costing_agent.features.recognizers.geometry import (
    extent_along,
    face_points,
    in_plane_extents,
)
from cad_costing_agent.features.topology import (
    LINEAR_TOLERANCE_MM,
    FaceDescriptor,
    TopologyModel,
    is_parallel,
    is_perpendicular,
)

SLOT_ASPECT_RATIO = 3.0
"""Floor length ÷ width at or above which a recess is named a slot."""

MINIMUM_DEPTH_MM = 1e-3
"""A wall must rise at least this far above the floor for a recess to exist."""

DEPTH_AGREEMENT_MM = 1e-4
"""Wall rises within this of each other count as a single prismatic depth."""


class _Recess:
    def __init__(self, floor: FaceDescriptor):
        self.floor = floor
        self.walls: list[FaceDescriptor] = []
        self.depth = 0.0
        self.uniform_depth = True


def _wall_rise(
    wall: FaceDescriptor, topology: TopologyModel, normal, floor_offset: float
) -> tuple[float, float]:
    """How far a wall extends below and above the floor plane, along `n`."""
    points = face_points(wall, topology)
    if not points:
        return 0.0, 0.0
    low, high = extent_along(points, normal)
    return low - floor_offset, high - floor_offset


def find_prismatic_recesses(context: RecognitionContext) -> list[_Recess]:
    """Every floor-plus-walls recess in the unclaimed geometry."""
    topology = context.topology
    recesses: list[_Recess] = []

    for floor in context.unclaimed_faces():
        if floor.surface_type != "plane" or floor.normal is None or floor.origin is None:
            continue
        if not floor.outer_edge_indices:
            continue

        normal = floor.normal.normalized()
        floor_offset = floor.origin.dot(normal)

        recess = _Recess(floor)
        rises: list[float] = []
        valid = True

        for edge_index in floor.outer_edge_indices:
            neighbours = [
                topology.faces[i]
                for i in (topology.edges[edge_index].face_indices if edge_index in topology.edges else ())
                if i != floor.index and i in topology.faces
            ]
            if not neighbours:
                valid = False
                break

            for wall in neighbours:
                if context.is_claimed(wall.index):
                    valid = False
                    break
                if wall.surface_type == "plane" and wall.normal is not None:
                    if not is_perpendicular(wall.normal, normal):
                        valid = False
                        break
                elif wall.surface_type == "cylinder" and wall.axis_direction is not None:
                    if not is_parallel(wall.axis_direction, normal):
                        valid = False
                        break
                else:
                    valid = False
                    break

                below, above = _wall_rise(wall, topology, normal, floor_offset)
                # The wall must sit on the floor and rise from it, not hang below.
                if above < MINIMUM_DEPTH_MM or below < -LINEAR_TOLERANCE_MM:
                    valid = False
                    break
                if wall not in recess.walls:
                    recess.walls.append(wall)
                    rises.append(above)
            if not valid:
                break

        if not valid or not recess.walls:
            continue

        recess.depth = min(rises)
        recess.uniform_depth = (max(rises) - min(rises)) <= DEPTH_AGREEMENT_MM
        recesses.append(recess)

    recesses.sort(key=lambda r: r.floor.index)
    return recesses


class PocketRecognizer:
    """Prismatic recesses, reported as pockets or slots by their proportions."""

    name = "pocket"

    def recognize(self, context: RecognitionContext) -> list[Feature]:
        features: list[Feature] = []
        for recess in find_prismatic_recesses(context):
            floor = recess.floor
            extents = in_plane_extents(floor, context.topology)
            if extents is None:
                continue
            length, width, centre = extents

            wall_indices = sorted(wall.index for wall in recess.walls)
            aspect = length / width if width > LINEAR_TOLERANCE_MM else float("inf")
            is_slot = aspect >= SLOT_ASPECT_RATIO
            rounded = [wall.index for wall in recess.walls if wall.surface_type == "cylinder"]

            evidence = [
                f"face {floor.index} is a planar floor of area {floor.area:.3f} mm²",
                f"its outer boundary is closed by {len(recess.walls)} wall face(s) "
                f"{wall_indices}, each perpendicular to the floor",
                f"every wall rises {recess.depth:.3f} mm above the floor in the direction of its "
                "outward normal, so the floor bounds a recess rather than an outer surface",
                f"floor measures {length:.3f} × {width:.3f} mm in its own plane, an aspect ratio "
                f"of {aspect:.2f}, so it is reported as a "
                f"{'slot' if is_slot else 'pocket'} (threshold {SLOT_ASPECT_RATIO:g})",
            ]
            if rounded:
                evidence.append(
                    f"wall face(s) {sorted(rounded)} are cylindrical, consistent with rounded "
                    "corners or a rounded end"
                )
            if not recess.uniform_depth:
                evidence.append(
                    "the walls do not all rise by the same amount, so the shallowest was taken "
                    "as the depth"
                )

            context.claim([floor.index] + wall_indices)
            features.append(
                Feature(
                    feature_id="",
                    feature_type=FeatureType.SLOT if is_slot else FeatureType.POCKET,
                    subtype="blind",
                    solid_index=floor.solid_index,
                    dimensions={
                        "length_mm": round(length, 6),
                        "width_mm": round(width, 6),
                        "depth_mm": round(recess.depth, 6),
                        "floor_area_mm2": round(floor.area, 6),
                    },
                    location=Point3D.of(*centre.as_tuple()),
                    axis=Point3D.of(*floor.normal.normalized().as_tuple()),
                    source_faces=sorted({floor.index, *wall_indices}),
                    source_edges=sorted(
                        {index for face in [floor, *recess.walls] for index in face.edge_indices}
                    ),
                    confidence=Confidence.HIGH if recess.uniform_depth else Confidence.MEDIUM,
                    detection_method="planar_floor_enclosed_by_rising_walls",
                    evidence=evidence,
                )
            )
        return features
