"""Planar faces that no more specific recognizer claimed.

This runs last. A pocket floor, a pocket wall and a chamfer are all planar, and
all of them are more informative described as what they are. What reaches this
recognizer is the remaining flat geometry — the outside faces of the part — and
reporting it completes the inventory so the face counts add up.

Bounds are measured in the plane itself rather than from an axis-aligned
bounding box, so a face that is not aligned with the global axes still reports
its own width and length.
"""

from __future__ import annotations

from cad_costing_agent.features.model import Confidence, Feature, FeatureType, Point3D
from cad_costing_agent.features.recognizers.base import RecognitionContext
from cad_costing_agent.features.recognizers.geometry import in_plane_extents


class PlanarFaceRecognizer:
    """Every remaining planar face, with its area, normal and in-plane size."""

    name = "plane"

    def recognize(self, context: RecognitionContext) -> list[Feature]:
        features: list[Feature] = []
        for face in context.unclaimed_faces():
            if face.surface_type != "plane" or face.normal is None or face.origin is None:
                continue

            extents = in_plane_extents(face, context.topology)
            dimensions = {"area_mm2": round(face.area, 6)}
            if extents is not None:
                length, width, centre = extents
                dimensions["length_mm"] = round(length, 6)
                dimensions["width_mm"] = round(width, 6)
                location = centre
            else:
                location = face.origin

            context.claim([face.index])
            features.append(
                Feature(
                    feature_id="",
                    feature_type=FeatureType.PLANE,
                    subtype=None,
                    solid_index=face.solid_index,
                    dimensions=dimensions,
                    location=Point3D.of(*location.as_tuple()),
                    axis=Point3D.of(*face.normal.as_tuple()),
                    source_faces=[face.index],
                    source_edges=list(face.edge_indices),
                    confidence=Confidence.HIGH,
                    detection_method="planar_face",
                    evidence=[
                        f"face {face.index} is a plane of area {face.area:.3f} mm²",
                        "its outward normal is reported as the feature axis",
                        *(
                            []
                            if extents is None
                            else ["length and width are measured in the plane of the face"]
                        ),
                        *(
                            []
                            if not face.inner_edge_indices
                            else [
                                f"it has {len(face.inner_edge_indices)} inner boundary edge(s), so "
                                "something passes through it"
                            ]
                        ),
                    ],
                )
            )
        return features
