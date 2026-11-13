"""Deterministic STEP fixtures for pull direction, draft and undercuts.

Built with OpenCASCADE from primitives, lofts and booleans and written through
OCCT's own STEP writer, in the style of `tests/step_fixtures.py`, whose helpers
they reuse. Every expected answer is stated next to its builder and follows
from the construction, not from running the code.

All dimensions are millimetres. Drafted walls are built from exact corner
coordinates (offset by `height x tan(draft)`), so each wall's draft is exact.
"""

from __future__ import annotations

import math

from tests.step_fixtures import _box, _cut, _fuse, to_step

DRAFT_DEG = 2.0
_TAN = math.tan(math.radians(DRAFT_DEG))


def _square_frustum(half_bottom: float, z_bottom: float, half_top: float, z_top: float, centre=(20.0, 20.0)):
    """A solid with square ends and four exactly planar walls between them.

    Six planar polygon faces sewn into a closed shell, so every wall is a
    `Plane` on the B-Rep (a loft would make them B-spline surfaces).
    """
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_MakeSolid,
        BRepBuilderAPI_Sewing,
    )
    from OCP.gp import gp_Pnt
    from OCP.ShapeFix import ShapeFix_Solid
    from OCP.TopoDS import TopoDS

    cx, cy = centre
    corners = ((-1, -1), (1, -1), (1, 1), (-1, 1))
    low = [(cx + a * half_bottom, cy + b * half_bottom, z_bottom) for a, b in corners]
    high = [(cx + a * half_top, cy + b * half_top, z_top) for a, b in corners]
    loops = [low[::-1], high] + [
        [low[i], low[(i + 1) % 4], high[(i + 1) % 4], high[i]] for i in range(4)
    ]
    sewing = BRepBuilderAPI_Sewing(1e-6)
    for loop in loops:
        polygon = BRepBuilderAPI_MakePolygon()
        for point in loop:
            polygon.Add(gp_Pnt(*point))
        polygon.Close()
        sewing.Add(BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face())
    sewing.Perform()
    solid = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(sewing.SewedShape())).Solid()
    fixer = ShapeFix_Solid(solid)
    fixer.Perform()  # orients the shell outward
    return fixer.Solid()


def _frustum_cone(r_bottom: float, z_bottom: float, r_top: float, z_top: float, centre=(20.0, 20.0)):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCone
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    return BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(centre[0], centre[1], z_bottom), gp_Dir(0.0, 0.0, 1.0)),
        r_bottom,
        r_top,
        z_top - z_bottom,
    ).Shape()


# --- a cup with every side wall drafted 2 deg, opening upward ------------------
# Outer: 40 x 40 at z=0, widening upward to the rim at z=20, so the outside
# releases downward (-Z half). Pocket: floor 30 x 30 at z=5, widening upward,
# so the inside releases upward (+Z half). 11 faces: bottom, 4 outer walls,
# rim, 4 inner walls, floor. Pulled along Z: no undercut, no zero-draft face,
# every wall exactly 2 deg. Along X or Y the pocket is an undercut.


def drafted_cup() -> bytes:
    outer = _square_frustum(20.0, 0.0, 20.0 + 20.0 * _TAN, 20.0)
    # The pocket tool runs past the rim so the cut leaves no coplanar sliver.
    pocket = _square_frustum(15.0, 5.0, 15.0 + 20.0 * _TAN, 25.0)
    return to_step(_cut(outer, pocket))


# --- the same cup with 0 deg walls ---------------------------------------------
# 40 x 40 x 20 block with a 30 x 30 pocket from z=5 out through the top.
# Pulled along Z: no undercut, and all 8 walls (4 outer, 4 inner) are zero-draft.
# Along X: the 4 inner walls and the floor are undercuts (5); likewise along Y.


def zero_draft_cup() -> bytes:
    return to_step(_cut(_box(40.0, 40.0, 20.0), _box(30.0, 30.0, 20.0, at=(5.0, 5.0, 5.0))))


# --- a round cup with conical walls drafted 2 deg ------------------------------
# The cone version of the drafted cup: a cone whose axis is the pull has the
# cone's half-angle as its exact draft. 5 faces: bottom, outer cone, rim, inner
# cone, floor. Pulled along Z: no undercut, cones at 2 deg.


def drafted_round_cup() -> bytes:
    outer = _frustum_cone(20.0, 0.0, 20.0 + 20.0 * _TAN, 20.0)
    pocket = _frustum_cone(15.0, 5.0, 15.0 + 20.0 * _TAN, 25.0)
    return to_step(_cut(outer, pocket))


# --- a block with a side hole --------------------------------------------------
# 40 x 30 x 20 with an R5 hole straight through along X at (y=15, z=10).
# Along Z or Y the bore is an undercut (1 face); along X nothing is, and the
# bore is an axial cylinder with an exact 0 deg draft. X is recommended.


def block_with_side_hole() -> bytes:
    from tests.step_fixtures import _cylinder

    return to_step(_cut(_box(40.0, 30.0, 20.0), _cylinder(5.0, 50.0, (-5.0, 15.0, 10.0), (1.0, 0.0, 0.0))))


# --- a closed T-slot: an overhang that no principal axis releases --------------
# 60 x 50 x 40 block. A narrow slot (x 15..45, y 22..28, z 30..40) opens the top
# into a wider chamber below it (x 15..45, y 15..35, z 18..30). Both stop short
# of the block's ends, so the chamber's ceiling hangs over its floor (Z), its end
# walls face each other (X) and its side walls face each other (Y): an undercut
# along every principal axis.


def closed_t_slot() -> bytes:
    void = _fuse(
        _box(30.0, 6.0, 15.0, at=(15.0, 22.0, 30.0)),
        _box(30.0, 20.0, 12.0, at=(15.0, 15.0, 18.0)),
    )
    return to_step(_cut(_box(60.0, 50.0, 40.0), void))


# --- a plain plate ---------------------------------------------------------------
# 100 x 60 x 5. No axis has an undercut. Zero-draft area along Z is the four
# edges, 2 x (100 + 60) x 5 = 1600 mm², the least of the three, so Z is
# recommended.


def plate() -> bytes:
    return to_step(_box(100.0, 60.0, 5.0))
