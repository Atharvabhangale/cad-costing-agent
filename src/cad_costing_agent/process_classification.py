"""Manufacturing family and process route: a recommendation, never a verdict.

    CAD → features → suggestions → **family + route recommendation** → engineer confirms

Two questions are kept apart because they are answered differently:

- **Family** — *what kind of making is this?* Machining, sheet metal,
  casting, forging, fabrication, additive, moulding, purchased, or unknown.
- **Route** — *which stages, in which order?* CNC milling plus drilling;
  sheet cutting plus bending; casting followed by machining. A route is a list
  of `RouteStage`s, and each stage says whether this application has a cost
  model for it.

What the geometry can and cannot say
------------------------------------
Shape does not determine process. A drilled 2 mm plate is equally a laser-cut
blank and a milled one; a housing with bored holes may be a casting machined
afterwards; a plain cylinder may be turned or bought as bar. Material,
tolerances, finish, quantity and the shop's machines decide those, and none of
them is in a CAD file. So:

- **Confidence is capped at `medium`** for any family, however clean the
  evidence. Geometry can never exclude a near-net-shape blank that is then
  machined. A faceted model, extra solids or faces nobody recognized lower it
  to `low`. Like every confidence in this codebase it is a three-way
  classification of which rule branches held, not a probability.
- **Only three signals are read**, each of them established from features the
  recognizers already report: machining features (pockets, slots, blind and
  counterbored holes), a *rotational form* (every external cylinder on one
  axis and every planar face perpendicular to it) and a *constant wall
  thickness* (most planar area in opposite pairs at one separation, with the
  volume equal to that thickness times the paired area). Each is reported as
  observed, not observed, or not assessable — with the reason.
- **Casting, forging, moulding, welding and additive manufacturing are never
  proposed.** No recognizer here detects draft angles, parting lines, weld
  preparations or lattice structures, so there is no evidence to stand on. They
  are listed in `not_assessed` and the engineer can choose them.
- **Purchased is never inferred from shape**, for the same reason
  `component_classification` refuses to: a bought part is modelled with the
  same solids as a made one.
- **When the evidence is thin the answer is `unknown`**, with what is needed to
  decide, rather than the nearest-looking family.

The engineer decides
--------------------
`system_*` fields hold the recommendation and are never overwritten.
`record_route_decision` writes the engineer's answer to `user_*` fields beside
them, so the pair *(recommended, chosen)* survives into the estimate snapshot.
Choosing the recommended route again is recorded as an acceptance, not an
override, so the correction count is not poisoned.

Costing boundary
----------------
This module computes no cost, no time and no rate, and imports no costing
module. It only *reports* whether a route can be priced: the existing costing
prices milling, drilling and turning operations, and nothing else. A route with
any other stage — including casting followed by machining — is
`cost_model_not_available`, and `/cost/cad-part` refuses it rather than pricing
the machined half and presenting it as the whole part. The same applies when
the engineer marks the route unknown. A recommendation of `unknown` that nobody
has reviewed does not block costing: the engineer's own operations are priced
exactly as before, and the estimate records that the route was not
established.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, computed_field

from cad_costing_agent.features.model import (
    Confidence,
    Feature,
    FeatureRecognitionResult,
    FeatureType,
)
from cad_costing_agent.operation_suggestions import (
    DRILLING,
    KNOWN_OPERATION_TYPES,
    MILLING,
    TURNING,
    OperationSuggestionResult,
    UserAction,
)

# ------------------------------------------------------------------ vocabulary


class ManufacturingFamily(str, Enum):
    MACHINING = "machining"
    SHEET_METAL = "sheet_metal"
    CASTING = "casting"
    FORGING = "forging"
    FABRICATION_WELDING = "fabrication_welding"
    ADDITIVE = "additive"
    MOULDING = "moulding"
    PURCHASED = "purchased"
    UNKNOWN = "unknown"


FAMILY_LABELS = {
    ManufacturingFamily.MACHINING: "Machining",
    ManufacturingFamily.SHEET_METAL: "Sheet metal",
    ManufacturingFamily.CASTING: "Casting",
    ManufacturingFamily.FORGING: "Forging",
    ManufacturingFamily.FABRICATION_WELDING: "Fabrication / welding",
    ManufacturingFamily.ADDITIVE: "Additive manufacturing",
    ManufacturingFamily.MOULDING: "Moulding",
    ManufacturingFamily.PURCHASED: "Purchased / off-the-shelf",
    ManufacturingFamily.UNKNOWN: "Unknown — insufficient evidence",
}

# Stage id → (label, the existing process type that prices it, or None).
# Only the three process types the existing costing and time estimation know
# carry a cost model; every other stage is named so it can be chosen and
# recorded, and is reported as not costable rather than priced as machining.
STAGES: dict[str, tuple[str, str | None]] = {
    "turning": ("CNC turning", TURNING),
    "milling": ("CNC milling", MILLING),
    "drilling": ("Drilling", DRILLING),
    "sheet_cutting": ("Sheet cutting (laser, plasma, punch or waterjet)", None),
    "bending": ("Bending / press-brake forming", None),
    "casting": ("Casting", None),
    "forging": ("Forging", None),
    "cutting": ("Cutting stock to size", None),
    "welding": ("Welding / joining", None),
    "additive_build": ("Additive build", None),
    "finishing": ("Post-processing and finishing", None),
    "moulding": ("Injection moulding", None),
    "purchase": ("Purchased complete", None),
}

# The routes an engineer can choose. A system recommendation is always one of
# these, so a recommendation and an override are directly comparable by id.
_ROUTE_DEFINITIONS: tuple[tuple[str, str, ManufacturingFamily, tuple[str, ...]], ...] = (
    ("cnc_milling", "CNC milling", ManufacturingFamily.MACHINING, ("milling",)),
    ("cnc_turning", "CNC turning", ManufacturingFamily.MACHINING, ("turning",)),
    ("drilling", "Drilling", ManufacturingFamily.MACHINING, ("drilling",)),
    ("milling_drilling", "Milling + drilling", ManufacturingFamily.MACHINING, ("milling", "drilling")),
    ("turning_drilling", "Turning + drilling", ManufacturingFamily.MACHINING, ("turning", "drilling")),
    ("turning_milling", "Turning + milling", ManufacturingFamily.MACHINING, ("turning", "milling")),
    (
        "turning_milling_drilling",
        "Turning + milling + drilling",
        ManufacturingFamily.MACHINING,
        ("turning", "milling", "drilling"),
    ),
    ("sheet_cutting", "Sheet cutting (flat blank)", ManufacturingFamily.SHEET_METAL, ("sheet_cutting",)),
    (
        "sheet_cutting_bending",
        "Sheet cutting + bending",
        ManufacturingFamily.SHEET_METAL,
        ("sheet_cutting", "bending"),
    ),
    ("casting", "Casting (as cast)", ManufacturingFamily.CASTING, ("casting",)),
    (
        "casting_machining",
        "Casting followed by CNC machining",
        ManufacturingFamily.CASTING,
        ("casting", "milling", "drilling"),
    ),
    ("forging", "Forging (as forged)", ManufacturingFamily.FORGING, ("forging",)),
    (
        "forging_machining",
        "Forging followed by CNC machining",
        ManufacturingFamily.FORGING,
        ("forging", "milling", "drilling"),
    ),
    (
        "cutting_welding",
        "Cutting + welding",
        ManufacturingFamily.FABRICATION_WELDING,
        ("cutting", "welding"),
    ),
    ("additive", "Additive build", ManufacturingFamily.ADDITIVE, ("additive_build",)),
    (
        "additive_finishing",
        "Additive build followed by finishing",
        ManufacturingFamily.ADDITIVE,
        ("additive_build", "finishing"),
    ),
    ("injection_moulding", "Injection moulding", ManufacturingFamily.MOULDING, ("moulding",)),
    ("purchased", "Purchased / off-the-shelf", ManufacturingFamily.PURCHASED, ("purchase",)),
    (
        "unknown",
        "Unknown — needs more information",
        ManufacturingFamily.UNKNOWN,
        (),
    ),
)

UNKNOWN_ROUTE_ID = "unknown"

# Families nothing in this application can find evidence for, and why. Listed
# on every classification so their absence is explained, never implied.
NOT_ASSESSED = (
    "Casting, forging and moulding are not assessed: no recognizer here detects draft angles, "
    "parting lines or near-net-shape surfaces, so geometry never proposes them. Choose one if "
    "it applies.",
    "Fabrication by welding is not assessed: weld preparations and beads are not recognized. "
    "Separate solids are reported, but they are not evidence of a weldment on their own.",
    "Additive manufacturing is not assessed: lattices, support-free overhangs and build "
    "orientation are not recognized.",
    "Purchased is never inferred from shape: a bought part is modelled with the same solids as a "
    "made one.",
)

# --- the rule constants. Each is reported in the evidence that used it.

# Two directions are parallel when |cos| is within this of 1.
PARALLEL_TOLERANCE = 1e-3
# Two axes are the same line when every point of one is this close to the other.
AXIS_OFFSET_TOLERANCE_MM = 0.01
# Plane separations within this (absolute, plus 1% relative) are the same thickness.
THICKNESS_TOLERANCE_MM = 0.01
THICKNESS_RELATIVE_TOLERANCE = 0.01
# A constant wall is "most of the part" when opposite plane pairs at the one
# separation hold at least this share of all planar area.
MIN_PAIRED_PLANE_SHARE = 0.75
# A wall is thin when its thickness is at most this share of the side it bounds
# (the square root of the paired area on one side).
MAX_WALL_THICKNESS_RATIO = 0.10
# The volume must equal thickness × one side of the paired area to this share;
# otherwise the paired faces are not a wall of that thickness.
VOLUME_CONSISTENCY_TOLERANCE = 0.10

# Features that are cut into a solid and that cutting and bending sheet cannot
# produce. They are strong machining evidence and rule a sheet route out.
_STRONG_MACHINING_TYPES = {"pocket", "slot"}
_STRONG_MACHINING_HOLES = {"blind", "counterbore", "blind_countersunk"}
_EXCLUDES_SHEET_CYLINDERS = {"shaft", "bore"}

_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}

# What each recognition representation means for the evidence. Anything but
# exact B-Rep lowers the recommendation to low.
_REPRESENTATION_DETAIL = {
    "brep": "features were read from exact B-Rep surfaces",
    "faceted": "features were reconstructed by fitting polygons, which is weaker evidence",
    "surface_model": (
        "features came from loose trimmed surfaces sewn into a shell, which lose analytic "
        "surface identity — holes and fillets may be missing from the evidence"
    ),
}


# ---------------------------------------------------------------------- models


class RouteStage(BaseModel):
    """One stage of a route, and whether anything here can price it."""

    model_config = ConfigDict(frozen=True)

    stage: str = Field(description="Stage id, e.g. 'milling', 'sheet_cutting', 'casting'.")
    label: str
    process_type: str | None = Field(
        default=None,
        description=(
            "The existing operation process type that prices this stage — 'milling', "
            "'drilling' or 'turning' — or null when none does."
        ),
    )
    cost_model_available: bool = Field(
        description="Whether the existing costing can price this stage. False is never priced."
    )
    basis: list[str] = Field(
        default_factory=list,
        description="Why the system included this stage. Empty for a stage a person chose.",
    )


class ProcessRoute(BaseModel):
    model_config = ConfigDict(frozen=True)

    route_id: str
    label: str
    family: ManufacturingFamily
    stages: list[RouteStage] = Field(default_factory=list)


class ClassificationSignal(BaseModel):
    """One piece of evidence, and whether it held."""

    model_config = ConfigDict(frozen=True)

    signal_id: str
    label: str
    observed: bool | None = Field(
        description="True observed, False checked and absent, None could not be assessed."
    )
    detail: str


class FamilyCandidate(BaseModel):
    """A family the evidence is consistent with, beside the recommended one."""

    model_config = ConfigDict(frozen=True)

    family: ManufacturingFamily
    reason: str


class ProcessClassification(BaseModel):
    """The recommendation, the engineer's answer beside it, and what costing may do."""

    model_config = ConfigDict(frozen=True)

    source_filename: str = ""
    representation: str = Field(description="'brep' or 'faceted', from recognition.")

    # --- the system's recommendation. Never overwritten.
    system_family: ManufacturingFamily
    system_route: ProcessRoute | None = Field(
        default=None, description="Null when the evidence supports no route."
    )
    system_confidence: Confidence
    system_status: str = Field(description="'recommended' or 'insufficient_evidence'.")
    system_reason: str
    system_rule: str = Field(description="Which classification rule produced this.")
    part_form: str = Field(
        description="'rotational', 'prismatic', 'constant_thickness' or 'undetermined'."
    )
    signals: list[ClassificationSignal] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    candidates: list[FamilyCandidate] = Field(
        default_factory=list, description="Other families the same evidence is consistent with."
    )
    not_assessed: list[str] = Field(default_factory=lambda: list(NOT_ASSESSED))
    needs_information: list[str] = Field(
        default_factory=list,
        description="What a person must supply to settle the family or the route.",
    )

    # --- the engineer's answer. Absent until they give one.
    user_action: UserAction = UserAction.PENDING
    user_route: ProcessRoute | None = None
    user_note: str | None = None

    # --- derived. Recomputed from the fields above on every read, so a payload
    # --- that round-trips through the browser cannot assert its own costability.

    @computed_field
    @property
    def route(self) -> ProcessRoute | None:
        """The effective route: the engineer's choice if any, else the recommendation."""
        if self.user_action is UserAction.EDITED:
            return self.user_route
        return self.system_route

    @computed_field
    @property
    def family(self) -> ManufacturingFamily:
        route = self.route
        if route is not None:
            return route.family
        return self.system_family

    @computed_field
    @property
    def decision_status(self) -> str:
        if self.user_action is UserAction.ACCEPTED:
            return "confirmed"
        if self.user_action is UserAction.EDITED:
            if self.user_route is None or self.user_route.family is ManufacturingFamily.UNKNOWN:
                return "marked_unknown"
            return "overridden"
        if self.system_route is None:
            return "insufficient_evidence"
        return "awaiting_review"

    @computed_field
    @property
    def unsupported_stages(self) -> list[str]:
        route = self.route
        if route is None:
            return []
        return [stage.label for stage in route.stages if not stage.cost_model_available]

    @computed_field
    @property
    def costing_status(self) -> str:
        """`machining_supported`, `cost_model_not_available`, `route_unknown` or `route_not_established`."""
        route = self.route
        if route is None or route.family is ManufacturingFamily.UNKNOWN or not route.stages:
            if self.decision_status == "marked_unknown":
                return "route_unknown"
            return "route_not_established"
        if self.unsupported_stages:
            return "cost_model_not_available"
        return "machining_supported"

    @computed_field
    @property
    def blocks_costing(self) -> bool:
        return self.costing_status in ("cost_model_not_available", "route_unknown")

    @computed_field
    @property
    def costing_note(self) -> str:
        status = self.costing_status
        route = self.route
        if status == "cost_model_not_available":
            assert route is not None
            stages = ", ".join(self.unsupported_stages)
            lead = (
                f"The route '{route.label}' includes {stages}, which this application has no cost "
                "model for."
            )
            if self.decision_status == "awaiting_review":
                return (
                    f"{lead} This is the system's recommendation and has not been reviewed: "
                    "confirm it, or override it with the route the part is actually made by. No "
                    "estimate is produced for a route that cannot be priced — pricing only the "
                    "machined stages would present part of the cost as all of it."
                )
            return (
                f"{lead} No estimate is produced: costing it needs a cost model and inputs for "
                "those stages, and pricing only the stages that can be priced would present part "
                "of the cost as all of it."
            )
        if status == "route_unknown":
            return (
                "The route was marked unknown, so no estimate is produced. Choose a route once the "
                "missing information is available."
            )
        if status == "route_not_established":
            return (
                "The geometry does not establish a manufacturing route. Your operations are "
                "costed as entered, and the estimate records that the route was not established."
            )
        if self.decision_status == "awaiting_review":
            return (
                "Costable with the existing machining rates. This is a recommendation that has "
                "not been confirmed."
            )
        return "Costable with the existing machining rates."


# ------------------------------------------------------------------- catalog


def _stage(stage_id: str, basis: Iterable[str] = ()) -> RouteStage:
    label, process_type = STAGES[stage_id]
    return RouteStage(
        stage=stage_id,
        label=label,
        process_type=process_type,
        cost_model_available=process_type is not None and process_type in KNOWN_OPERATION_TYPES,
        basis=list(basis),
    )


ROUTE_CATALOG: dict[str, ProcessRoute] = {
    route_id: ProcessRoute(
        route_id=route_id,
        label=label,
        family=family,
        stages=[_stage(stage) for stage in stages],
    )
    for route_id, label, family, stages in _ROUTE_DEFINITIONS
}

_ROUTE_BY_STAGES = {
    tuple(stage.stage for stage in route.stages): route for route in ROUTE_CATALOG.values()
}


def route_catalog() -> list[ProcessRoute]:
    """Every route an engineer can choose, in catalog order."""
    return list(ROUTE_CATALOG.values())


def _route_for(stages: list[RouteStage]) -> ProcessRoute:
    key = tuple(stage.stage for stage in stages)
    template = _ROUTE_BY_STAGES[key]
    return template.model_copy(update={"stages": stages})


# ------------------------------------------------------------------- geometry


def _xyz(v) -> tuple[float, float, float]:
    return v if isinstance(v, tuple) else (v.x, v.y, v.z)


def _dot(a, b) -> float:
    (ax, ay, az), (bx, by, bz) = _xyz(a), _xyz(b)
    return ax * bx + ay * by + az * bz


def _sub(a, b) -> tuple[float, float, float]:
    return (a.x - b.x, a.y - b.y, a.z - b.z)


def _distance_to_line(point, origin, direction) -> float:
    """Perpendicular distance from a point to the line through origin along direction."""
    dx, dy, dz = _sub(point, origin)
    along = dx * direction.x + dy * direction.y + dz * direction.z
    px, py, pz = dx - along * direction.x, dy - along * direction.y, dz - along * direction.z
    return math.sqrt(px * px + py * py + pz * pz)


def _parallel(a, b) -> bool:
    return abs(abs(_dot(a, b)) - 1.0) <= PARALLEL_TOLERANCE


def _cut_into_solid(feature: Feature) -> bool:
    """Pockets, slots, blind and counterbored holes: strong machining evidence."""
    if feature.feature_type.value in _STRONG_MACHINING_TYPES:
        return True
    return feature.feature_type is FeatureType.HOLE and feature.subtype in _STRONG_MACHINING_HOLES


def _excludes_sheet(feature: Feature) -> bool:
    """Anything cutting and bending a sheet cannot produce."""
    if _cut_into_solid(feature):
        return True
    return (
        feature.feature_type is FeatureType.CYLINDER
        and feature.subtype in _EXCLUDES_SHEET_CYLINDERS
    )


def _ids(features: Iterable[Feature]) -> str:
    return ", ".join(feature.feature_id for feature in features)


def _rotational_form(features: list[Feature]) -> tuple[bool | None, str]:
    """Every external cylinder on one axis, and every planar face perpendicular to it.

    That is what a body of revolution looks like through these recognizers:
    cylinders about the turning axis, and shoulders and end faces square to it.
    A boss on a plate fails on the plate's side faces; a turned shaft with a
    milled flat fails on the flat. Off-axis holes are allowed — cross and
    bolt-circle holes are secondary drilling on a turned part.
    """
    shafts = [
        f
        for f in features
        if f.feature_type is FeatureType.CYLINDER
        and f.subtype == "shaft"
        and f.axis is not None
        and f.location is not None
    ]
    if not shafts:
        return None, "no external cylindrical surface was recognized, so no turning axis exists to test"

    reference = shafts[0]
    for shaft in shafts[1:]:
        if not _parallel(shaft.axis, reference.axis) or (
            _distance_to_line(shaft.location, reference.location, reference.axis)
            > AXIS_OFFSET_TOLERANCE_MM
        ):
            return False, (
                f"external cylinders {reference.feature_id} and {shaft.feature_id} do not share "
                "one axis, so the part is not a single body of revolution"
            )

    pockets = [f for f in features if f.feature_type in (FeatureType.POCKET, FeatureType.SLOT)]
    if pockets:
        return False, (
            f"pocket/slot feature(s) {_ids(pockets)} are not surfaces of revolution about the "
            "cylinder axis"
        )

    planes = [f for f in features if f.feature_type is FeatureType.PLANE and f.axis is not None]
    off_axis = [p for p in planes if not _parallel(p.axis, reference.axis)]
    if off_axis:
        shown = _ids(off_axis[:6]) + (" …" if len(off_axis) > 6 else "")
        return False, (
            f"{len(off_axis)} planar face(s) ({shown}) are not perpendicular to the axis of "
            f"{reference.feature_id}, which a body of revolution does not have"
        )

    return True, (
        f"{len(shafts)} external cylinder(s) ({_ids(shafts)}) share one axis within "
        f"{AXIS_OFFSET_TOLERANCE_MM} mm, and all {len(planes)} planar face(s) are perpendicular "
        "to it"
    )


def _constant_thickness(
    features: list[Feature], volume_mm3: float
) -> tuple[bool | None, str, float | None, int]:
    """Most planar area in opposite pairs at one separation, matching the volume.

    For each planar face, the nearest opposite-facing plane behind it (inside
    the material) gives a wall thickness. If one thickness accounts for most
    of the planar area, the part is thin compared to that area, and the volume
    equals the thickness times one side of the paired area, the part is a
    constant-thickness wall — the shape cutting and bending sheet produces, and
    equally the shape of a machined plate.

    Returns (observed, detail, thickness, distinct wall directions).
    """
    planes = [
        f
        for f in features
        if f.feature_type is FeatureType.PLANE
        and f.axis is not None
        and f.location is not None
        and f.dimensions.get("area_mm2", 0.0) > 0.0
    ]
    if len(planes) < 2:
        return None, "fewer than two planar faces were recognized, so no wall can be measured", None, 0
    if volume_mm3 <= 0.0:
        return None, "the volume was not measured, so a wall thickness cannot be checked against it", None, 0

    nearest: list[tuple[Feature, float]] = []
    for plane in planes:
        best: float | None = None
        for other in planes:
            if other is plane or other.solid_index != plane.solid_index:
                continue
            if _dot(plane.axis, other.axis) > -1.0 + PARALLEL_TOLERANCE:
                continue
            separation = -_dot(_sub(other.location, plane.location), plane.axis)
            if separation <= THICKNESS_TOLERANCE_MM:
                continue
            if best is None or separation < best:
                best = separation
        if best is not None:
            nearest.append((plane, best))

    if not nearest:
        return False, "no planar face has an opposite parallel face, so there is no constant wall", None, 0

    total_area = sum(p.dimensions["area_mm2"] for p in planes)

    def same(a: float, b: float) -> bool:
        return abs(a - b) <= THICKNESS_TOLERANCE_MM + THICKNESS_RELATIVE_TOLERANCE * min(a, b)

    # The separation with the most planar area behind it. Ties go to the thinner.
    best_thickness, best_area = None, 0.0
    for _, candidate in sorted(nearest, key=lambda item: item[1]):
        area = sum(p.dimensions["area_mm2"] for p, s in nearest if same(s, candidate))
        if area > best_area + 1e-9:
            best_thickness, best_area = candidate, area
    assert best_thickness is not None
    thickness = round(best_thickness, 6)
    paired = [p for p, s in nearest if same(s, best_thickness)]
    share = best_area / total_area if total_area > 0 else 0.0

    if share < MIN_PAIRED_PLANE_SHARE:
        return False, (
            f"opposite planar faces {thickness:g} mm apart hold {share:.0%} of the planar area, "
            f"below the {MIN_PAIRED_PLANE_SHARE:.0%} at which the part is treated as one "
            "constant wall"
        ), thickness, 0

    side_area = best_area / 2.0
    ratio = thickness / math.sqrt(side_area)
    if ratio > MAX_WALL_THICKNESS_RATIO:
        return False, (
            f"a {thickness:g} mm wall holds {share:.0%} of the planar area, but it is "
            f"{ratio:.0%} of the side it bounds, above the {MAX_WALL_THICKNESS_RATIO:.0%} at "
            "which a wall is treated as thin — this reads as a block or thick plate"
        ), thickness, 0

    expected = thickness * side_area
    mismatch = abs(volume_mm3 - expected) / volume_mm3
    if mismatch > VOLUME_CONSISTENCY_TOLERANCE:
        return False, (
            f"a {thickness:g} mm wall would hold {expected:.1f} mm³, but the part measures "
            f"{volume_mm3:.1f} mm³ ({mismatch:.0%} apart, above the "
            f"{VOLUME_CONSISTENCY_TOLERANCE:.0%} tolerance), so the paired faces are not one wall"
        ), thickness, 0

    directions: list = []
    for plane in paired:
        if not any(_parallel(plane.axis, seen) for seen in directions):
            directions.append(plane.axis)

    return True, (
        f"opposite planar faces {thickness:g} mm apart hold {share:.0%} of the planar area, the "
        f"wall is {ratio:.1%} of the side it bounds, and the volume ({volume_mm3:.1f} mm³) is "
        f"within {mismatch:.1%} of {thickness:g} mm × the paired area; the wall runs in "
        f"{len(directions)} direction(s)"
    ), thickness, len(directions)


# ----------------------------------------------------------------- the rules


def _cap(*levels: Confidence) -> Confidence:
    return min(levels, key=lambda level: _CONFIDENCE_RANK[level])


def _suggested_processes(suggestions: OperationSuggestionResult) -> dict[str, list[str]]:
    """Process type → the feature ids whose suggestions proposed it, in order."""
    found: dict[str, list[str]] = {}
    for suggestion in suggestions.suggestions:
        found.setdefault(suggestion.operation_type, []).append(suggestion.feature_id)
    return found


def _basis(process: str, feature_ids: list[str]) -> list[str]:
    shown = ", ".join(feature_ids[:8]) + (" …" if len(feature_ids) > 8 else "")
    return [f"{len(feature_ids)} {process} suggestion(s), from feature(s) {shown}"]


def classify_process(
    recognition: FeatureRecognitionResult,
    suggestions: OperationSuggestionResult,
) -> ProcessClassification:
    """Recommend a manufacturing family and route from recognized features. Deterministic.

    Reads only what recognition and the operation suggestions already
    established — no second parse, no thresholds on raw geometry beyond the
    three signals described in the module docstring.
    """
    features = list(recognition.features)
    representation = recognition.representation
    volume = sum(solid.volume_mm3 for solid in recognition.solids)

    strong = [f for f in features if _cut_into_solid(f)]
    sheet_excluders = [f for f in features if _excludes_sheet(f)]
    through_holes = [
        f for f in features if f.feature_type is FeatureType.HOLE and f.subtype is not None
        and f.subtype.startswith("through")
    ]
    processes = _suggested_processes(suggestions)

    rotational, rotational_detail = _rotational_form(features)
    thin, thin_detail, thickness, wall_directions = _constant_thickness(features, volume)

    signals = [
        ClassificationSignal(
            signal_id="exact_geometry",
            label="Exact geometry",
            observed=representation == "brep",
            detail=_REPRESENTATION_DETAIL.get(
                representation,
                f"features came from a '{representation}' representation, not exact B-Rep "
                "surfaces, which is weaker evidence",
            ),
        ),
        ClassificationSignal(
            signal_id="machining_features",
            label="Features cut into a solid",
            observed=bool(strong),
            detail=(
                f"{len(strong)} pocket, slot, blind or counterbored hole feature(s): {_ids(strong)}"
                if strong
                else "no pocket, slot, blind hole or counterbore was recognized"
            ),
        ),
        ClassificationSignal(
            signal_id="rotational_form",
            label="Rotational form",
            observed=rotational,
            detail=rotational_detail,
        ),
        ClassificationSignal(
            signal_id="constant_thickness",
            label="Constant wall thickness",
            observed=thin,
            detail=thin_detail,
        ),
        ClassificationSignal(
            signal_id="single_solid",
            label="One solid",
            observed=recognition.solid_count == 1,
            detail=f"the file holds {recognition.solid_count} solid(s) for this part",
        ),
        ClassificationSignal(
            signal_id="fully_recognized",
            label="Every face recognized",
            observed=recognition.unclassified_face_count == 0,
            detail=(
                "every face was claimed by a recognizer"
                if recognition.unclassified_face_count == 0
                else f"{recognition.unclassified_face_count} face(s) were not recognized as any "
                "feature (freeform or unsupported surfaces), so the evidence is incomplete"
            ),
        ),
    ]

    # The ceiling every recommendation starts from, and what lowers it.
    ceiling = Confidence.MEDIUM
    weakening: list[str] = []
    if representation != "brep":
        ceiling = Confidence.LOW
        weakening.append(_REPRESENTATION_DETAIL.get(representation, "features were not read from exact B-Rep surfaces"))
    if recognition.solid_count > 1:
        ceiling = Confidence.LOW
        weakening.append(f"the file holds {recognition.solid_count} separate solids")
    if recognition.unclassified_face_count:
        ceiling = Confidence.LOW
        weakening.append(f"{recognition.unclassified_face_count} face(s) were not recognized")

    needs: list[str] = []
    candidates: list[FamilyCandidate] = []
    evidence: list[str] = [
        "Family confidence is capped at medium: geometry cannot exclude a near-net-shape blank "
        "(a casting or forging) that is then machined, and it does not record the material, "
        "tolerances, quantity or machines that usually decide the process.",
        *(f"Confidence lowered: {reason}." for reason in weakening),
    ]

    if recognition.solid_count > 1:
        candidates.append(
            FamilyCandidate(
                family=ManufacturingFamily.FABRICATION_WELDING,
                reason=(
                    f"{recognition.solid_count} solids under one part may be a weldment — or a "
                    "multi-body modelling choice. The geometry does not say which."
                ),
            )
        )
        needs.append(
            f"whether the {recognition.solid_count} solids are joined (welded or bonded) or are "
            "separate parts"
        )

    # --- no features at all: nothing to stand on.
    if not features:
        evidence.append("No features were recognized, so no manufacturing evidence exists.")
        evidence.extend(recognition.warnings)
        return _unknown(
            recognition,
            signals,
            evidence,
            candidates,
            needs,
            rule="no_features_unclassified",
            reason="no features were recognized in this model, so no family can be recommended",
            part_form="undetermined",
        )

    # --- constant thickness with nothing cut into it: a sheet candidate.
    if thin and not sheet_excluders:
        route_stages = ["sheet_cutting"] + (["bending"] if wall_directions > 1 else [])
        basis = {
            "sheet_cutting": [thin_detail]
            + ([f"{len(through_holes)} through hole(s) cut in the wall"] if through_holes else []),
            "bending": [
                f"the {thickness:g} mm wall runs in {wall_directions} directions, which a flat "
                "blank bent along lines produces"
            ],
        }
        stages = [_stage(stage, basis[stage]) for stage in route_stages]
        candidates.append(
            FamilyCandidate(
                family=ManufacturingFamily.MACHINING,
                reason=(
                    "a constant-thickness part is equally a plate or bar that was milled and "
                    "drilled; the geometry cannot tell the two apart"
                ),
            )
        )
        if wall_directions > 1:
            candidates.append(
                FamilyCandidate(
                    family=ManufacturingFamily.FABRICATION_WELDING,
                    reason="walls in several directions may also be separate plates welded together",
                )
            )
        return ProcessClassification(
            source_filename=recognition.filename,
            representation=representation,
            system_family=ManufacturingFamily.SHEET_METAL,
            system_route=_route_for(stages),
            # Low however clean: a sheet route is the one most often wrong for
            # a part that is really a machined plate, and the cost of the two
            # differs entirely.
            system_confidence=Confidence.LOW,
            system_status="recommended",
            system_reason=(
                f"a constant {thickness:g} mm wall with nothing cut into it is the shape sheet "
                "cutting" + (" and bending" if wall_directions > 1 else "") + " produces. This "
                "is a recommendation, not a finding: the same shape can be machined from plate"
            ),
            system_rule="constant_thickness_to_sheet_metal",
            part_form="constant_thickness",
            signals=signals,
            evidence=evidence
            + ["Confidence is low: a sheet route and a machined plate cannot be told apart by shape."],
            candidates=candidates,
            needs_information=needs
            + [
                "whether the part is cut from sheet or machined from plate or bar",
                "sheet material and thickness specification",
            ],
        )

    machining_processes = [p for p in (TURNING, MILLING, DRILLING) if p in processes]
    only_incidental = not strong and not through_holes and rotational is not True and not any(
        f.feature_type is FeatureType.CYLINDER for f in features
    )

    # --- nothing but faces and blends: stock, a cast blank, or a machined part.
    if not machining_processes or only_incidental:
        evidence.append(
            "Only planar faces and blends were recognized. A block like this is equally raw "
            "stock, a cast or forged blank, or a machined part."
        )
        evidence.extend(recognition.warnings)
        candidates.insert(
            0,
            FamilyCandidate(
                family=ManufacturingFamily.MACHINING,
                reason="flat faces may be milled, but nothing shows material was removed",
            ),
        )
        return _unknown(
            recognition,
            signals,
            evidence,
            candidates,
            needs,
            rule="insufficient_features_unclassified",
            reason=(
                "only flat faces and blends were recognized, which every family can produce, so "
                "no family is recommended"
            ),
            part_form="undetermined",
        )

    # --- machining. Decide the route's stages from the suggestions.
    stage_ids: list[str] = []
    basis: dict[str, list[str]] = {}
    if rotational is True and TURNING in processes:
        stage_ids.append("turning")
        basis["turning"] = [rotational_detail, *_basis(TURNING, processes[TURNING])]
        part_form = "rotational"
    else:
        part_form = "prismatic"
    if MILLING in processes and part_form == "rotational":
        # Rotational form means every planar face is square to the turning axis
        # and nothing is pocketed, so the faces and blends the per-feature rules
        # proposed milling for are faced and formed on the lathe.
        basis["turning"].append(
            f"faces and blends {', '.join(processes[MILLING])} were suggested for milling feature "
            "by feature; on a body of revolution they are square to the axis, so at route level "
            "they are faced and formed in the turning stage"
        )
    elif MILLING in processes:
        stage_ids.append("milling")
        basis["milling"] = _basis(MILLING, processes[MILLING])
    if TURNING in processes and rotational is not True:
        # Per-feature, the shaft suggestion stands. At route level, a boss on a
        # prismatic part is milled; turning it would need the part on a lathe.
        if "milling" not in stage_ids:
            stage_ids.append("milling")
            basis["milling"] = []
        basis["milling"].append(
            f"external cylinder(s) {', '.join(processes[TURNING])} were suggested for turning "
            "feature by feature, but the part's form is not rotational ("
            + rotational_detail
            + "), so at route level they are treated as milled bosses"
        )
    if DRILLING in processes:
        stage_ids.append("drilling")
        basis["drilling"] = _basis(DRILLING, processes[DRILLING])

    stages = [_stage(stage, basis.get(stage, [])) for stage in stage_ids]
    route = _route_for(stages)

    declined_holes = [d for d in suggestions.declined if d.feature_type == FeatureType.HOLE.value]
    if declined_holes:
        evidence.append(
            f"{len(declined_holes)} hole(s) ({', '.join(d.feature_id for d in declined_holes)}) "
            "were recognized but given no operation, because their end conditions could not be "
            "established. The route does not include drilling them; add it if they are drilled."
        )
        needs.append(
            "how the hole(s) the suggestions declined are produced: "
            + ", ".join(d.feature_id for d in declined_holes)
        )

    confidence = _cap(ceiling, Confidence.MEDIUM if (strong or rotational) else Confidence.LOW)
    if not strong and rotational is not True:
        evidence.append(
            "Confidence is low: through holes and flat faces are also what a cut or cast blank "
            "carries; nothing here was cut into the solid."
        )

    if thin:
        evidence.append(
            f"A constant wall was also found ({thin_detail}), but {_ids(sheet_excluders)} cannot "
            "be produced by cutting and bending sheet, so machining leads."
        )
        candidates.append(
            FamilyCandidate(
                family=ManufacturingFamily.SHEET_METAL,
                reason="the constant wall could be sheet with the cut features added afterwards",
            )
        )
    candidates.append(
        FamilyCandidate(
            family=ManufacturingFamily.CASTING,
            reason=(
                "a cast or forged blank machined afterwards carries the same machined features; "
                "quantity and material usually decide"
            ),
        )
    )
    if part_form == "rotational" and not strong and not through_holes and set(processes) == {TURNING}:
        candidates.append(
            FamilyCandidate(
                family=ManufacturingFamily.PURCHASED,
                reason="a plain cylinder may be bar stock used as bought",
            )
        )

    reason = (
        "the part is a single body of revolution with turned surfaces"
        if part_form == "rotational"
        else "machinable features were recognized on a prismatic part"
    )
    if strong:
        reason += f", including feature(s) cut into the solid ({_ids(strong)})"
    reason += f"; the suggested operations make the route '{route.label}'"

    return ProcessClassification(
        source_filename=recognition.filename,
        representation=representation,
        system_family=ManufacturingFamily.MACHINING,
        system_route=route,
        system_confidence=confidence,
        system_status="recommended",
        system_reason=reason,
        system_rule=f"{part_form}_features_to_machining",
        part_form=part_form,
        signals=signals,
        evidence=evidence,
        candidates=candidates,
        needs_information=needs
        + [
            "the stock form — bar, plate, or a cast or forged blank",
            "production quantity, which can favour a near-net-shape process",
        ],
    )


def _unknown(
    recognition: FeatureRecognitionResult,
    signals: list[ClassificationSignal],
    evidence: list[str],
    candidates: list[FamilyCandidate],
    needs: list[str],
    *,
    rule: str,
    reason: str,
    part_form: str,
) -> ProcessClassification:
    return ProcessClassification(
        source_filename=recognition.filename,
        representation=recognition.representation,
        system_family=ManufacturingFamily.UNKNOWN,
        system_route=None,
        system_confidence=Confidence.LOW,
        system_status="insufficient_evidence",
        system_reason=reason,
        system_rule=rule,
        part_form=part_form,
        signals=signals,
        evidence=evidence,
        candidates=candidates,
        needs_information=needs
        + [
            "the manufacturing process — the geometry does not establish one",
            "the stock form and material",
        ],
    )


# -------------------------------------------------------------- the decision


def record_route_decision(
    classification: ProcessClassification,
    action: UserAction,
    *,
    route_id: str | None = None,
    note: str | None = None,
) -> ProcessClassification:
    """Return a copy carrying the engineer's answer, with the recommendation intact.

    - `accepted` confirms the recommended route. Refused when nothing was
      recommended — there is nothing to accept, and accepting "unknown" would
      read as a decision nobody made.
    - `edited` chooses a catalog route instead. Choosing the recommended route
      is recorded as an acceptance. Choosing `unknown` marks the route as
      needing more information, which holds costing until it is answered.
    - `pending` withdraws the answer.
    """
    clean_note = note.strip() if note and note.strip() else None

    if action is UserAction.PENDING:
        if route_id is not None:
            raise ValueError("A route may only be supplied with 'edited'.")
        return classification.model_copy(
            update={"user_action": UserAction.PENDING, "user_route": None, "user_note": clean_note}
        )

    if action is UserAction.ACCEPTED:
        if route_id is not None:
            raise ValueError("A route may only be supplied with 'edited'.")
        if classification.system_route is None:
            raise ValueError(
                "Nothing was recommended, so there is nothing to accept. Choose a route, or "
                f"choose '{UNKNOWN_ROUTE_ID}' to record that it is not yet known."
            )
        return classification.model_copy(
            update={"user_action": UserAction.ACCEPTED, "user_route": None, "user_note": clean_note}
        )

    if action is UserAction.EDITED:
        if not route_id:
            raise ValueError("An overridden route needs the route the engineer chose.")
        if route_id not in ROUTE_CATALOG:
            raise ValueError(
                f"Unknown route '{route_id}'. Choose one of: {', '.join(ROUTE_CATALOG)}."
            )
        system = classification.system_route
        if system is not None and system.route_id == route_id:
            return classification.model_copy(
                update={"user_action": UserAction.ACCEPTED, "user_route": None, "user_note": clean_note}
            )
        return classification.model_copy(
            update={
                "user_action": UserAction.EDITED,
                "user_route": ROUTE_CATALOG[route_id],
                "user_note": clean_note,
            }
        )

    raise ValueError(
        f"'{action.value}' is not a route decision. Use 'accepted', 'edited' (with "
        f"'{UNKNOWN_ROUTE_ID}' to mark it unknown) or 'pending'."
    )


def plan_route_differences(
    classification: ProcessClassification, plan_processes: Collection[str]
) -> list[str]:
    """Where the operations being costed disagree with the effective route.

    Reported, never corrected: the plan and the route are both the engineer's,
    and which one is wrong is theirs to say.
    """
    route = classification.route
    if route is None or not route.stages:
        return []
    route_processes = {stage.process_type for stage in route.stages if stage.process_type}
    used = {process.strip().lower() for process in plan_processes}
    notes = [
        f"The plan costs {process} operations, which the route '{route.label}' does not include."
        for process in sorted(used - route_processes)
    ]
    notes.extend(
        f"The route '{route.label}' includes {process}, but the plan costs no {process} operation."
        for process in sorted(route_processes - used)
    )
    return notes


__all__ = [
    "FAMILY_LABELS",
    "NOT_ASSESSED",
    "ROUTE_CATALOG",
    "STAGES",
    "UNKNOWN_ROUTE_ID",
    "ClassificationSignal",
    "FamilyCandidate",
    "ManufacturingFamily",
    "ProcessClassification",
    "ProcessRoute",
    "RouteStage",
    "classify_process",
    "plan_route_differences",
    "record_route_decision",
    "route_catalog",
]
