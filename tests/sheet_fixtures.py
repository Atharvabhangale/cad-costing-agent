"""Deterministic sheet-metal STEP fixtures, generated locally with OpenCASCADE.

Each bent part is a 2D sheet profile — straight runs joined by circular bends of
a stated inside radius — extruded across its width. Because the profile is
built from those numbers, its flat pattern can be worked out by hand: every
expected answer sits next to its builder and is asserted in
`tests/test_sheet_metal.py`.

All dimensions are millimetres. Nothing is downloaded or checked in as binary.
"""

from __future__ import annotations

import math

from tests.step_fixtures import _box, _cut, _cylinder, _fuse, to_step

THICKNESS = 2.0
INNER_RADIUS = 3.0
WIDTH = 40.0


def _rotate(point, centre, angle):
    (x, y), (cx, cy) = point, centre
    c, s = math.cos(angle), math.sin(angle)
    return (cx + c * (x - cx) - s * (y - cy), cy + s * (x - cx) + c * (y - cy))


def _walk(segments, t, r):
    """Side A and side B of a sheet profile, as ("line", p0, p1) / ("arc", p0, pm, p1).

    `segments` is a list of ("line", length) and ("bend", degrees, "left"|"right").
    Side A is the walked reference line; the material lies `t` to its left. A
    left bend has side A on the outside (radius r + t), a right bend on the
    inside (radius r).
    """
    p, d = (0.0, 0.0), (1.0, 0.0)
    side_a: list = []  # ("line", p0, p1) | ("arc", p0, pm, p1)
    side_b: list = []

    def left(vector):
        return (-vector[1], vector[0])

    for segment in segments:
        n = left(d)
        b = (p[0] + n[0] * t, p[1] + n[1] * t)
        if segment[0] == "line":
            length = segment[1]
            p1 = (p[0] + d[0] * length, p[1] + d[1] * length)
            b1 = (b[0] + d[0] * length, b[1] + d[1] * length)
            side_a.append(("line", p, p1))
            side_b.append(("line", b, b1))
            p = p1
        else:
            _, degrees, direction = segment
            angle = math.radians(degrees) * (1 if direction == "left" else -1)
            if direction == "left":
                centre = (p[0] + n[0] * (r + t), p[1] + n[1] * (r + t))
            else:
                centre = (p[0] - n[0] * r, p[1] - n[1] * r)
            side_a.append(("arc", p, _rotate(p, centre, angle / 2), _rotate(p, centre, angle)))
            side_b.append(("arc", b, _rotate(b, centre, angle / 2), _rotate(b, centre, angle)))
            p = _rotate(p, centre, angle)
            d = _rotate(d, (0.0, 0.0), angle)
    return side_a, side_b


def _edge(item, reverse=False):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Pnt

    def pnt(xy):
        return gp_Pnt(xy[0], xy[1], 0.0)

    if item[0] == "line":
        a, b = (item[2], item[1]) if reverse else (item[1], item[2])
        return BRepBuilderAPI_MakeEdge(pnt(a), pnt(b)).Edge()
    a, m, b = (item[3], item[2], item[1]) if reverse else (item[1], item[2], item[3])
    return BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(pnt(a), pnt(m), pnt(b)).Value()).Edge()


def sheet_profile_solid(segments, *, thickness=THICKNESS, inner_radius=INNER_RADIUS, width=WIDTH):
    """Extrude a sheet profile along +Z by `width`.

    Straight lengths are the flat runs between bends, the same on both sides,
    which is what makes the flat length a hand sum.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec

    side_a, side_b = _walk(segments, thickness, inner_radius)
    edge = _edge

    def pnt(xy):
        return gp_Pnt(xy[0], xy[1], 0.0)

    wire = BRepBuilderAPI_MakeWire()
    for item in side_a:
        wire.Add(edge(item))
    end_a, end_b = side_a[-1][-1], side_b[-1][-1]
    wire.Add(BRepBuilderAPI_MakeEdge(pnt(end_a), pnt(end_b)).Edge())
    for item in reversed(side_b):
        wire.Add(edge(item, reverse=True))
    wire.Add(BRepBuilderAPI_MakeEdge(pnt(side_b[0][1]), pnt(side_a[0][1])).Edge())
    face = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, width)).Shape()


# --- 1. a flat 100 x 60 x 2 plate with two Ø10 through holes ---------------------
# No bends. Flat = the plate. Perimeter = 2(100 + 60) + 2 x π x 10 = 320 + 20π.
# Contours: the outline and two holes = 3; pierces = 3. Net area 6000 - 2 x 25π.
def flat_plate_two_holes() -> bytes:
    shape = _box(100.0, 60.0, 2.0)
    for x in (25.0, 75.0):
        shape = _cut(shape, _cylinder(5.0, 10.0, (x, 30.0, -5.0)))
    return to_step(shape)


# --- 2. an L-bracket: 50 flat, 90° bend r = 3, 30 flat; t = 2, 40 wide ----------
# Flat length = 50 + 30 + BA, BA = (π/2)(3 + 2K). Perimeter = 2 x flat + 2 x 40.
L_BRACKET = [("line", 50.0), ("bend", 90.0, "left"), ("line", 30.0)]


def l_bracket() -> bytes:
    return to_step(sheet_profile_solid(L_BRACKET))


# --- 3. a U-channel: 40, 90° left, 60, 90° left, 40 -----------------------------
# Flat length = 140 + 2 x BA.
U_CHANNEL = [
    ("line", 40.0),
    ("bend", 90.0, "left"),
    ("line", 60.0),
    ("bend", 90.0, "left"),
    ("line", 40.0),
]


def u_channel() -> bytes:
    return to_step(sheet_profile_solid(U_CHANNEL))


# --- 4. the L-bracket with a Ø8 hole through its 30 mm flange --------------------
# The 30 mm flange is the upright one: x in [53, 55] once the left bend about
# (50, 5) has turned it, rising from y = 5. Its hole unfolds with it: one more
# contour, π x 8 more perimeter, 16π less area.
L_FLANGE_HOLE_AT = (15.0, 20.0)  # (distance along the flange from its tangent line, z)


def l_bracket_with_flange_hole() -> bytes:
    shape = sheet_profile_solid(L_BRACKET)
    centre_x = 50.0
    along, z = L_FLANGE_HOLE_AT
    y = (INNER_RADIUS + THICKNESS) + along
    hole = _cylinder(4.0, 20.0, (centre_x - 10.0, y, z), direction=(1.0, 0.0, 0.0))
    return to_step(_cut(shape, hole))


# --- 5. a block: not a sheet --------------------------------------------------------
def thick_block() -> bytes:
    return to_step(_box(60.0, 40.0, 20.0))


# --- 6. a plate that steps from 2 mm to 4 mm: varying thickness --------------------
def stepped_plate() -> bytes:
    return to_step(_fuse(_box(100.0, 50.0, 2.0), _box(40.0, 50.0, 4.0, at=(60.0, 0.0, 0.0))))


# --- 7. a Z-bracket: one bend each way -------------------------------------------
# Flat length = 30 + 25 + 35 + 2 x BA (both bends are 90°, r = 3).
Z_BRACKET = [
    ("line", 30.0),
    ("bend", 90.0, "left"),
    ("line", 25.0),
    ("bend", 90.0, "right"),
    ("line", 35.0),
]


def z_bracket() -> bytes:
    return to_step(sheet_profile_solid(Z_BRACKET))


# --- 8. a 45° bend --------------------------------------------------------------------
# Flat length = 40 + 40 + (π/4)(3 + 2K).
OBTUSE = [("line", 40.0), ("bend", 45.0, "right"), ("line", 40.0)]


def bend_45() -> bytes:
    return to_step(sheet_profile_solid(OBTUSE))


# --- 9. the U-channel turned to an arbitrary orientation -------------------------
# Same answers as the U-channel: an unfold must not depend on where the part sits.
def rotated_u_channel() -> bytes:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

    rotation = gp_Trsf()
    rotation.SetRotation(gp_Ax1(gp_Pnt(3.0, -7.0, 11.0), gp_Dir(0.3, 0.7, -0.5)), math.radians(37.0))
    shift = gp_Trsf()
    shift.SetTranslation(gp_Vec(120.0, -45.0, 18.0))
    shape = BRepBuilderAPI_Transform(sheet_profile_solid(U_CHANNEL), rotation, True).Shape()
    return to_step(BRepBuilderAPI_Transform(shape, shift, True).Shape())


# --- 10. a hem: a 180° bend folds the sheet back on itself ------------------------
def hemmed_strip() -> bytes:
    return to_step(sheet_profile_solid([("line", 40.0), ("bend", 180.0, "left"), ("line", 20.0)]))


# --- 11. a closed square tube of bent sheet: a ring of four bends -----------------
def closed_tube() -> bytes:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec

    segments = [("line", 30.0), ("bend", 90.0, "left")] * 4
    side_a, side_b = _walk(segments, THICKNESS, INNER_RADIUS)
    outer, inner = BRepBuilderAPI_MakeWire(), BRepBuilderAPI_MakeWire()
    for item in side_a:
        outer.Add(_edge(item))
    for item in reversed(side_b):
        inner.Add(_edge(item, reverse=True))
    face = BRepBuilderAPI_MakeFace(outer.Wire())
    face.Add(inner.Wire())
    return to_step(BRepPrimAPI_MakePrism(face.Face(), gp_Vec(0.0, 0.0, WIDTH)).Shape())


SHEET_FIXTURES = {
    "rotated_u_channel": rotated_u_channel,
    "hemmed_strip": hemmed_strip,
    "closed_tube": closed_tube,
    "flat_plate_two_holes": flat_plate_two_holes,
    "l_bracket": l_bracket,
    "u_channel": u_channel,
    "l_bracket_with_flange_hole": l_bracket_with_flange_hole,
    "thick_block": thick_block,
    "stepped_plate": stepped_plate,
    "z_bracket": z_bracket,
    "bend_45": bend_45,
}
