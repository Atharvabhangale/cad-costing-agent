"""Which way a tool has to come from to reach a feature, and whether it can.

    recognized features + the B-Rep they came from → approach directions → access check

This is evidence for a *setup proposal*, nothing more. It reads the shape the
recognizers already read and the features they already produced; it parses no
file of its own, decides no grouping, and knows nothing about operations,
times, rates or money. `setup_proposal.py` turns what this module reports into
a proposed grouping of a plan's operations, and only a person applying that
proposal changes a plan.

Approach directions
-------------------
A direction here is the way the tool points *away* from the part — the same
sense as an outward face normal — so "+Z (top)" means the tool comes down from
above. Every candidate is read from the B-Rep, never assumed:

- **a blind hole** — its floor is a planar face of the hole, and that floor's
  outward-from-material normal points out through the one open end. That end
  is the only approach.
- **a through hole** — both ends are open, so both outward axis directions are
  candidates, each starting from its own end of the bore. A countersunk through
  hole is approached from the countersink's end only: the countersink is cut
  from that side.
- **a counterbore** — the annular floor of the wide bore faces out through its
  open end; that is the only approach, whatever the pilot does.
- **a pocket or slot** — the floor's outward normal.
- **a planar face** — its outward normal (facing). A planar face is also
  reachable by the *flank* of a tool whose axis lies in the face's plane
  (side milling), along any other candidate direction perpendicular to the
  face; those candidates are labelled `flank` and only count when the face is
  approached from a direction some other feature already needs.
- **a fillet or chamfer** — the outward normals of the planar faces it blends
  between, read from the edges it shares with them. Only planes it faces
  toward count: the end faces a blend merely meets are perpendicular to it,
  and a tool along them would run down its length.

Features with no approach rule are reported with the reason rather than
guessed at: a hole whose ends the recognizer could not classify, an enclosed
void, an internal cylinder that no hole rule claimed, an external cylinder
(turning has no milling approach; the proposal leaves turned work out before
it gets here), or a blend with no planar neighbour.

The access check
----------------
For each candidate direction, straight rays are cast from the feature along
that direction — from the centre and four rim points of a hole's open end, or
from a grid of points on a face, each lifted a hundredth of a millimetre off
the surface — and the direction is **reachable** only when every ray leaves the
part without meeting material. One blocked ray makes the direction unreachable,
and the report says how many rays were blocked and how far away the nearest hit
was, so "not reachable along +Z: 5 of 5 rays hit material 30 mm away" is a
sentence the evidence supports.

**A ray is not a tool.** What is not checked, and is stated on every proposal:
the holder and shank (anything wider than the feature), the tool's length or
reach, undercuts that a straight ray passes but a cutter cannot, and the
fixture itself. A direction reported reachable is necessary for access, not
sufficient.

Everything here is deterministic: the same file and features give the same
directions, the same rays and the same answers.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from cad_costing_agent.features.model import Feature, FeatureType, Point3D
from cad_costing_agent.features.rays import FACE_GRID, FINE_FACE_GRID, RAY_START_MM, ShapeRays

# A hundredth of a millimetre: how far a ray's origin is lifted off the surface
# it starts from, so the surface itself is not reported as the first thing hit.
RAY_LIFT_MM = 0.01
# Rim rays of a hole sit at this fraction of the radius, inside the bore.
RIM_FRACTION = 0.9
# Grid points per face edge (`FACE_GRID`, then `FINE_FACE_GRID` for a face too
# thin for it) and the near-hit cut-off (`RAY_START_MM`) are `features.rays`'s,
# imported above: 3 × 3 is nine rays per face per direction.
# Directions within this of each other (1 - dot) are the same direction.
SAME_DIRECTION = 1e-6
# A face normal this close to perpendicular to a direction is a flank face.
PERPENDICULAR = 1e-6

PRIMARY = "primary"
FLANK = "flank"

REACHABLE = "reachable"
UNREACHABLE = "unreachable"
NO_APPROACH = "no_approach"

AXIS_ORDER = ("+Z", "-Z", "+X", "-X", "+Y", "-Y")
AXIS_LABELS = {
    "+Z": "+Z (top)",
    "-Z": "−Z (bottom)",
    "+X": "+X",
    "-X": "−X",
    "+Y": "+Y",
    "-Y": "−Y",
}

LIMITATIONS = (
    "A ray is not a tool: straight rays from the feature are checked, not the cutter, holder or "
    "spindle, so a holder or shank wider than the feature can still collide where every ray is "
    "clear.",
    "Tool length and reach are not checked: a deep feature reachable along a clear line may still "
    "need a tool longer than any the shop has.",
    "Undercuts are not detected: a straight ray can pass where a cutter of any real width cannot.",
    "The fixture is not modelled: whatever holds the part may itself block a direction reported "
    "clear here.",
)


class ApproachUnavailable(ValueError):
    """The file carries no exact geometry to cast rays against."""


class ApproachCandidate(BaseModel):
    """One direction a tool could come from, and whether straight rays get there."""

    direction: Point3D = Field(description="Unit vector pointing from the feature out to the tool.")
    direction_key: str = Field(description="'+Z' for an axis direction, otherwise the rounded vector.")
    direction_label: str = Field(description="How the direction is named to a person, e.g. '+Z (top)'.")
    kind: str = Field(
        description=(
            "'primary' when the direction is the feature's own (a hole's open end, a floor or "
            "face normal, a blended face's normal); 'flank' for a planar face side-milled by a "
            "tool whose axis lies in its plane."
        )
    )
    basis: str = Field(description="The B-Rep fact this candidate was read from.")
    origin: Point3D | None = Field(
        default=None, description="Where the representative ray starts. A pointer for the viewer."
    )
    rays_cast: int = 0
    rays_blocked: int = 0
    nearest_hit_mm: float | None = Field(
        default=None, description="Distance to the nearest material any ray met, or null if none did."
    )
    reachable: bool = False
    reason: str = ""


class FeatureApproach(BaseModel):
    """Every candidate direction for one feature, with the access check on each."""

    feature_id: str
    feature_type: str
    subtype: str | None = None
    status: str = Field(description="'reachable', 'unreachable' or 'no_approach'.")
    rule: str = Field(description="Which approach rule applied, e.g. 'blind_hole_open_end'.")
    reason: str
    candidates: list[ApproachCandidate] = Field(default_factory=list)

    @property
    def reachable_keys(self) -> list[str]:
        return [c.direction_key for c in self.candidates if c.reachable]


# ---------------------------------------------------------------- directions


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(c * c for c in vector))
    if length == 0:
        return vector
    return tuple(c / length for c in vector)  # type: ignore[return-value]


def _clean(value: float) -> float:
    rounded = round(value, 6)
    return 0.0 if rounded == 0 else rounded


def direction_key(direction: tuple[float, float, float]) -> str:
    """A stable name for a direction: '+Z' for an axis, else the rounded vector."""
    x, y, z = (_clean(c) for c in _unit(direction))
    for name, axis in (("X", (1.0, 0.0, 0.0)), ("Y", (0.0, 1.0, 0.0)), ("Z", (0.0, 0.0, 1.0))):
        dot = x * axis[0] + y * axis[1] + z * axis[2]
        if abs(1.0 - abs(dot)) < SAME_DIRECTION:
            return ("+" if dot > 0 else "-") + name
    return f"({x:g}, {y:g}, {z:g})"


def direction_label(key: str) -> str:
    return AXIS_LABELS.get(key, key)


def direction_order(key: str) -> tuple:
    """The fixed order ties are broken in: the six axes first, then the rest by name."""
    if key in AXIS_ORDER:
        return (0, AXIS_ORDER.index(key), "")
    return (1, 0, key)


def _same(a, b) -> bool:
    return abs(1.0 - (a[0] * b[0] + a[1] * b[1] + a[2] * b[2])) < SAME_DIRECTION


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _add(a, b, scale: float = 1.0):
    return (a[0] + b[0] * scale, a[1] + b[1] * scale, a[2] + b[2] * scale)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _perpendicular_basis(direction):
    helper = (1.0, 0.0, 0.0) if abs(direction[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = _unit(_cross(direction, helper))
    v = _unit(_cross(direction, u))
    return u, v


# ---------------------------------------------------------------- the B-Rep side
#
# The shape index, face sampling and ray casting live in `features.rays`, shared
# with the pull-direction analysis so there is one ray caster. Moved unchanged.

_Shape = ShapeRays


# ---------------------------------------------------------------- the access check


def _check(
    model: _Shape,
    *,
    direction,
    kind: str,
    basis: str,
    origins: list[tuple],
    facing_normals: list[tuple] | None = None,
) -> ApproachCandidate:
    """Cast one ray per origin along `direction`; reachable only if every one is clear."""
    direction = _unit(direction)
    key = direction_key(direction)
    label = direction_label(key)
    blocked = 0
    unevaluated = 0
    facing_away = 0
    nearest: float | None = None
    for position, origin in enumerate(origins):
        if facing_normals is not None and _dot(facing_normals[position], direction) < -PERPENDICULAR:
            # This part of the surface faces away from a tool on this side.
            facing_away += 1
            blocked += 1
            continue
        evaluated, hit = model.first_hit(origin, direction)
        if not evaluated:
            unevaluated += 1
            blocked += 1
            continue
        if hit is not None:
            blocked += 1
            nearest = hit if nearest is None else min(nearest, hit)

    cast = len(origins)
    reachable = cast > 0 and blocked == 0
    if cast == 0:
        reason = f"not checked along {label}: no point on the feature could be sampled to cast from"
    elif reachable:
        reason = f"reachable along {label}: all {cast} straight ray(s) left the part without meeting material"
    else:
        parts = []
        hit_count = blocked - facing_away - unevaluated
        if hit_count:
            parts.append(
                f"{hit_count} of {cast} ray(s) hit material"
                + (f", the nearest {nearest:.3f} mm away" if nearest is not None else "")
            )
        if facing_away:
            parts.append(f"{facing_away} of {cast} sampled point(s) face away from a tool on that side")
        if unevaluated:
            parts.append(f"{unevaluated} ray(s) could not be evaluated, so are counted as blocked")
        reason = f"not reachable along {label}: " + "; ".join(parts)

    return ApproachCandidate(
        direction=Point3D.of(*direction),
        direction_key=key,
        direction_label=label,
        kind=kind,
        basis=basis,
        origin=Point3D.of(*origins[0]) if origins else None,
        rays_cast=cast,
        rays_blocked=blocked,
        nearest_hit_mm=None if nearest is None else round(nearest, 6),
        reachable=reachable,
        reason=reason,
    )


def _end_origins(centre, direction, radius: float) -> list[tuple]:
    """A hole end's centre and four rim points, lifted just outside the opening."""
    start = _add(centre, direction, RAY_LIFT_MM)
    u, v = _perpendicular_basis(direction)
    rim = RIM_FRACTION * radius
    return [start] + [
        _add(start, axis, sign * rim) for axis in (u, v) for sign in (1.0, -1.0)
    ]


def _face_origins(model: _Shape, faces: list[int]) -> tuple[list[tuple], list[tuple]]:
    origins: list[tuple] = []
    normals: list[tuple] = []
    for index in faces:
        for point, normal in model.samples(index):
            origins.append(_add(point, normal, RAY_LIFT_MM))
            normals.append(normal)
    return origins, normals


# ---------------------------------------------------------------- the rules


def _no_approach(feature: Feature, rule: str, reason: str) -> FeatureApproach:
    return FeatureApproach(
        feature_id=feature.feature_id,
        feature_type=feature.feature_type.value,
        subtype=feature.subtype,
        status=NO_APPROACH,
        rule=rule,
        reason=reason,
    )


def _bore_ends(model: _Shape, feature: Feature, radius: float):
    """The two ends of a bore of this radius, as points on its axis, from the B-Rep."""
    if feature.axis is None or feature.location is None:
        return None
    axis = _unit(feature.axis.as_tuple())
    base = feature.location.as_tuple()
    params: list[float] = []
    for index in feature.source_faces:
        info = model.describe(index)
        if info["type"] != "cylinder" or abs(info["radius"] - radius) > 1e-3:
            continue
        for v in info["v"]:
            point = _add(info["location"], _unit(info["axis"]), v)
            params.append(_dot(_add(point, base, -1.0), axis))
    if not params:
        return None
    low, high = min(params), max(params)
    return _add(base, axis, low), _add(base, axis, high), axis


def _plane_faces_square_to(model: _Shape, feature: Feature, axis) -> list[tuple[int, tuple, tuple]]:
    """(index, outward normal, origin) of the feature's planar faces square to `axis`."""
    found = []
    for index in feature.source_faces:
        info = model.describe(index)
        if info["type"] == "plane" and abs(1.0 - abs(_dot(_unit(info["normal"]), axis))) < 1e-6:
            found.append((index, _unit(info["normal"]), info["origin"]))
    return found


def _hole(model: _Shape, feature: Feature) -> FeatureApproach:
    subtype = (feature.subtype or "").lower()
    countersunk = subtype.endswith("_countersunk")
    base = subtype[: -len("_countersunk")] if countersunk else subtype
    diameter = feature.dimensions.get("diameter_mm")

    if base in ("ambiguous", "enclosed") or not base:
        return _no_approach(
            feature,
            f"{base or 'unclassified'}_hole_no_approach",
            "the recognizer could not establish which end of this bore is open"
            if base != "enclosed"
            else "both ends of this bore are closed, so no straight approach exists",
        )
    if diameter is None:
        return _no_approach(feature, "hole_without_diameter", "no diameter was recorded for this hole")

    if base == "counterbore":
        wide = feature.dimensions.get("counterbore_diameter_mm")
        ends = _bore_ends(model, feature, (wide or 0.0) / 2.0) if wide else None
        if ends is None:
            return _no_approach(feature, "counterbore_without_wide_bore", "the wide bore was not found on the B-Rep")
        low, high, axis = ends
        floors = [f for f in _plane_faces_square_to(model, feature, axis)]
        # The annulus is the floor of the wide bore, at one of its two ends.
        annulus = next(
            (
                f
                for f in floors
                if min(abs(_dot(_add(f[2], low, -1.0), axis)), abs(_dot(_add(f[2], high, -1.0), axis)))
                < 1e-3
            ),
            None,
        )
        if annulus is None:
            return _no_approach(feature, "counterbore_without_floor", "the counterbore's annular floor was not found on the B-Rep")
        index, normal, _ = annulus
        open_end = high if _dot(high, normal) > _dot(low, normal) else low
        candidate = _check(
            model,
            direction=normal,
            kind=PRIMARY,
            basis=(
                f"face {index} is the counterbore's annular floor; its outward normal points out "
                "through the counterbore's open end, the only side the step can be cut from"
            ),
            origins=_end_origins(open_end, normal, wide / 2.0),
        )
        return _finish(feature, "counterbore_open_end", [candidate])

    ends = _bore_ends(model, feature, diameter / 2.0)
    if ends is None:
        return _no_approach(feature, "hole_without_bore", "the bore's cylindrical faces were not found on the B-Rep")
    low, high, axis = ends

    if base == "blind":
        floors = _plane_faces_square_to(model, feature, axis)
        if not floors:
            return _no_approach(feature, "blind_hole_without_floor", "the blind hole's floor was not found on the B-Rep")
        index, normal, origin = floors[0]
        open_end = high if _dot(high, normal) > _dot(low, normal) else low
        candidate = _check(
            model,
            direction=normal,
            kind=PRIMARY,
            basis=(
                f"face {index} is the floor of this blind hole; its outward-from-material normal "
                "points out through the one open end"
            ),
            origins=_end_origins(open_end, normal, diameter / 2.0),
        )
        return _finish(feature, "blind_hole_open_end", [candidate])

    if base == "through":
        candidates = []
        options = [(high, axis), (low, (-axis[0], -axis[1], -axis[2]))]
        if countersunk:
            cone = next(
                (i for i in feature.source_faces if model.describe(i)["type"] == "cone"), None
            )
            if cone is not None:
                centre = model.face_centre(cone)
                options = [
                    min(
                        options,
                        key=lambda o: sum((o[0][k] - centre[k]) ** 2 for k in range(3)),
                    )
                ]
        for end, outward in options:
            candidates.append(
                _check(
                    model,
                    direction=outward,
                    kind=PRIMARY,
                    basis=(
                        "the countersunk end of a through hole: the countersink is cut from this side"
                        if countersunk and len(options) == 1
                        else "one open end of a through hole, pointing out along the bore's axis"
                    ),
                    origins=_end_origins(end, outward, diameter / 2.0),
                )
            )
        return _finish(
            feature, "through_hole_countersunk_end" if countersunk and len(options) == 1 else "through_hole_both_ends", candidates
        )

    return _no_approach(feature, "unknown_hole_subtype", f"no approach rule for hole subtype '{feature.subtype}'")


def _recess(model: _Shape, feature: Feature) -> FeatureApproach:
    if feature.axis is None:
        return _no_approach(feature, "recess_without_axis", "no floor normal was recorded for this recess")
    axis = _unit(feature.axis.as_tuple())
    floors = [f for f in _plane_faces_square_to(model, feature, axis) if _same(f[1], axis)]
    if not floors:
        return _no_approach(feature, "recess_without_floor", "the floor was not found on the B-Rep")
    index = floors[0][0]
    origins, normals = _face_origins(model, [index])
    candidate = _check(
        model,
        direction=axis,
        kind=PRIMARY,
        basis=f"face {index} is the {feature.feature_type.value}'s floor; its outward normal is the approach",
        origins=origins,
        facing_normals=normals,
    )
    return _finish(feature, f"{feature.feature_type.value}_floor_normal", [candidate])


def _plane(model: _Shape, feature: Feature) -> FeatureApproach:
    if not feature.source_faces:
        return _no_approach(feature, "plane_without_face", "no CAD face was recorded for this plane")
    index = feature.source_faces[0]
    info = model.describe(index)
    if info["type"] != "plane":
        return _no_approach(feature, "plane_not_planar", f"face {index} is not a plane on the B-Rep")
    normal = _unit(info["normal"])
    origins, normals = _face_origins(model, [index])
    candidate = _check(
        model,
        direction=normal,
        kind=PRIMARY,
        basis=f"face {index}'s outward normal: the face is faced from this side",
        origins=origins,
        facing_normals=normals,
    )
    return _finish(feature, "plane_outward_normal", [candidate])


def _blend(model: _Shape, feature: Feature) -> FeatureApproach:
    neighbours = model.planar_neighbour_normals(feature.source_faces)
    if not neighbours:
        return _no_approach(
            feature,
            "blend_without_planar_neighbour",
            f"this {feature.feature_type.value} meets no planar face, so no approach direction is read from the B-Rep",
        )
    origins, normals = _face_origins(model, feature.source_faces)
    if not normals:
        return _no_approach(feature, "blend_without_samples", "no point on the blend could be sampled")
    # The faces a blend *blends* are the ones it faces toward; the end faces it
    # merely meets (square to a fillet's axis, or beside a chamfer) are
    # perpendicular to it, and a tool along them would run down its length.
    facing = _unit(tuple(sum(n[k] for n in normals) for k in range(3)))
    candidates: list[ApproachCandidate] = []
    seen: list[tuple] = []
    for index, normal in neighbours:
        if _dot(normal, facing) <= 1e-3 or any(_same(normal, other) for other in seen):
            continue
        seen.append(normal)
        candidates.append(
            _check(
                model,
                direction=normal,
                kind=PRIMARY,
                basis=(
                    f"the outward normal of face {index}, a plane this {feature.feature_type.value} "
                    "blends into"
                ),
                origins=origins,
                facing_normals=normals,
            )
        )
    if not candidates:
        return _no_approach(
            feature,
            "blend_without_blended_plane",
            f"no plane this {feature.feature_type.value} blends into faces the same way it does",
        )
    return _finish(feature, f"{feature.feature_type.value}_neighbour_normals", candidates)


def _finish(feature: Feature, rule: str, candidates: list[ApproachCandidate]) -> FeatureApproach:
    candidates = sorted(candidates, key=lambda c: direction_order(c.direction_key))
    reachable = [c for c in candidates if c.reachable]
    if reachable:
        status = REACHABLE
        reason = "reachable along " + ", ".join(c.direction_label for c in reachable)
    else:
        status = UNREACHABLE
        reason = "not reachable along any candidate direction: " + "; ".join(c.reason for c in candidates)
    return FeatureApproach(
        feature_id=feature.feature_id,
        feature_type=feature.feature_type.value,
        subtype=feature.subtype,
        status=status,
        rule=rule,
        reason=reason,
        candidates=candidates,
    )


def _flank_candidates(
    model: _Shape, feature: Feature, approach: FeatureApproach, universe: list[tuple]
) -> FeatureApproach:
    """Add side-milling candidates to a planar face, along directions in its plane."""
    if not approach.candidates:
        return approach
    normal = approach.candidates[0].direction.as_tuple()
    origins, _ = _face_origins(model, feature.source_faces[:1])
    extra = []
    for direction in universe:
        if abs(_dot(direction, normal)) >= PERPENDICULAR:
            continue
        extra.append(
            _check(
                model,
                direction=direction,
                kind=FLANK,
                basis=(
                    "side milling: a tool whose axis lies in this face's plane cuts it with its "
                    "flank, so the face can be milled in a setup approached along this direction"
                ),
                origins=origins,
            )
        )
    if not extra:
        return approach
    return _finish(feature, approach.rule, [*approach.candidates, *extra])


_RULES = {
    FeatureType.HOLE: _hole,
    FeatureType.POCKET: _recess,
    FeatureType.SLOT: _recess,
    FeatureType.PLANE: _plane,
    FeatureType.FILLET: _blend,
    FeatureType.CHAMFER: _blend,
}


def approach_directions(shape, features: list[Feature]) -> list[FeatureApproach]:
    """Candidate approach directions and the access check, for each feature given.

    `shape` must be the shape these features were recognized from, so that
    their `source_faces` index into it. Faceted features carry no faces to read
    and are refused.
    """
    if any(feature.source_type != "brep" for feature in features):
        raise ApproachUnavailable(
            "Approach directions need exact B-Rep faces; features reconstructed from polygons "
            "carry none to cast rays from."
        )
    model = _Shape(shape)
    results: list[FeatureApproach] = []
    for feature in features:
        rule = _RULES.get(feature.feature_type)
        if rule is None:
            results.append(
                _no_approach(
                    feature,
                    f"{feature.feature_type.value}_no_approach",
                    (
                        "an external or unclassified cylinder has no milling approach rule; "
                        "turned work has no milling approach at all"
                    )
                    if feature.feature_type is FeatureType.CYLINDER
                    else f"no approach rule for a {feature.feature_type.value}",
                )
            )
            continue
        results.append(rule(model, feature))

    # Flank candidates only along directions some feature already needs, so a
    # side face never invents a setup of its own by being side-milled.
    universe: list[tuple] = []
    for approach in results:
        for candidate in approach.candidates:
            if candidate.kind != PRIMARY or not candidate.reachable:
                continue
            vector = candidate.direction.as_tuple()
            if not any(_same(vector, other) for other in universe):
                universe.append(vector)
    universe.sort(key=lambda v: direction_order(direction_key(v)))

    by_id = {feature.feature_id: feature for feature in features}
    return [
        _flank_candidates(model, by_id[a.feature_id], a, universe)
        if a.rule == "plane_outward_normal"
        else a
        for a in results
    ]


# ---------------------------------------------------------------- reading the file


def shape_and_features(data: bytes, source_format: str, *, component_id: str | None = None):
    """The exact shape and the features recognized from it, or why there is none.

    `source_format` is the canonical name `cad_import` already identified — this
    reads no file extension. The shape comes from the same single readers and the
    features from the same `recognize_features` every other path uses, so the
    feature ids here are the ids the plan's operations link to.
    """
    from cad_costing_agent.features import recognize_component_features, recognize_features

    if component_id is not None:
        from cad_costing_agent.cad_assembly import read_step_assembly

        structure = read_step_assembly(data)
        definition = structure.definitions.get(component_id)
        if definition is None:
            raise LookupError(f"No component '{component_id}' in this file.")
        if not definition.has_geometry:
            raise ApproachUnavailable(
                f"Component '{definition.name}' carries no geometry the reader could transfer, "
                "so there is nothing to cast rays against."
            )
        recognition = recognize_component_features(
            definition.shape,
            component_id=definition.name or component_id,
            component_name=definition.name,
        )
        return definition.shape, recognition

    if source_format == "step":
        from cad_costing_agent.cad_geometry import read_step_shape
        from cad_costing_agent.features.step_text import classify_step_bytes

        representation = classify_step_bytes(data)
        if representation.is_faceted:
            raise ApproachUnavailable(
                "This STEP file is a faceted export: it describes the part as loose polygons, with "
                "no exact faces to cast rays from or read a hole's open end off. Setups stay as "
                "grouped; regroup them by hand. " + " ".join(representation.notes)
            )
        shape = read_step_shape(data)
        return shape, recognize_features(shape, filename="", source_format="step")

    if source_format == "iges":
        from cad_costing_agent.cad_iges import (
            analyse_iges_shape,
            carries_a_solid,
            ensure_readable,
            read_iges_shape,
        )

        if not carries_a_solid(data):
            raise ApproachUnavailable(
                "This IGES file stores loose surfaces rather than a solid. Its cylinders come back "
                "as generic surfaces, so holes are not recognized and there is no inside or outside "
                "to cast rays through. Re-export with solid geometry, or as STEP."
            )
        ensure_readable(data)
        topology = analyse_iges_shape(read_iges_shape(data))
        if topology.shape is None or topology.sewing_applied:
            raise ApproachUnavailable(
                "This IGES file's geometry had to be sewn from loose surfaces, so it carries no "
                "exact solid to cast rays against."
            )
        return topology.shape, recognize_features(topology.shape, filename="", source_format="iges")

    raise ApproachUnavailable(
        "Approach directions need an analytic B-Rep (STEP, or IGES carrying a solid). This file "
        "does not carry one."
    )


__all__ = [
    "ApproachCandidate",
    "ApproachUnavailable",
    "FeatureApproach",
    "LIMITATIONS",
    "approach_directions",
    "direction_key",
    "direction_label",
    "direction_order",
    "shape_and_features",
]
