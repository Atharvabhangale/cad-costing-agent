"""Whether recognized features describe a single body of revolution, and its extent.

Geometry only: this says nothing about how a part is made. It is shared by the
operation suggestions (an end face of a turned part is faced on the lathe), the
process classification (a rotational form is evidence for a turning route) and
the bar-stock proposal (the bar a turned part is cut from) — one test, so the
three cannot disagree about which parts are rotational.

A part is rotational here when every external cylinder lies on one axis, no
pocket or slot exists, and every planar face is square to that axis. Off-axis
holes are allowed: cross holes and bolt circles are secondary drilling on a
turned part.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cad_costing_agent.features.model import Feature, FeatureType, Point3D

# Two directions are parallel when |cos| is within this of 1.
PARALLEL_TOLERANCE = 1e-3
# Two axes are the same line when every point of one is this close to the other.
AXIS_OFFSET_TOLERANCE_MM = 0.01


@dataclass(frozen=True)
class RotationalForm:
    observed: bool | None  # None: no external cylinder, so nothing to test
    detail: str
    axis_origin: Point3D | None = None
    axis: Point3D | None = None
    max_diameter_mm: float | None = None
    length_mm: float | None = None  # overall extent along the axis


def _xyz(v) -> tuple[float, float, float]:
    return v if isinstance(v, tuple) else (v.x, v.y, v.z)


def _dot(a, b) -> float:
    (ax, ay, az), (bx, by, bz) = _xyz(a), _xyz(b)
    return ax * bx + ay * by + az * bz


def _sub(a, b) -> tuple[float, float, float]:
    (ax, ay, az), (bx, by, bz) = _xyz(a), _xyz(b)
    return (ax - bx, ay - by, az - bz)


def parallel(a, b) -> bool:
    return abs(abs(_dot(a, b)) - 1.0) <= PARALLEL_TOLERANCE


def _distance_to_line(point, origin, direction) -> float:
    dx, dy, dz = _sub(point, origin)
    along = _dot((dx, dy, dz), direction)
    (ux, uy, uz) = _xyz(direction)
    px, py, pz = dx - along * ux, dy - along * uy, dz - along * uz
    return math.sqrt(px * px + py * py + pz * pz)


def _ids(features) -> str:
    return ", ".join(feature.feature_id for feature in features)


def rotational_form(features: list[Feature]) -> RotationalForm:
    """Test the features for a single body of revolution, and measure it if so."""
    shafts = [
        f
        for f in features
        if f.feature_type is FeatureType.CYLINDER
        and f.subtype == "shaft"
        and f.axis is not None
        and f.location is not None
    ]
    if not shafts:
        return RotationalForm(
            None, "no external cylindrical surface was recognized, so no turning axis exists to test"
        )

    reference = shafts[0]
    for shaft in shafts[1:]:
        if not parallel(shaft.axis, reference.axis) or (
            _distance_to_line(shaft.location, reference.location, reference.axis)
            > AXIS_OFFSET_TOLERANCE_MM
        ):
            return RotationalForm(
                False,
                f"external cylinders {reference.feature_id} and {shaft.feature_id} do not share "
                "one axis, so the part is not a single body of revolution",
            )

    pockets = [f for f in features if f.feature_type in (FeatureType.POCKET, FeatureType.SLOT)]
    if pockets:
        return RotationalForm(
            False,
            f"pocket/slot feature(s) {_ids(pockets)} are not surfaces of revolution about the "
            "cylinder axis",
        )

    planes = [f for f in features if f.feature_type is FeatureType.PLANE and f.axis is not None]
    off_axis = [p for p in planes if not parallel(p.axis, reference.axis)]
    if off_axis:
        shown = _ids(off_axis[:6]) + (" …" if len(off_axis) > 6 else "")
        return RotationalForm(
            False,
            f"{len(off_axis)} planar face(s) ({shown}) are not perpendicular to the axis of "
            f"{reference.feature_id}, which a body of revolution does not have",
        )

    # The extent along the axis: shaft ends and end-face positions, projected.
    origin, axis = reference.location, reference.axis
    positions: list[float] = []
    for shaft in shafts:
        start = _dot(_sub(shaft.location, origin), axis)
        length = shaft.dimensions.get("length_mm", 0.0)
        # The cylinder runs from its start point along its own axis, which may
        # point either way relative to the reference.
        sign = 1.0 if _dot(shaft.axis, axis) > 0 else -1.0
        positions.extend([start, start + sign * length])
    positions.extend(
        _dot(_sub(plane.location, origin), axis) for plane in planes if plane.location is not None
    )
    max_diameter = max(shaft.dimensions.get("diameter_mm", 0.0) for shaft in shafts)
    length = max(positions) - min(positions) if positions else None

    return RotationalForm(
        True,
        f"{len(shafts)} external cylinder(s) ({_ids(shafts)}) share one axis within "
        f"{AXIS_OFFSET_TOLERANCE_MM} mm, and all {len(planes)} planar face(s) are perpendicular "
        "to it",
        axis_origin=origin,
        axis=axis,
        max_diameter_mm=round(max_diameter, 6) if max_diameter else None,
        length_mm=round(length, 6) if length else None,
    )


__all__ = ["RotationalForm", "parallel", "rotational_form"]
