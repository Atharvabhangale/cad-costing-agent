"""Edge treatments: fillets and chamfers.

These are the lowest-priority recognizers, and they are the ones where the
geometric evidence is weakest, so both rules are deliberately conservative:
they would rather miss a blend than invent one.

Fillet
------
A fillet is an arc of a cylinder that blends smoothly into what it meets. So
the rule is: a cylindrical face that does **not** sweep a full turn — a full
turn is a bore or a shaft, not a blend — whose lateral neighbours are
*tangent* to it. Tangency is tested directly: at the midpoint of the shared
edge, the fillet's outward normal and the neighbour's outward normal must be
parallel. Two tangent neighbours is the complete signature of a rolled edge and
is reported at high confidence; one is reported at medium, because the other
side could not be confirmed.

Concave or convex follows from which side the material is on, exactly as it
does for a bore versus a shaft.

Chamfer
-------
A chamfer is a narrow flat bevel. The rule is: a planar face whose two opposite
neighbours meet it at **equal oblique angles** — the symmetry that distinguishes
a bevel from an ordinary wall — and which is narrow relative to its length.
That is real evidence but not conclusive: a narrow oblique face could be a
designed surface rather than a broken edge. Chamfers are therefore never
reported above medium confidence, and the angle that was measured is always
included so the claim can be checked.

A chamfer around a hole is conical rather than planar, and is reported by the
hole recognizer as a countersink instead.
"""

from __future__ import annotations

import math

from cad_costing_agent.features.grouping import group_cylindrical_faces
from cad_costing_agent.features.model import Confidence, Feature, FeatureType, Point3D
from cad_costing_agent.features.recognizers.base import RecognitionContext
from cad_costing_agent.features.recognizers.geometry import in_plane_extents, outward_normal_at
from cad_costing_agent.features.topology import (
    LINEAR_TOLERANCE_MM,
    FaceDescriptor,
    TopologyModel,
    is_parallel,
)

CHAMFER_MIN_ANGLE_DEG = 5.0
CHAMFER_MAX_ANGLE_DEG = 85.0
"""A bevel meets its neighbours obliquely; outside this range it is a wall or a step."""

CHAMFER_ANGLE_SYMMETRY_DEG = 5.0
"""How closely the two neighbour angles must agree for the face to read as a bevel."""

CHAMFER_MAX_ASPECT_RATIO = 0.5
"""A chamfer's width must be at most this fraction of its length."""


def _tangent_neighbours(
    face: FaceDescriptor, topology: TopologyModel
) -> list[tuple[int, int]]:
    """Neighbours whose surface is tangent to this face at their shared edge."""
    tangent: list[tuple[int, int]] = []
    for edge_index in face.edge_indices:
        edge = topology.edges.get(edge_index)
        if edge is None:
            continue
        own_normal = outward_normal_at(face, edge.midpoint)
        if own_normal is None:
            continue
        for other_index in edge.face_indices:
            if other_index == face.index or other_index not in topology.faces:
                continue
            other = topology.faces[other_index]
            other_normal = outward_normal_at(other, edge.midpoint)
            if other_normal is None:
                continue
            if is_parallel(own_normal, other_normal):
                tangent.append((other_index, edge_index))
    return sorted(set(tangent))


class FilletRecognizer:
    """Partial cylindrical sweeps that blend tangentially into their neighbours."""

    name = "fillet"

    def recognize(self, context: RecognitionContext) -> list[Feature]:
        features: list[Feature] = []
        for group in group_cylindrical_faces(context.unclaimed_faces()):
            if group.is_complete:
                continue  # a full turn is a bore or a shaft, not a blend

            tangent: list[tuple[int, int]] = []
            for face in group.faces:
                tangent.extend(_tangent_neighbours(face, context.topology))
            tangent = sorted({pair for pair in tangent if pair[0] not in set(group.face_indices)})
            if not tangent:
                continue

            confidence = Confidence.HIGH if len(tangent) >= 2 else Confidence.MEDIUM
            subtype = "concave" if group.is_internal else "convex"
            neighbour_faces = sorted({index for index, _ in tangent})

            evidence = [
                f"face(s) {group.face_indices} sweep {math.degrees(group.total_sweep):.1f}° of a "
                f"cylinder of radius {group.radius:.3f} mm, less than a full turn",
                f"face(s) {neighbour_faces} are tangent to it at the shared edge(s), so the "
                "surface rolls smoothly into them",
                f"material lies {'outside' if group.is_internal else 'inside'} the arc, making "
                f"this a {subtype} blend",
            ]
            if confidence is Confidence.MEDIUM:
                evidence.append(
                    "only one tangent neighbour was found, so the opposite side of the blend "
                    "could not be confirmed"
                )

            context.claim(group.face_indices)
            features.append(
                Feature(
                    feature_id="",
                    feature_type=FeatureType.FILLET,
                    subtype=subtype,
                    solid_index=group.solid_index,
                    dimensions={
                        "radius_mm": round(group.radius, 6),
                        "length_mm": round(group.length, 6),
                    },
                    location=Point3D.of(*group.start_point.as_tuple()),
                    axis=Point3D.of(*group.axis_direction.as_tuple()),
                    source_faces=group.face_indices,
                    source_edges=group.edge_indices,
                    confidence=confidence,
                    detection_method="partial_cylindrical_sweep_tangent_to_neighbours",
                    evidence=evidence,
                )
            )
        return features


class ChamferRecognizer:
    """Narrow planar bevels meeting their neighbours at equal oblique angles."""

    name = "chamfer"

    def recognize(self, context: RecognitionContext) -> list[Feature]:
        topology = context.topology
        features: list[Feature] = []

        for face in context.unclaimed_faces():
            if face.surface_type != "plane" or face.normal is None:
                continue
            extents = in_plane_extents(face, topology)
            if extents is None:
                continue
            length, width, centre = extents
            if length <= LINEAR_TOLERANCE_MM or width / length > CHAMFER_MAX_ASPECT_RATIO:
                continue

            # Angles to the planar neighbours across this face's boundary.
            angles: dict[int, float] = {}
            for edge_index in face.outer_edge_indices:
                edge = topology.edges.get(edge_index)
                if edge is None or edge.curve_type != "line":
                    continue
                for other_index in edge.face_indices:
                    if other_index == face.index or other_index not in topology.faces:
                        continue
                    other = topology.faces[other_index]
                    if other.surface_type != "plane" or other.normal is None:
                        continue
                    cosine = max(-1.0, min(1.0, face.normal.normalized().dot(other.normal.normalized())))
                    angles[other_index] = math.degrees(math.acos(abs(cosine)))

            oblique = {
                index: angle
                for index, angle in angles.items()
                if CHAMFER_MIN_ANGLE_DEG <= angle <= CHAMFER_MAX_ANGLE_DEG
            }
            if len(oblique) < 2:
                continue
            ordered = sorted(oblique.items(), key=lambda item: item[0])
            first, second = ordered[0][1], ordered[1][1]
            if abs(first - second) > CHAMFER_ANGLE_SYMMETRY_DEG:
                continue

            context.claim([face.index])
            features.append(
                Feature(
                    feature_id="",
                    feature_type=FeatureType.CHAMFER,
                    subtype="planar",
                    solid_index=face.solid_index,
                    dimensions={
                        "width_mm": round(width, 6),
                        "length_mm": round(length, 6),
                        "angle_deg": round((first + second) / 2.0, 6),
                        "area_mm2": round(face.area, 6),
                    },
                    location=Point3D.of(*centre.as_tuple()),
                    axis=Point3D.of(*face.normal.normalized().as_tuple()),
                    source_faces=[face.index],
                    source_edges=list(face.edge_indices),
                    confidence=Confidence.MEDIUM,
                    detection_method="narrow_planar_face_at_equal_oblique_angles",
                    evidence=[
                        f"face {face.index} is a planar strip {length:.3f} × {width:.3f} mm, a "
                        f"width-to-length ratio of {width / length:.3f}",
                        f"it meets face {ordered[0][0]} at {first:.1f}° and face "
                        f"{ordered[1][0]} at {second:.1f}°, angles that agree within "
                        f"{CHAMFER_ANGLE_SYMMETRY_DEG:g}°",
                        "equal oblique angles on opposite sides are the signature of a bevel, "
                        "but a narrow angled face can also be a designed surface, so this is "
                        "reported at medium confidence",
                    ],
                )
            )
        return features
