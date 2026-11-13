"""Sheet-metal measurement: is this a constant-thickness sheet, where are its bends,
and what does it look like flat?

    B-Rep → topology scan → **sheet test → bends → flat pattern** → (times, later)

What this measures
------------------
A part made by cutting a flat blank and bending it on a press brake has a
recognisable B-Rep, and every rule below is checked on the exact geometry:

- **Two skins at one thickness t.** Every planar face of either skin has an
  antiparallel planar face directly behind it, at exactly t. Every bend is a
  pair of coaxial cylinders, the concave inner one of radius r and the convex
  outer one of radius r + t, sweeping the same angle over the same length.
- **Everything else runs straight through the thickness.** An edge face is
  perpendicular to the skins it joins and touches both of them; a hole is a
  cylinder square to its flange, exactly t deep. A face that lies inside the
  thickness — a pocket floor, a step, a counterbore — is not something cutting
  and bending sheet produces.
- **The volume agrees.** For such a solid the volume is exactly
  t x (planar area of one skin + Σ θ·L·(r + t/2)), so any variation in thickness
  shows up as a volume that does not match. Checked on every part.
- **It is thin.** t is at most `MAX_THICKNESS_RATIO` of the square root of one
  skin's area — the same 10 % the route classifier uses (a test holds the two
  equal). A 20 mm block passes every other test as a "plate" and fails this one.

Features recognition already found that sheet cannot make (pockets, slots,
blind or counterbored holes, bosses and bores) are handed in by the caller from
the classifier's own rule (`process_classification.sheet_excluding_features`),
so this module and the route recommendation cannot disagree about them.

The flat pattern
----------------
Unfolding uses the standard bend allowance:

    BA = θ · (r + K · t)        θ the bend's sweep in radians, r its inside radius

**K is an input.** The K-factor places the neutral axis inside the thickness; it
depends on the material, the tooling and r/t, and no geometry says what it is.
It is a request field the engineer enters, recorded on the result, and there is
no default: without it every K-independent figure is still reported (thickness,
bends, contours, pierces, the cut length of every flange edge) and the flat size
is `null`, with `needs_input: ["k_factor"]` and the formula it would use.

Each flange is placed in one plane by a rigid map, starting from the largest
flange; across each bend the next flange starts BA beyond the tangent line, so
the bend's strip is BA wide. The cut outline is every edge of one skin that
borders an edge face: flange edges keep their length, and each end of a bend
unfolds to a straight run of length BA. Its closed loops are the cut contours;
each one is started with one pierce.

As a self-check, the area enclosed by the unfolded outline (outer loop minus
inner loops, by Green's theorem over the exact lines and arcs) must equal the
skin's flange area plus BA x L for every bend. If it does not, the flanges
overlap when flat or the outline did not close, and no flat pattern is claimed.

Scope, honestly
---------------
Analytic B-Rep only. Straight bends between planar flanges, in a tree: every
bend joins exactly two flanges, full width, with square ends. Hems (180°),
conical bends, a bend running into another bend, a closed ring of bends, a
sharp corner with no bend radius, a cut-out across a bend and outline edges
that are not lines or arcs are reported as unsupported with the reason — never
approximated. Whether flanges collide with each other when folded is not
checked, and no nesting, mass or material cost is derived from the blank here:
`sheet_nesting` estimates a grid nest from the flat pattern, and `raw_stock`
proposes the sheet share for a person to confirm.

This module imports only the feature layer's own topology descriptors and no
OpenCASCADE: the shape is scanned once by `features.topology`.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from cad_costing_agent.features.topology import (
    EdgeDescriptor,
    FaceDescriptor,
    TopologyModel,
    Vec3,
    canonical_direction,
    is_parallel,
    is_perpendicular,
    point_on_axis_nearest_origin,
)

METHOD = "sheet_unfold_v1"
BEND_ALLOWANCE_FORMULA = "BA = θ × (r + K × t)"

# Two lengths within this are equal. Analytic B-Rep keeps exact surfaces, so
# this is tight; it is a micrometre.
LENGTH_TOLERANCE_MM = 1e-3
# Two angular sweeps within this are equal (about 0.0006°).
ANGLE_TOLERANCE_RAD = 1e-5
# The volume must equal t x the skin's area (with the bends' exact share) to
# this fraction. A genuine sheet solid matches to integration noise.
VOLUME_RELATIVE_TOLERANCE = 1e-3
# A sheet is thin: t at most this share of the square root of one skin's area.
# The same 10 % as `process_classification.MAX_WALL_THICKNESS_RATIO`.
MAX_THICKNESS_RATIO = 0.10
# A bend sweeping this close to 180° or more is a hem or a curl.
MAX_BEND_SWEEP_RAD = math.pi - 1e-3
# The unfolded outline's enclosed area must equal the expected flat area to this.
AREA_RELATIVE_TOLERANCE = 1e-5
# The K-factor's physical range: the neutral axis lies between the inside
# surface and the middle of the sheet.
K_FACTOR_MIN_EXCLUSIVE = 0.0
K_FACTOR_MAX = 0.5

PIERCE_RULE = (
    "one pierce per closed cut contour — each hole or cut-out and the outline. A cut started "
    "from the sheet's edge would save the outline's pierce; that is not assumed."
)

SHEET = "sheet"
NOT_SHEET = "not_sheet"
UNSUPPORTED = "unsupported"


# ---------------------------------------------------------------------- models


class SheetBend(BaseModel):
    """One straight bend, as measured. Nothing here is assumed except K."""

    model_config = ConfigDict(frozen=True)

    bend_id: str
    angle_deg: float = Field(description="The angle bent through: the cylinders' sweep.")
    inner_radius_mm: float
    outer_radius_mm: float
    length_mm: float = Field(description="Along the bend line: the cylinders' axial extent.")
    axis_direction: tuple[float, float, float]
    inner_face: int
    outer_face: int
    flanges: tuple[str, str] = Field(description="The two flanges this bend joins.")
    bend_allowance_mm: float | None = Field(
        default=None, description="θ × (r + K × t); null until a K-factor is entered."
    )
    bend_allowance_formula: str = Field(description="This bend's numbers in the BA formula.")


class SheetFlange(BaseModel):
    model_config = ConfigDict(frozen=True)

    flange_id: str
    faces: list[int] = Field(description="Its faces on the skin the outline is read from.")
    opposite_faces: list[int] = Field(description="The faces of the other skin, t behind them.")
    area_mm2: float = Field(description="Flat area of this flange, holes excluded.")
    normal: tuple[float, float, float]


class FlatPattern(BaseModel):
    """The unfolded blank, for the K-factor entered."""

    model_config = ConfigDict(frozen=True)

    k_factor: float
    k_factor_source: Literal["entered"] = "entered"
    bend_allowance_formula: str = BEND_ALLOWANCE_FORMULA
    rectangle_length_mm: float = Field(description="Bounding rectangle along `rectangle_orientation`.")
    rectangle_width_mm: float = Field(description="Bounding rectangle across it.")
    rectangle_area_mm2: float
    rectangle_orientation: str
    outline_area_mm2: float = Field(description="Area inside the outer contour.")
    net_area_mm2: float = Field(description="Outline area minus every inner contour.")
    cut_perimeter_mm: float = Field(description="Outer contour plus every inner contour.")
    outer_perimeter_mm: float
    inner_perimeter_mm: float
    contour_count: int
    pierce_count: int
    self_check: str


class SheetMetalAnalysis(BaseModel):
    """Whether the part is a sheet, its bends, and its flat pattern if K is known."""

    model_config = ConfigDict(frozen=True)

    filename: str = ""
    source_format: str = "step"
    representation: str = "brep"
    method: str = METHOD
    units: str = "mm"
    status: Literal["sheet", "not_sheet", "unsupported"]
    reasons: list[str] = Field(
        default_factory=list,
        description="Why the part is not a sheet, or why its flat pattern cannot be worked out here.",
    )
    thickness_mm: float | None = None
    bends: list[SheetBend] = Field(default_factory=list)
    flanges: list[SheetFlange] = Field(default_factory=list)
    contour_count: int | None = Field(
        default=None, description="Closed cut contours: the outline and every hole or cut-out."
    )
    pierce_count: int | None = None
    pierce_rule: str = PIERCE_RULE
    flange_edge_cut_length_mm: float | None = Field(
        default=None,
        description=(
            "Cut length of every flange edge, which does not depend on K. The cut perimeter is "
            "this plus one bend allowance for each bend end on the outline."
        ),
    )
    bend_end_count: int | None = None
    k_factor: float | None = None
    flat_pattern: FlatPattern | None = None
    needs_input: list[str] = Field(default_factory=list)
    flat_pattern_note: str | None = None
    checks: list[str] = Field(default_factory=list, description="What was checked, with its numbers.")
    notes: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def is_sheet(self) -> bool:
        return self.status == SHEET

    @computed_field
    @property
    def bend_count(self) -> int:
        return len(self.bends)


SCOPE_NOTES = (
    "Measured from exact B-Rep surfaces. Straight bends between planar flanges only; hems, "
    "conical bends, closed rings of bends and cut-outs across a bend are reported as unsupported.",
    "Whether the flanges collide when folded is not checked. No mass or material cost is derived "
    "from the blank here; a sheet share is proposed from it separately, for you to confirm.",
)


def _round(value: float) -> float:
    return round(value, 6)


def _tuple(vector: Vec3) -> tuple[float, float, float]:
    return (_round(vector.x) + 0.0, _round(vector.y) + 0.0, _round(vector.z) + 0.0)


def validate_k_factor(k_factor: float | None) -> float | None:
    """None stays None. Anything else must be a number in (0, 0.5]."""
    if k_factor is None:
        return None
    if isinstance(k_factor, bool) or not isinstance(k_factor, (int, float)) or not math.isfinite(k_factor):
        raise ValueError(f"The K-factor must be a number; got {k_factor!r}.")
    if not K_FACTOR_MIN_EXCLUSIVE < k_factor <= K_FACTOR_MAX:
        raise ValueError(
            f"The K-factor must be above {K_FACTOR_MIN_EXCLUSIVE:g} and at most {K_FACTOR_MAX:g} — "
            f"the neutral axis lies between the inside surface and the middle of the sheet; got "
            f"{k_factor:g}."
        )
    return float(k_factor)


# --------------------------------------------------------------- small geometry


def _bbox_overlap(a: FaceDescriptor, b: FaceDescriptor, margin: float) -> bool:
    return (
        a.bbox_min.x - margin <= b.bbox_max.x
        and b.bbox_min.x - margin <= a.bbox_max.x
        and a.bbox_min.y - margin <= b.bbox_max.y
        and b.bbox_min.y - margin <= a.bbox_max.y
        and a.bbox_min.z - margin <= b.bbox_max.z
        and b.bbox_min.z - margin <= a.bbox_max.z
    )


def _behind_distance(face: FaceDescriptor, other: FaceDescriptor) -> float | None:
    """How far behind `face` (inside its material) the antiparallel plane `other` lies."""
    if face.normal is None or other.normal is None or face.origin is None or other.origin is None:
        return None
    if face.normal.dot(other.normal) > -1.0 + 1e-6:
        return None
    return (face.origin - other.origin).dot(face.normal)


def _axial_range(face: FaceDescriptor, direction: Vec3) -> tuple[float, float]:
    """A cylinder's axial extent in absolute coordinates along `direction`."""
    sign = 1.0 if face.axis_direction.dot(direction) > 0 else -1.0
    base = face.axis_location.dot(direction)
    ends = sorted((base + sign * face.axial_start, base + sign * face.axial_end))
    return ends[0], ends[1]


def _same_axis(a: FaceDescriptor, b: FaceDescriptor) -> bool:
    if not is_parallel(a.axis_direction, b.axis_direction):
        return False
    direction = canonical_direction(a.axis_direction)
    pa = point_on_axis_nearest_origin(a.axis_location, direction)
    pb = point_on_axis_nearest_origin(b.axis_location, direction)
    return (pa - pb).length() <= LENGTH_TOLERANCE_MM


class _Points:
    """Clusters 3D points within the length tolerance into vertex ids."""

    def __init__(self) -> None:
        self.points: list[Vec3] = []

    def id(self, point: Vec3) -> int:
        for index, known in enumerate(self.points):
            if (known - point).length() <= LENGTH_TOLERANCE_MM:
                return index
        self.points.append(point)
        return len(self.points) - 1


class _Map:
    """A rigid map from one flange's plane into the flat pattern's 2D plane."""

    def __init__(self, origin: Vec3, image: tuple[float, float], b1: Vec3, c1, b2: Vec3, c2) -> None:
        self.origin, self.image = origin, image
        self.b1, self.c1, self.b2, self.c2 = b1, c1, b2, c2

    def linear(self, vector: Vec3) -> tuple[float, float]:
        s, w = vector.dot(self.b1), vector.dot(self.b2)
        return (self.c1[0] * s + self.c2[0] * w, self.c1[1] * s + self.c2[1] * w)

    def __call__(self, point: Vec3) -> tuple[float, float]:
        dx, dy = self.linear(point - self.origin)
        return (self.image[0] + dx, self.image[1] + dy)


def _angle(centre, point) -> float:
    return math.atan2(point[1] - centre[1], point[0] - centre[0])


def _arc_sweep(centre, start, middle, end, full: bool) -> tuple[float, float]:
    """(start angle, signed sweep) of the arc from start through middle to end."""
    a_start, a_mid, a_end = _angle(centre, start), _angle(centre, middle), _angle(centre, end)
    if full:
        ccw_mid = (a_mid - a_start) % (2 * math.pi)
        return a_start, (2 * math.pi if ccw_mid <= math.pi else -2 * math.pi)
    ccw = (a_end - a_start) % (2 * math.pi)
    if (a_mid - a_start) % (2 * math.pi) < ccw:
        return a_start, ccw
    return a_start, ccw - 2 * math.pi


# ------------------------------------------------------------------ the analysis


def _result(status: str, reasons: list[str], checks: list[str], **values) -> SheetMetalAnalysis:
    return SheetMetalAnalysis(status=status, reasons=reasons, checks=checks, notes=list(SCOPE_NOTES), **values)


def analyse_sheet_metal(
    topology: TopologyModel,
    *,
    k_factor: float | None = None,
    sheet_excluders: list[tuple[str, str]] | None = None,
    filename: str = "",
    source_format: str = "step",
) -> SheetMetalAnalysis:
    """Decide whether the scanned solid is a constant-thickness sheet, and unfold it.

    `sheet_excluders` is (feature id, description) for every recognized feature
    the route classifier's own rule says sheet cannot produce. `k_factor` is the
    engineer's; without it the flat size is not worked out.
    """
    k = validate_k_factor(k_factor)
    meta = {"filename": filename, "source_format": source_format, "k_factor": k}
    checks: list[str] = []
    faces = topology.faces
    edges = topology.edges

    if len(topology.solids) != 1:
        return _result(
            NOT_SHEET,
            [f"The file holds {len(topology.solids)} solids; a sheet part is one body."],
            checks,
            **meta,
        )
    checks.append("one solid")

    if sheet_excluders:
        return _result(
            NOT_SHEET,
            [
                f"{feature_id} ({description}) cannot be produced by cutting and bending sheet — "
                "the same rule the route recommendation applies."
                for feature_id, description in sheet_excluders
            ],
            checks,
            **meta,
        )

    other = [f for f in faces.values() if f.surface_type not in ("plane", "cylinder")]
    if other:
        kinds = sorted({f.surface_type for f in other})
        return _result(
            UNSUPPORTED,
            [
                f"Face(s) {', '.join(str(f.index) for f in other)} are {'/'.join(kinds)} surfaces. "
                "This measurement handles planar flanges and cylindrical bends only — a cone "
                "(countersink or conical bend), a torus or a free-form surface is not unfolded here."
            ],
            checks,
            **meta,
        )

    planes = [f for f in faces.values() if f.surface_type == "plane"]
    cylinders = [f for f in faces.values() if f.surface_type == "cylinder"]

    # --- the thickness: the largest planar face's nearest opposite plane behind it
    def nearest_behind(face: FaceDescriptor) -> float | None:
        best = None
        for other_face in planes:
            if other_face is face:
                continue
            distance = _behind_distance(face, other_face)
            if distance is None or distance <= LENGTH_TOLERANCE_MM:
                continue
            if not _bbox_overlap(face, other_face, distance + LENGTH_TOLERANCE_MM):
                continue
            if best is None or distance < best:
                best = distance
        return best

    candidates = [(face, nearest_behind(face)) for face in planes]
    candidates = [(face, distance) for face, distance in candidates if distance is not None]
    if not candidates:
        return _result(
            NOT_SHEET,
            ["No planar face has an opposite planar face behind it, so there is no sheet thickness."],
            checks,
            **meta,
        )
    seed, thickness = max(candidates, key=lambda item: (item[0].area, -item[0].index))
    t = thickness
    checks.append(
        f"thickness {t:g} mm: face {seed.index}, the largest planar face, to the opposite face "
        "nearest behind it"
    )

    # --- skins: planar faces with an opposite face at exactly t, and bend pairs
    partners: dict[int, list[int]] = {}
    for face in planes:
        for other_face in planes:
            if other_face is face:
                continue
            distance = _behind_distance(face, other_face)
            if distance is None or abs(distance - t) > LENGTH_TOLERANCE_MM:
                continue
            if _bbox_overlap(face, other_face, t + LENGTH_TOLERANCE_MM):
                partners.setdefault(face.index, []).append(other_face.index)

    bend_pairs: list[tuple[FaceDescriptor, FaceDescriptor]] = []  # (inner, outer)
    paired_cylinders: set[int] = set()
    for inner in sorted((c for c in cylinders if c.is_internal), key=lambda f: f.index):
        for outer in sorted((c for c in cylinders if not c.is_internal), key=lambda f: f.index):
            if outer.index in paired_cylinders or inner.index in paired_cylinders:
                continue
            if not _same_axis(inner, outer):
                continue
            if abs((outer.radius - inner.radius) - t) > LENGTH_TOLERANCE_MM:
                continue
            if abs(inner.angular_extent - outer.angular_extent) > ANGLE_TOLERANCE_RAD:
                continue
            direction = canonical_direction(inner.axis_direction)
            ri, ro = _axial_range(inner, direction), _axial_range(outer, direction)
            if abs(ri[0] - ro[0]) > LENGTH_TOLERANCE_MM or abs(ri[1] - ro[1]) > LENGTH_TOLERANCE_MM:
                continue
            bend_pairs.append((inner, outer))
            paired_cylinders.update((inner.index, outer.index))
            partners.setdefault(inner.index, []).append(outer.index)
            partners.setdefault(outer.index, []).append(inner.index)
    skin = set(partners)

    # --- the two sides: adjacent skin faces are on one side, partners on the other
    def adjacent(face_index: int) -> list[tuple[int, int]]:
        """(neighbour face, shared edge) pairs."""
        pairs = []
        for edge_index in faces[face_index].edge_indices:
            for neighbour in edges[edge_index].face_indices:
                if neighbour != face_index and neighbour in faces:
                    pairs.append((neighbour, edge_index))
        return pairs

    side: dict[int, int] = {seed.index: 0}
    queue = [seed.index]
    conflict = None
    while queue and conflict is None:
        current = queue.pop(0)
        links = [(n, side[current]) for n, _ in adjacent(current) if n in skin]
        links += [(p, 1 - side[current]) for p in partners.get(current, [])]
        for neighbour, wanted in links:
            if neighbour not in side:
                side[neighbour] = wanted
                queue.append(neighbour)
            elif side[neighbour] != wanted:
                conflict = (current, neighbour)
                break
    if conflict is not None:
        return _result(
            NOT_SHEET,
            [
                f"Faces {conflict[0]} and {conflict[1]} would have to be on both skins at once, so "
                "the part is not one sheet of constant thickness."
            ],
            checks,
            **meta,
        )
    stray = sorted(skin - set(side))
    if stray:
        return _result(
            NOT_SHEET,
            [
                f"Face(s) {', '.join(map(str, stray))} are opposite another face at {t:g} mm but are "
                "not part of the same continuous sheet."
            ],
            checks,
            **meta,
        )
    side_a = {i for i, s in side.items() if s == 0}
    side_b = {i for i, s in side.items() if s == 1}
    checks.append(f"{len(side_a)} face(s) on one skin, {len(side_b)} on the other")

    # --- a sharp corner between flanges cannot be bent
    for group in (side_a, side_b):
        for index in sorted(group):
            if faces[index].surface_type != "plane":
                continue
            for neighbour, _ in adjacent(index):
                if neighbour in group and faces[neighbour].surface_type == "plane":
                    if not is_parallel(faces[index].normal, faces[neighbour].normal):
                        return _result(
                            UNSUPPORTED,
                            [
                                f"Faces {index} and {neighbour} meet at a sharp corner with no bend "
                                "radius. A press-brake bend has an inside radius, so this corner is "
                                "not unfolded here — it may be a welded or machined corner, or a "
                                "bend modelled without its radius."
                            ],
                            checks,
                            thickness_mm=_round(t),
                            **meta,
                        )
                if neighbour in (side_b if group is side_a else side_a):
                    return _result(
                        NOT_SHEET,
                        [f"Faces {index} and {neighbour} are on opposite skins yet touch: zero thickness there."],
                        checks,
                        thickness_mm=_round(t),
                        **meta,
                    )

    # --- everything that is not a skin must run straight through the thickness
    reasons: list[str] = []
    not_through: list[int] = []
    for face in sorted(faces.values(), key=lambda f: f.index):
        if face.index in skin:
            continue
        neighbours = [n for n, _ in adjacent(face.index)]
        touches_a = any(n in side_a for n in neighbours)
        touches_b = any(n in side_b for n in neighbours)
        described = f"Face {face.index} ({face.surface_type}, {face.area:.2f} mm²)"
        if not (touches_a and touches_b):
            not_through.append(face.index)
            continue
        for neighbour in neighbours:
            if neighbour not in skin:
                continue
            other_face = faces[neighbour]
            if face.surface_type == "plane":
                square = (
                    is_perpendicular(face.normal, other_face.normal)
                    if other_face.surface_type == "plane"
                    else is_parallel(face.normal, other_face.axis_direction)
                )
                if not square:
                    reasons.append(
                        f"{described} is not square to the sheet at face {neighbour}: a bevel or "
                        "chamfer through the thickness is not a sheet edge."
                    )
                    break
            else:
                if other_face.surface_type != "plane":
                    reasons.append(
                        f"{described} is a cylindrical cut that crosses bend face {neighbour}; "
                        "a hole or cut-out across a bend is not unfolded here."
                    )
                    break
                if not is_parallel(face.axis_direction, other_face.normal):
                    reasons.append(
                        f"{described} is a cylinder not square to flange face {neighbour}: an angled "
                        "hole is not cut from flat sheet."
                    )
                    break
                if abs((face.axial_extent or 0.0) - t) > LENGTH_TOLERANCE_MM:
                    reasons.append(
                        f"{described} is a cylinder {face.axial_extent or 0.0:g} mm deep, not "
                        f"{t:g} mm: it does not run through the sheet."
                    )
                    break
    if not_through:
        # The separations between opposite planes parallel to the sheet, which
        # say plainly when the thickness itself varies.
        separations = sorted(
            {round(distance, 3) for face, distance in candidates if is_parallel(face.normal, seed.normal)}
        )
        varies = (
            f" Opposite faces parallel to the sheet are {' and '.join(f'{d:g}' for d in separations)} "
            "mm apart: the thickness varies."
            if len(separations) > 1
            else ""
        )
        reasons.insert(
            0,
            f"Face(s) {', '.join(map(str, not_through))} do not run through the {t:g} mm thickness "
            "from one skin to the other: a step, pocket or partial-depth cut, or a change of "
            f"thickness.{varies}",
        )
    if reasons:
        return _result(NOT_SHEET, reasons, checks, thickness_mm=_round(t), **meta)
    edge_faces = len(faces) - len(skin)
    checks.append(f"{edge_faces} edge or hole face(s), each square to the sheet and {t:g} mm through")

    # --- the volume, and thinness
    planar_a_area = sum(faces[i].area for i in side_a if faces[i].surface_type == "plane")
    bend_volume_share = 0.0
    for inner, outer in bend_pairs:
        theta = inner.angular_extent
        length = inner.axial_extent
        bend_volume_share += theta * length * (inner.radius + t / 2.0)
    expected_volume = t * (planar_a_area + bend_volume_share)
    actual_volume = topology.solids[0].volume
    if actual_volume <= 0 or abs(actual_volume - expected_volume) > VOLUME_RELATIVE_TOLERANCE * actual_volume:
        return _result(
            NOT_SHEET,
            [
                f"The volume is {actual_volume:.2f} mm³, but a {t:g} mm sheet with these faces would "
                f"be {expected_volume:.2f} mm³: the thickness is not constant."
            ],
            checks,
            thickness_mm=_round(t),
            **meta,
        )
    checks.append(
        f"volume {actual_volume:.2f} mm³ = {t:g} mm × the skin's area, bends included "
        f"({expected_volume:.2f} mm³)"
    )
    skin_area = planar_a_area + sum(
        (inner.radius if inner.index in side_a else outer.radius) * inner.angular_extent * inner.axial_extent
        for inner, outer in bend_pairs
    )
    size = math.sqrt(skin_area) if skin_area > 0 else 0.0
    if size <= 0 or t > MAX_THICKNESS_RATIO * size:
        ratio = t / size if size else float("inf")
        return _result(
            NOT_SHEET,
            [
                f"The wall is {t:g} mm thick, {ratio:.0%} of the part's size (√ of one face's "
                f"area, {size:.1f} mm) — above {MAX_THICKNESS_RATIO:.0%}. That is a block or a "
                "thick plate, not sheet."
            ],
            checks,
            thickness_mm=_round(t),
            **meta,
        )
    checks.append(f"thin: {t:g} mm is {t / size:.1%} of √(skin area) = {size:.1f} mm")

    # The part is a sheet. Everything from here decides whether it can be unfolded.
    values = {"thickness_mm": _round(t), **meta}

    # --- flanges: coplanar adjacent skin-A planes are one flange
    a_planes = sorted(i for i in side_a if faces[i].surface_type == "plane")
    parent = {i: i for i in a_planes}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for index in a_planes:
        for neighbour, _ in adjacent(index):
            if neighbour in parent:
                parent[find(neighbour)] = find(index)
    groups: dict[int, list[int]] = {}
    for index in a_planes:
        groups.setdefault(find(index), []).append(index)

    # --- bends: each A-side cylinder joins two flanges along two straight tangent lines
    unsupported: list[str] = []
    bend_records = []
    for inner, outer in sorted(bend_pairs, key=lambda pair: min(pair[0].index, pair[1].index)):
        a_face = inner if inner.index in side_a else outer
        theta = a_face.angular_extent
        if theta >= MAX_BEND_SWEEP_RAD:
            unsupported.append(
                f"Bend faces {inner.index}/{outer.index} sweep {math.degrees(theta):.1f}°: a hem or "
                "curl, which is not unfolded here."
            )
            continue
        tangent_lines: list[tuple[int, int]] = []  # (flange group, edge)
        ends: list[int] = []
        problem = None
        for edge_index in a_face.edge_indices:
            edge = edges[edge_index]
            others = [n for n in edge.face_indices if n != a_face.index]
            if len(others) != 1:
                problem = "an edge that is not shared by exactly two faces"
                break
            neighbour = others[0]
            if neighbour in parent and edge.curve_type == "line":
                tangent_lines.append((find(neighbour), edge_index))
            elif neighbour not in skin and edge.curve_type == "circle":
                ends.append(edge_index)
            else:
                problem = (
                    f"edge {edge_index} ({edge.curve_type}) joins it to face {neighbour}, which is "
                    "neither a flange tangent line nor a square bend end"
                )
                break
        if problem is None and (len(tangent_lines) != 2 or len(ends) != 2):
            problem = (
                f"it has {len(tangent_lines)} tangent line(s) to flanges and {len(ends)} end(s), "
                "not two of each"
            )
        if problem is None and tangent_lines[0][0] == tangent_lines[1][0]:
            problem = "both its tangent lines are on the same flange"
        if problem is not None:
            unsupported.append(
                f"Bend faces {inner.index}/{outer.index} are not a straight, full-width bend between "
                f"two flanges: {problem}. Reliefs, cut-outs across a bend and bends running into "
                "bends are not unfolded here."
            )
            continue
        bend_records.append(
            {
                "inner": inner,
                "outer": outer,
                "a_face": a_face,
                "theta": theta,
                "length": a_face.axial_extent,
                "tangents": tangent_lines,
                "ends": ends,
            }
        )
    if unsupported:
        return _result(UNSUPPORTED, unsupported, checks, **values)

    # --- the flanges and bends must form a tree
    tree_parent = {g: g for g in groups}

    def tree_find(g):
        while tree_parent[g] != g:
            tree_parent[g] = tree_parent[tree_parent[g]]
            g = tree_parent[g]
        return g

    for record in bend_records:
        g1, g2 = record["tangents"][0][0], record["tangents"][1][0]
        r1, r2 = tree_find(g1), tree_find(g2)
        if r1 == r2:
            return _result(
                UNSUPPORTED,
                [
                    "The bends form a closed ring (a box or tube closed on itself); a closed ring "
                    "cannot be unfolded without choosing a seam, which is not done here."
                ],
                checks,
                **values,
            )
        tree_parent[r1] = r2
    if len({tree_find(g) for g in groups}) != 1:
        return _result(
            UNSUPPORTED,
            ["The flanges do not all connect through bends, so there is no single flat blank."],
            checks,
            **values,
        )

    # --- the cut outline: every skin-A edge that borders an edge or hole face
    outline: list[tuple[EdgeDescriptor, int]] = []  # (edge, owning A face)
    for index in sorted(side_a):
        for edge_index in faces[index].edge_indices:
            edge = edges[edge_index]
            if any(n not in skin for n in edge.face_indices if n != index):
                outline.append((edge, index))
    bend_end_edges = {e for record in bend_records for e in record["ends"]}
    bad = [
        edge.index
        for edge, _ in outline
        if edge.index not in bend_end_edges and edge.curve_type not in ("line", "circle")
    ]
    if bad:
        return _result(
            UNSUPPORTED,
            [
                f"Outline edge(s) {', '.join(map(str, bad))} are neither straight lines nor circular "
                "arcs, so the flat outline is not drawn here."
            ],
            checks,
            **values,
        )

    points = _Points()
    degree: dict[int, int] = {}
    edge_vertices: dict[int, tuple[int, int]] = {}
    for edge, _ in outline:
        v0, v1 = points.id(edge.start), points.id(edge.end)
        edge_vertices[edge.index] = (v0, v1)
        degree[v0] = degree.get(v0, 0) + 1
        degree[v1] = degree.get(v1, 0) + 1
    if any(count != 2 for count in degree.values()):
        return _result(
            UNSUPPORTED,
            ["The cut outline does not close into simple loops, so its contours cannot be counted."],
            checks,
            **values,
        )
    vertex_parent = {v: v for v in degree}

    def vfind(v):
        while vertex_parent[v] != v:
            vertex_parent[v] = vertex_parent[vertex_parent[v]]
            v = vertex_parent[v]
        return v

    for v0, v1 in edge_vertices.values():
        vertex_parent[vfind(v0)] = vfind(v1)
    loops: dict[int, list[EdgeDescriptor]] = {}
    for edge, _ in outline:
        loops.setdefault(vfind(edge_vertices[edge.index][0]), []).append(edge)
    contour_count = len(loops)
    flange_edge_length = sum(edge.length for edge, _ in outline if edge.index not in bend_end_edges)
    bend_end_count = len(bend_end_edges)
    checks.append(
        f"{contour_count} closed cut contour(s); {flange_edge_length:.3f} mm of flange edges plus "
        f"{bend_end_count} bend end(s) on the outline"
    )

    # --- ids: the largest flange first, then outward through the bends
    root = max(groups, key=lambda g: (sum(faces[i].area for i in groups[g]), -min(groups[g])))
    order = [root]
    flange_ids = {root: "F-1"}
    bend_order: list[dict] = []
    frontier = [root]
    while frontier:
        current = frontier.pop(0)
        for record in sorted(bend_records, key=lambda r: r["a_face"].index):
            if record in bend_order:
                continue
            groups_here = [g for g, _ in record["tangents"]]
            if current not in groups_here:
                continue
            child = groups_here[1] if groups_here[0] == current else groups_here[0]
            if child in flange_ids:
                continue
            flange_ids[child] = f"F-{len(flange_ids) + 1}"
            order.append(child)
            bend_order.append({**record, "parent": current, "child": child})
            frontier.append(child)

    def ba(record) -> float | None:
        if k is None:
            return None
        return record["theta"] * (record["inner"].radius + k * t)

    bends = []
    for number, record in enumerate(bend_order, start=1):
        record["bend_id"] = f"B-{number}"
        theta, r = record["theta"], record["inner"].radius
        allowance = ba(record)
        bends.append(
            SheetBend(
                bend_id=record["bend_id"],
                angle_deg=_round(math.degrees(theta)),
                inner_radius_mm=_round(r),
                outer_radius_mm=_round(record["outer"].radius),
                length_mm=_round(record["length"]),
                axis_direction=_tuple(canonical_direction(record["inner"].axis_direction)),
                inner_face=record["inner"].index,
                outer_face=record["outer"].index,
                flanges=(flange_ids[record["parent"]], flange_ids[record["child"]]),
                bend_allowance_mm=None if allowance is None else _round(allowance),
                bend_allowance_formula=(
                    f"BA = {theta:.6g} × ({r:g} + K × {t:g})"
                    if allowance is None
                    else f"BA = {theta:.6g} × ({r:g} + {k:g} × {t:g}) = {allowance:.4f} mm"
                ),
            )
        )

    flanges = []
    for group in order:
        members = sorted(groups[group])
        opposite = sorted({p for i in members for p in partners.get(i, [])})
        flanges.append(
            SheetFlange(
                flange_id=flange_ids[group],
                faces=members,
                opposite_faces=opposite,
                area_mm2=_round(sum(faces[i].area for i in members)),
                normal=_tuple(faces[members[0]].normal),
            )
        )

    values.update(
        bends=bends,
        flanges=flanges,
        contour_count=contour_count,
        pierce_count=contour_count,
        flange_edge_cut_length_mm=_round(flange_edge_length),
        bend_end_count=bend_end_count,
    )

    if k is None:
        return _result(
            SHEET,
            [],
            checks,
            needs_input=["k_factor"],
            flat_pattern_note=(
                "No K-factor was entered, so the flat size is not worked out. Each bend unfolds to "
                f"{BEND_ALLOWANCE_FORMULA}; K depends on the material, the tooling and r/t, and no "
                "default is assumed. The cut perimeter is the flange-edge length above plus one "
                "bend allowance per bend end."
            ),
            **values,
        )

    # --- unfold: place every flange in one plane, BA beyond its parent's tangent line
    root_face = faces[min(groups[root])]
    n_root = root_face.normal
    reference = None
    first_bend = next((r for r in bend_order if r["parent"] == root), None)
    if first_bend is not None:
        reference = first_bend["inner"].axis_direction.normalized()
        orientation = f"along the bend line of {first_bend['bend_id']}"
    else:
        straight = [
            edge for edge, owner in outline if edge.curve_type == "line" and find(owner) == root
        ]
        if straight:
            longest = max(straight, key=lambda e: (round(e.length, 6), -e.index))
            reference = (longest.end - longest.start).normalized()
            orientation = f"along the longest straight edge (edge {longest.index})"
    if reference is None:
        trial = Vec3(1.0, 0.0, 0.0)
        if is_parallel(trial, n_root):
            trial = Vec3(0.0, 1.0, 0.0)
        reference = (trial - n_root.scaled(trial.dot(n_root))).normalized()
        orientation = "along the model's x axis projected onto the flange"
    maps: dict[int, _Map] = {
        root: _Map(root_face.origin, (0.0, 0.0), reference, (1.0, 0.0), n_root.cross(reference), (0.0, 1.0))
    }
    for record in bend_order:
        p_group, c_group = record["parent"], record["child"]
        tangents = dict(record["tangents"])
        tp = edges[tangents[p_group]].start
        tc = edges[tangents[c_group]].start
        axis = record["a_face"].axis_direction.normalized()
        chord = tc - tp
        chord_perp = chord - axis.scaled(chord.dot(axis))
        n_p = faces[min(groups[p_group])].normal
        n_c = faces[min(groups[c_group])].normal
        d_p = (chord_perp - n_p.scaled(chord_perp.dot(n_p))).normalized()
        e_c = (chord_perp - n_c.scaled(chord_perp.dot(n_c))).normalized()
        parent_map = maps[p_group]
        a2, d2 = parent_map.linear(axis), parent_map.linear(d_p)
        start = parent_map(tp)
        shift = chord.dot(axis)
        allowance = ba(record)
        image = (
            start[0] + a2[0] * shift + d2[0] * allowance,
            start[1] + a2[1] * shift + d2[1] * allowance,
        )
        maps[c_group] = _Map(tc, image, axis, a2, e_c, d2)

    # 2D outline pieces, per loop: ("line", p0, p1, v0, v1) or ("arc", c, R, a0, sweep, v0, v1)
    def flange_of(edge: EdgeDescriptor, owner: int) -> int:
        return find(owner)

    pieces: dict[int, list[tuple]] = {}
    owners = {edge.index: owner for edge, owner in outline}
    xs: list[float] = []
    ys: list[float] = []
    for root_vertex, loop_edges in loops.items():
        items = []
        for edge in loop_edges:
            v0, v1 = edge_vertices[edge.index]
            if edge.index in bend_end_edges:
                record = next(r for r in bend_order if edge.index in r["ends"])
                ends = []
                for point in (edge.start, edge.end):
                    group = min(
                        (record["parent"], record["child"]),
                        key=lambda g: abs((point - faces[min(groups[g])].origin).dot(faces[min(groups[g])].normal)),
                    )
                    ends.append(maps[group](point))
                items.append(("line", ends[0], ends[1], v0, v1))
                continue
            group_map = maps[flange_of(edge, owners[edge.index])]
            if edge.curve_type == "line":
                items.append(("line", group_map(edge.start), group_map(edge.end), v0, v1))
            else:
                centre = group_map(edge.centre)
                full = v0 == v1
                start_angle, sweep = _arc_sweep(
                    centre, group_map(edge.start), group_map(edge.midpoint), group_map(edge.end), full
                )
                items.append(("arc", centre, edge.radius, start_angle, sweep, v0, v1))
        pieces[root_vertex] = items

    for items in pieces.values():
        for item in items:
            if item[0] == "line":
                for point in (item[1], item[2]):
                    xs.append(point[0])
                    ys.append(point[1])
            else:
                _, centre, radius, start_angle, sweep = item[:5]
                angles = [start_angle, start_angle + sweep]
                for quarter in range(-8, 9):
                    angle = quarter * math.pi / 2
                    low, high = sorted((start_angle, start_angle + sweep))
                    if low - 1e-12 <= angle <= high + 1e-12:
                        angles.append(angle)
                for angle in angles:
                    xs.append(centre[0] + radius * math.cos(angle))
                    ys.append(centre[1] + radius * math.sin(angle))

    def loop_area_and_length(items: list[tuple]) -> tuple[float, float]:
        """Signed area by Green's theorem over the loop, walked in order, and its length."""
        by_vertex: dict[int, list[int]] = {}
        for position, item in enumerate(items):
            v0, v1 = item[-2], item[-1]
            by_vertex.setdefault(v0, []).append(position)
            if v1 != v0:
                by_vertex.setdefault(v1, []).append(position)
        used: set[int] = set()
        area = 0.0
        length = 0.0
        position = 0
        vertex = items[0][-2]
        while position is not None and position not in used:
            used.add(position)
            item = items[position]
            forward = item[-2] == vertex
            if item[0] == "line":
                p0, p1 = (item[1], item[2]) if forward else (item[2], item[1])
                area += 0.5 * (p0[0] * p1[1] - p1[0] * p0[1])
                length += math.dist(p0, p1)
            else:
                _, centre, radius, start_angle, sweep = item[:5]
                a1, phi = (start_angle, sweep) if forward else (start_angle + sweep, -sweep)
                a2 = a1 + phi
                area += 0.5 * (
                    radius * centre[0] * (math.sin(a2) - math.sin(a1))
                    - radius * centre[1] * (math.cos(a2) - math.cos(a1))
                    + radius * radius * phi
                )
                length += abs(phi) * radius
            vertex = item[-1] if forward else item[-2]
            nxt = [p for p in by_vertex.get(vertex, []) if p not in used]
            position = nxt[0] if nxt else None
        return area, length

    loop_figures = [loop_area_and_length(items) for items in pieces.values()]
    magnitudes = sorted((abs(area), length) for area, length in loop_figures)
    outer_area, outer_length = magnitudes[-1]
    inner = magnitudes[:-1]
    inner_area = sum(area for area, _ in inner)
    inner_length = sum(length for _, length in inner)
    net_area = outer_area - inner_area
    expected_area = sum(faces[i].area for i in a_planes) + sum(
        ba(record) * record["length"] for record in bend_order
    )
    perimeter = outer_length + inner_length
    expected_perimeter = flange_edge_length + sum(
        ba(record) * len(record["ends"]) for record in bend_order
    )
    if (
        abs(net_area - expected_area) > AREA_RELATIVE_TOLERANCE * expected_area
        or abs(perimeter - expected_perimeter) > AREA_RELATIVE_TOLERANCE * expected_perimeter
    ):
        return _result(
            UNSUPPORTED,
            [
                f"The unfolded outline encloses {net_area:.3f} mm², but the flanges and bend "
                f"allowances add up to {expected_area:.3f} mm²: the flanges overlap when flat, or the "
                "outline could not be closed. No flat pattern is claimed."
            ],
            checks,
            **values,
        )

    width_x = max(xs) - min(xs)
    width_y = max(ys) - min(ys)
    flat = FlatPattern(
        k_factor=k,
        rectangle_length_mm=_round(width_x),
        rectangle_width_mm=_round(width_y),
        rectangle_area_mm2=_round(width_x * width_y),
        rectangle_orientation=orientation,
        outline_area_mm2=_round(outer_area),
        net_area_mm2=_round(net_area),
        cut_perimeter_mm=_round(perimeter),
        outer_perimeter_mm=_round(outer_length),
        inner_perimeter_mm=_round(inner_length),
        contour_count=contour_count,
        pierce_count=contour_count,
        self_check=(
            f"the unfolded outline encloses {net_area:.4f} mm², equal to the flanges' "
            f"{sum(faces[i].area for i in a_planes):.4f} mm² plus BA × length for "
            f"{len(bend_order)} bend(s)"
        ),
    )
    return _result(SHEET, [], checks, flat_pattern=flat, **values)


__all__ = [
    "BEND_ALLOWANCE_FORMULA",
    "FlatPattern",
    "MAX_THICKNESS_RATIO",
    "SheetBend",
    "SheetFlange",
    "SheetMetalAnalysis",
    "analyse_sheet_metal",
    "validate_k_factor",
]
