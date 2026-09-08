"""Cylindrical surfaces that are not holes.

Runs after the hole recognizer, so a bore that was classified as a hole is not
reported a second time here. What is left is:

- **shafts and bosses** — complete external cylinders, material inside;
- **bores** — complete internal cylinders whose ends the hole recognizer could
  not classify, so they are reported as cylindrical surfaces rather than
  claimed as holes on evidence that was not there.

Partial sweeps are deliberately left alone: an arc of a cylinder is the
signature of a fillet, and the blend recognizer has already had its chance at
them.
"""

from __future__ import annotations

import math

from cad_costing_agent.features.grouping import group_cylindrical_faces
from cad_costing_agent.features.model import Confidence, Feature, FeatureType, Point3D
from cad_costing_agent.features.recognizers.base import RecognitionContext


class CylinderRecognizer:
    """Complete cylindrical surfaces no more specific recognizer explained."""

    name = "cylinder"

    def recognize(self, context: RecognitionContext) -> list[Feature]:
        features: list[Feature] = []
        for group in group_cylindrical_faces(context.unclaimed_faces()):
            if not group.is_complete:
                continue

            subtype = "bore" if group.is_internal else "shaft"
            side = (
                "the surface normal points toward the axis, so material lies outside it"
                if group.is_internal
                else "the surface normal points away from the axis, so material lies inside it"
            )
            context.claim(group.face_indices)
            features.append(
                Feature(
                    feature_id="",
                    feature_type=FeatureType.CYLINDER,
                    subtype=subtype,
                    solid_index=group.solid_index,
                    dimensions={
                        "diameter_mm": round(group.diameter, 6),
                        "radius_mm": round(group.radius, 6),
                        "length_mm": round(group.length, 6),
                    },
                    location=Point3D.of(*group.start_point.as_tuple()),
                    axis=Point3D.of(*group.axis_direction.as_tuple()),
                    source_faces=group.face_indices,
                    source_edges=group.edge_indices,
                    confidence=Confidence.HIGH,
                    detection_method="complete_cylindrical_surface",
                    evidence=[
                        f"face(s) {group.face_indices} sweep "
                        f"{math.degrees(group.total_sweep):.1f}° about a common axis at radius "
                        f"{group.radius:.3f} mm",
                        side,
                        f"axial length {group.length:.3f} mm measured between the surface's ends",
                    ],
                )
            )
        return features
