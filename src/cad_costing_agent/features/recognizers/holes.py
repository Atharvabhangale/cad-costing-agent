"""Hole recognition: complete internal cylinders, classified by their ends.

The rule
--------
1. Take the cylindrical faces whose material lies *outside* the surface — the
   bores. A boss or a shaft has material inside and is not a hole.
2. Group them into complete cylinders (see `grouping.py`). A group whose faces
   do not add up to a full turn is a partial sweep — a blend, not a bore — and
   is left alone.
3. Find the circular edge at each end of the bore, and ask what lies across it:

   - a **planar face carrying that circle on its outer wire** is a floor: the
     bore stops there, so that end is CLOSED;
   - a **planar face carrying that circle on an inner wire** is a wall the bore
     breaks through: that end is OPEN;
   - a **coaxial cone or a coaxial cylinder** means the bore continues into a
     countersink, a chamfer or a diameter change: OPEN;
   - **nothing at all** means a free boundary on an open shell: OPEN, but the
     evidence is weaker;
   - anything else is UNKNOWN.

4. Both ends open is a **through** hole. Exactly one closed is a **blind** hole
   and its depth is the bore length. Both closed is an enclosed internal void,
   which is reported at low confidence because it is far more often a modelling
   artefact than a real feature.

Counterbores and countersinks
-----------------------------
These fall out of the same evidence rather than needing their own scan. A
counterbore is a wider blind bore whose floor is an annulus, with a narrower
coaxial bore starting from that annulus's inner circle; the two are merged into
one hole. A countersink is a coaxial cone at an open end, and its included
angle is twice the cone's half-angle.

Nothing here infers a drilling operation. A hole is a feature; turning features
into operations is a later step.
"""

from __future__ import annotations

import math

from cad_costing_agent.features.grouping import CylinderGroup, group_cylindrical_faces
from cad_costing_agent.features.model import Confidence, Feature, FeatureType, Point3D
from cad_costing_agent.features.recognizers.base import RecognitionContext
from cad_costing_agent.features.topology import (
    LINEAR_TOLERANCE_MM,
    EdgeDescriptor,
    FaceDescriptor,
    is_parallel,
)

END_OPEN = "open"
END_CLOSED = "closed"
END_UNKNOWN = "unknown"


class _End:
    """One end of a bore: how it terminates, and what told us."""

    def __init__(self, parameter: float, edge: EdgeDescriptor | None):
        self.parameter = parameter
        self.edge = edge
        self.state = END_UNKNOWN
        self.evidence = ""
        self.cap_face: FaceDescriptor | None = None
        self.cone_face: FaceDescriptor | None = None


class HoleRecognizer:
    """Complete internal cylinders, classified through / blind by their ends."""

    name = "hole"

    def recognize(self, context: RecognitionContext) -> list[Feature]:
        groups = [
            group
            for group in group_cylindrical_faces(context.unclaimed_faces())
            if group.is_internal and group.is_complete
        ]
        if not groups:
            return []

        analysed = [(group, self._ends(context, group)) for group in groups]
        merged_away: set[int] = set()
        features: list[Feature] = []

        # Counterbores first, so the wide bore and its pilot become one hole
        # rather than a blind hole plus a through hole on the same axis.
        for index, (group, ends) in enumerate(analysed):
            if index in merged_away:
                continue
            partner = self._counterbore_partner(analysed, index, merged_away)
            if partner is None:
                continue
            partner_index, cap = partner
            pilot_group, pilot_ends = analysed[partner_index]
            merged_away.update({index, partner_index})
            features.append(self._counterbore_feature(context, group, ends, pilot_group, pilot_ends, cap))

        for index, (group, ends) in enumerate(analysed):
            if index in merged_away:
                continue
            features.append(self._hole_feature(context, group, ends))

        return features

    # -- end classification -------------------------------------------------

    def _end_circle(self, context: RecognitionContext, group: CylinderGroup, parameter: float):
        """The circular edge closing the bore at this axial parameter."""
        best: EdgeDescriptor | None = None
        for edge_index in group.edge_indices:
            edge = context.topology.edges.get(edge_index)
            if edge is None or edge.curve_type != "circle" or edge.radius is None:
                continue
            if abs(edge.radius - group.radius) > LINEAR_TOLERANCE_MM:
                continue
            if edge.centre is None:
                continue
            if abs(group.parameter_of(edge.centre) - parameter) > LINEAR_TOLERANCE_MM:
                continue
            if best is None or edge.index < best.index:
                best = edge
        return best

    def _ends(self, context: RecognitionContext, group: CylinderGroup) -> tuple[_End, _End]:
        group_faces = set(group.face_indices)
        ends = []
        for parameter in (group.axial_start, group.axial_end):
            end = _End(parameter, self._end_circle(context, group, parameter))
            if end.edge is None:
                end.state = END_UNKNOWN
                end.evidence = f"no circular edge closes the bore at axial parameter {parameter:.3f}"
                ends.append(end)
                continue

            others = [
                context.topology.faces[i]
                for i in end.edge.face_indices
                if i not in group_faces and i in context.topology.faces
            ]
            if not others:
                end.state = END_OPEN
                end.evidence = f"edge {end.edge.index} bounds no other face, so the bore is open there"
                ends.append(end)
                continue

            other = sorted(others, key=lambda f: f.index)[0]
            if other.surface_type == "plane" and other.normal is not None and is_parallel(
                other.normal, group.axis_direction
            ):
                if end.edge.index in other.outer_edge_indices:
                    end.state = END_CLOSED
                    end.cap_face = other
                    end.evidence = (
                        f"face {other.index} is a plane perpendicular to the axis and edge "
                        f"{end.edge.index} is its outer boundary, so it is the floor of the bore"
                    )
                else:
                    end.state = END_OPEN
                    end.cap_face = other
                    end.evidence = (
                        f"edge {end.edge.index} is an inner boundary of face {other.index}, so "
                        "the bore breaks through that face"
                    )
            elif other.surface_type == "cone" and other.axis_direction is not None and is_parallel(
                other.axis_direction, group.axis_direction
            ):
                end.state = END_OPEN
                end.cone_face = other
                end.evidence = (
                    f"face {other.index} is a coaxial cone, so the bore opens into a "
                    "countersink or chamfer"
                )
            elif other.surface_type == "cylinder" and other.axis_direction is not None and is_parallel(
                other.axis_direction, group.axis_direction
            ):
                end.state = END_OPEN
                end.evidence = f"face {other.index} is a coaxial cylinder, so the bore continues"
            else:
                end.state = END_UNKNOWN
                end.evidence = (
                    f"face {other.index} is a {other.surface_type} and does not classify this end"
                )
            ends.append(end)
        return ends[0], ends[1]

    # -- counterbore --------------------------------------------------------

    def _counterbore_partner(self, analysed, index, merged_away):
        """A narrower coaxial bore starting from this bore's annular floor."""
        group, ends = analysed[index]
        closed = [end for end in ends if end.state == END_CLOSED and end.cap_face is not None]
        if len(closed) != 1:
            return None
        cap = closed[0].cap_face
        if not cap.inner_edge_indices:
            return None

        for other_index, (other, other_ends) in enumerate(analysed):
            if other_index == index or other_index in merged_away:
                continue
            if other.solid_index != group.solid_index or not other.is_coaxial_with(group):
                continue
            if other.radius >= group.radius - LINEAR_TOLERANCE_MM:
                continue
            # The pilot must start exactly where the wide bore's floor is, and
            # its opening circle must be that floor's inner boundary.
            touches = min(
                abs(other.axial_start - closed[0].parameter),
                abs(other.axial_end - closed[0].parameter),
            )
            if touches > LINEAR_TOLERANCE_MM:
                continue
            shares_annulus = any(
                edge_index in cap.inner_edge_indices for edge_index in other.edge_indices
            )
            if not shares_annulus:
                continue
            return other_index, cap
        return None

    def _counterbore_feature(self, context, wide, wide_ends, pilot, pilot_ends, cap):
        classification, confidence, evidence = self._classify(pilot_ends)
        evidence = [
            f"face(s) {wide.face_indices} form a complete bore of Ø{wide.diameter:.3f} mm",
            f"face {cap.index} is an annular floor of that bore",
            f"face(s) {pilot.face_indices} form a narrower coaxial bore of Ø{pilot.diameter:.3f} mm "
            "starting at that annulus",
            *evidence,
        ]
        context.claim(wide.face_indices + pilot.face_indices + [cap.index])

        entry = self._entry_point(wide, wide_ends)
        return Feature(
            feature_id="",
            feature_type=FeatureType.HOLE,
            subtype="counterbore",
            solid_index=pilot.solid_index,
            dimensions={
                "diameter_mm": round(pilot.diameter, 6),
                "depth_mm": round(pilot.length, 6),
                "counterbore_diameter_mm": round(wide.diameter, 6),
                "counterbore_depth_mm": round(wide.length, 6),
            },
            location=Point3D.of(*entry.as_tuple()),
            axis=Point3D.of(*wide.axis_direction.as_tuple()),
            source_faces=sorted(set(wide.face_indices + pilot.face_indices + [cap.index])),
            source_edges=sorted(set(wide.edge_indices + pilot.edge_indices)),
            confidence=confidence if classification != "ambiguous" else Confidence.LOW,
            detection_method="coaxial_wide_and_narrow_bores_joined_by_annular_floor",
            evidence=evidence,
        )

    # -- plain holes --------------------------------------------------------

    def _classify(self, ends: tuple[_End, _End]):
        states = [end.state for end in ends]
        evidence = [end.evidence for end in ends if end.evidence]
        if END_UNKNOWN in states:
            return "ambiguous", Confidence.LOW, evidence
        if states.count(END_OPEN) == 2:
            return "through", Confidence.HIGH, evidence
        if states.count(END_CLOSED) == 1:
            return "blind", Confidence.HIGH, evidence
        return "enclosed", Confidence.LOW, evidence

    def _entry_point(self, group: CylinderGroup, ends: tuple[_End, _End]):
        for end in ends:
            if end.state == END_OPEN:
                return group.point_at(end.parameter)
        return group.start_point

    def _hole_feature(self, context: RecognitionContext, group: CylinderGroup, ends):
        classification, confidence, evidence = self._classify(ends)
        evidence = [
            f"face(s) {group.face_indices} sweep {math.degrees(group.total_sweep):.1f}° about a "
            f"common axis at radius {group.radius:.3f} mm, so they form one complete bore",
            "the surface normal points toward the axis, so the material is outside it",
            *evidence,
        ]

        dimensions = {
            "diameter_mm": round(group.diameter, 6),
            "depth_mm": round(group.length, 6),
        }
        subtype = classification
        faces = list(group.face_indices)

        # A blind hole's floor belongs to the hole. Without this it would be
        # reported separately as a small circular plane, which is true but
        # tells the reader less than "this is the bottom of that hole".
        for end in ends:
            if end.state == END_CLOSED and end.cap_face is not None:
                faces.append(end.cap_face.index)

        countersink = next((end.cone_face for end in ends if end.cone_face is not None), None)
        if countersink is not None and countersink.semi_angle is not None:
            included = abs(math.degrees(countersink.semi_angle)) * 2.0
            dimensions["countersink_angle_deg"] = round(included, 6)
            if countersink.bbox_min is not None:
                dimensions["countersink_diameter_mm"] = round(
                    max(
                        countersink.bbox_max.x - countersink.bbox_min.x,
                        countersink.bbox_max.y - countersink.bbox_min.y,
                        countersink.bbox_max.z - countersink.bbox_min.z,
                    ),
                    6,
                )
            subtype = f"{classification}_countersunk"
            faces.append(countersink.index)
            evidence.append(
                f"face {countersink.index} is a coaxial cone at an open end with an included "
                f"angle of {included:.1f}°, so the bore is countersunk"
            )

        context.claim(faces)
        entry = self._entry_point(group, ends)
        return Feature(
            feature_id="",
            feature_type=FeatureType.HOLE,
            subtype=subtype,
            solid_index=group.solid_index,
            dimensions=dimensions,
            location=Point3D.of(*entry.as_tuple()),
            axis=Point3D.of(*group.axis_direction.as_tuple()),
            source_faces=sorted(set(faces)),
            source_edges=sorted(set(group.edge_indices)),
            confidence=confidence,
            detection_method="complete_internal_cylinder_classified_by_end_conditions",
            evidence=evidence,
        )
