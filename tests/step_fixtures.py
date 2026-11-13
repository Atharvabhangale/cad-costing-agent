"""Deterministic STEP fixtures, generated locally with OpenCASCADE.

Nothing here is downloaded and nothing is checked in as binary. Each part is
built from primitives and booleans, written through OCCT's own STEP writer and
read back through the same reader the application uses, so the expected feature
list can be stated exactly and the fixtures cannot drift.

Every builder returns STEP bytes. The expected features for each are written
next to the builder and asserted in `tests/test_features.py`.

All dimensions are millimetres.
"""

from __future__ import annotations

import os
import tempfile

BLOCK = (60.0, 40.0, 20.0)


def _silence() -> None:
    from OCP.Message import Message, Message_PrinterOStream

    Message.DefaultMessenger_s().RemovePrinters(Message_PrinterOStream.get_type_descriptor_s())


def to_step(shape, unit: str = "MM") -> bytes:
    """Write an OCCT shape out as STEP bytes."""
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    _silence()
    # Global OCCT state, so always set it rather than inheriting.
    Interface_Static.SetCVal_s("write.step.unit", unit)

    file_descriptor, path = tempfile.mkstemp(suffix=".step")
    os.close(file_descriptor)
    try:
        writer = STEPControl_Writer()
        writer.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)
        assert writer.Write(path) == IFSelect_ReturnStatus.IFSelect_RetDone
        with open(path, "rb") as step_file:
            return step_file.read()
    finally:
        os.unlink(path)


def _box(x, y, z, at=None):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    if at is None:
        return BRepPrimAPI_MakeBox(x, y, z).Shape()
    return BRepPrimAPI_MakeBox(gp_Pnt(*at), x, y, z).Shape()


def _cylinder(radius, height, at, direction=(0.0, 0.0, 1.0)):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(*at), gp_Dir(*direction)), radius, height
    ).Shape()


def _cut(shape, tool):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    return BRepAlgoAPI_Cut(shape, tool).Shape()


def _fuse(shape, tool):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    return BRepAlgoAPI_Fuse(shape, tool).Shape()


# --- 1. a plain rectangular block -------------------------------------------
# Expected: 6 planes, nothing else. The baseline that proves the recognizers
# do not invent features, and in particular that the top face of a block is
# not mistaken for a pocket floor.
def plain_block() -> bytes:
    return to_step(_box(*BLOCK))


# --- 2. one through hole ----------------------------------------------------
# Expected: 1 hole, subtype "through", Ø10.0, depth 20.0 (the block thickness),
# axis +Z. Plus 6 planes; the two the hole passes through carry an inner
# boundary.
def block_with_through_hole() -> bytes:
    return to_step(_cut(_box(*BLOCK), _cylinder(5.0, 40.0, (30.0, 20.0, -10.0))))


# --- 3. four through holes --------------------------------------------------
# Expected: 4 holes, all "through", all Ø10.0, all depth 10.0, all axis +Z.
def plate_with_four_holes() -> bytes:
    shape = _box(80.0, 60.0, 10.0)
    for x, y in ((15.0, 15.0), (65.0, 15.0), (15.0, 45.0), (65.0, 45.0)):
        shape = _cut(shape, _cylinder(5.0, 30.0, (x, y, -10.0)))
    return to_step(shape)


# --- 4. one blind hole ------------------------------------------------------
# Expected: 1 hole, subtype "blind", Ø8.0, depth 12.0, axis +Z. The hole claims
# its own circular floor, so that face is not also reported as a plane.
def block_with_blind_hole() -> bytes:
    return to_step(_cut(_box(*BLOCK), _cylinder(4.0, 20.0, (30.0, 20.0, 8.0))))


# --- 5. a pocket ------------------------------------------------------------
# Expected: 1 pocket, 60.0 × 30.0, depth 8.0. Aspect ratio 2.0, below the slot
# threshold.
def block_with_pocket() -> bytes:
    return to_step(_cut(_box(80.0, 50.0, 20.0), _box(60.0, 30.0, 20.0, at=(10.0, 10.0, 12.0))))


# --- 6. a slot --------------------------------------------------------------
# Expected: 1 slot, 60.0 × 12.0, depth 6.0. Aspect ratio 5.0, at or above the
# slot threshold, so the same rule names it a slot rather than a pocket.
def block_with_slot() -> bytes:
    return to_step(_cut(_box(80.0, 50.0, 20.0), _box(60.0, 12.0, 20.0, at=(10.0, 19.0, 14.0))))


# --- 7. several cylindrical surfaces ----------------------------------------
# A boss on a plate, bored through, with a separate blind hole.
# Expected: 1 through hole Ø10 (depth 30: it passes through plate and boss),
# 1 blind hole Ø8 depth 12, and 1 external cylinder (the Ø18 boss).
def plate_with_boss_and_bores() -> bytes:
    shape = _fuse(_box(*BLOCK), _cylinder(9.0, 10.0, (30.0, 20.0, 20.0)))
    shape = _cut(shape, _cylinder(5.0, 50.0, (30.0, 20.0, -10.0)))
    shape = _cut(shape, _cylinder(4.0, 20.0, (12.0, 20.0, 8.0)))
    return to_step(shape)


# --- 8. a counterbore -------------------------------------------------------
# Ø8 through, opened out to Ø14 for the top 6 mm.
# Expected: 1 hole, subtype "counterbore", diameter 8.0, counterbore diameter
# 14.0, counterbore depth 6.0.
def block_with_counterbore() -> bytes:
    shape = _cut(_box(*BLOCK), _cylinder(4.0, 40.0, (30.0, 20.0, -10.0)))
    shape = _cut(shape, _cylinder(7.0, 10.0, (30.0, 20.0, 14.0)))
    return to_step(shape)


# --- 9. a countersunk hole --------------------------------------------------
# Ø8 through with a 90° countersink opening to Ø16 at the top.
# Expected: 1 hole whose subtype ends in "_countersunk", with a
# countersink_angle_deg of 90.
def block_with_countersunk_hole() -> bytes:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCone
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    shape = _cut(_box(*BLOCK), _cylinder(4.0, 40.0, (30.0, 20.0, -10.0)))
    cone = BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(30.0, 20.0, 16.0), gp_Dir(0.0, 0.0, 1.0)), 4.0, 8.0, 4.0
    ).Shape()
    return to_step(_cut(shape, cone))


# --- 10. a filleted edge ----------------------------------------------------
# Expected: 1 fillet, radius 3.0, convex.
def block_with_fillet() -> bytes:
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    shape = _box(*BLOCK)
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    first_edge = TopoDS.Edge_s(explorer.Current())
    maker = BRepFilletAPI_MakeFillet(shape)
    maker.Add(3.0, first_edge)
    return to_step(maker.Shape())


# --- 11. a chamfered edge ---------------------------------------------------
# Expected: 1 chamfer at 45°, width sqrt(2) x 2.5 for a 2.5 mm leg.
def block_with_chamfer() -> bytes:
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    shape = _box(*BLOCK)
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    first_edge = TopoDS.Edge_s(explorer.Current())
    maker = BRepFilletAPI_MakeChamfer(shape)
    maker.Add(2.5, first_edge)
    return to_step(maker.Shape())


# --- 12. two separate solids in one file ------------------------------------
# Expected: solid_count 2; solid 0 a plain block (planes only), solid 1 a block
# with one through hole. Features must stay attributed to their own solid.
def two_solids() -> bytes:
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.TopoDS import TopoDS_Compound

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    builder.Add(compound, _box(20.0, 20.0, 20.0))

    drilled = _cut(_box(30.0, 20.0, 20.0), _cylinder(6.0, 40.0, (15.0, 10.0, -10.0)))
    move = gp_Trsf()
    move.SetTranslation(gp_Vec(50.0, 0.0, 0.0))
    builder.Add(compound, BRepBuilderAPI_Transform(drilled, move).Shape())
    return to_step(compound)


# --- 13. a rotated pocket ---------------------------------------------------
# The same pocket as fixture 5, turned 30° about Z. Expected: the same
# 60.0 × 30.0 × 8.0, proving dimensions are measured in the face's own plane
# and not from an axis-aligned bounding box.
def rotated_block_with_pocket() -> bytes:
    import math

    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf

    shape = _cut(_box(80.0, 50.0, 20.0), _box(60.0, 30.0, 20.0, at=(10.0, 10.0, 12.0)))
    turn = gp_Trsf()
    turn.SetRotation(gp_Ax1(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), math.radians(30.0))
    return to_step(BRepBuilderAPI_Transform(shape, turn).Shape())


ALL_FIXTURES = {
    "plain_block": plain_block,
    "block_with_through_hole": block_with_through_hole,
    "plate_with_four_holes": plate_with_four_holes,
    "block_with_blind_hole": block_with_blind_hole,
    "block_with_pocket": block_with_pocket,
    "block_with_slot": block_with_slot,
    "plate_with_boss_and_bores": plate_with_boss_and_bores,
    "block_with_counterbore": block_with_counterbore,
    "block_with_countersunk_hole": block_with_countersunk_hole,
    "block_with_fillet": block_with_fillet,
    "block_with_chamfer": block_with_chamfer,
    "two_solids": two_solids,
    "rotated_block_with_pocket": rotated_block_with_pocket,
}


# --- shapes for manufacturing-process classification -----------------------
# Kept out of ALL_FIXTURES: they exist to exercise the family and route rules in
# `process_classification`, not the recognizers' per-fixture expectations.


# A stepped shaft: Ø30 × 40 then Ø20 × 30, one axis (+Z), ends and the shoulder
# square to it. Expected: two coaxial external cylinders and three planes, all
# perpendicular to the axis — a body of revolution.
def stepped_shaft() -> bytes:
    return to_step(
        _fuse(_cylinder(15.0, 40.0, (0.0, 0.0, 0.0)), _cylinder(10.0, 30.0, (0.0, 0.0, 40.0)))
    )


# The same shaft with a Ø8 blind hole 15 mm deep drilled on the axis from the
# small end. Expected: still rotational — the hole is on the axis and its end
# face stays square to it — with a drilling stage after turning.
def stepped_shaft_with_axial_hole() -> bytes:
    shaft = _fuse(
        _cylinder(15.0, 40.0, (0.0, 0.0, 0.0)), _cylinder(10.0, 30.0, (0.0, 0.0, 40.0))
    )
    return to_step(_cut(shaft, _cylinder(4.0, 20.0, (0.0, 0.0, 55.0))))


# The same shaft with a Ø6 cross hole through the large diameter, along X. The
# recognizer cannot classify the ends of a hole that opens onto curved faces, so
# it reports the hole as ambiguous and no drilling is suggested for it.
def stepped_shaft_with_cross_hole() -> bytes:
    shaft = _fuse(
        _cylinder(15.0, 40.0, (0.0, 0.0, 0.0)), _cylinder(10.0, 30.0, (0.0, 0.0, 40.0))
    )
    return to_step(_cut(shaft, _cylinder(3.0, 60.0, (-30.0, 0.0, 20.0), direction=(1.0, 0.0, 0.0))))


# A 2 mm L-bracket: a 60 × 40 base and a 40 mm upstand, both 2 mm thick, with
# two Ø6 through holes in the base. Expected: one constant 2 mm wall running in
# two directions, nothing cut into it — the shape cutting and bending produce.
def bent_sheet_bracket() -> bytes:
    shape = _fuse(_box(60.0, 40.0, 2.0), _box(2.0, 40.0, 40.0))
    for x, y in ((30.0, 12.0), (30.0, 28.0)):
        shape = _cut(shape, _cylinder(3.0, 10.0, (x, y, -5.0)))
    return to_step(shape)


# A flat 100 × 80 × 2 blank with four Ø8 through holes. Expected: a constant
# wall in one direction only, so a flat cut blank — no bending stage.
def flat_sheet_blank() -> bytes:
    shape = _box(100.0, 80.0, 2.0)
    for x, y in ((15.0, 15.0), (85.0, 15.0), (15.0, 65.0), (85.0, 65.0)):
        shape = _cut(shape, _cylinder(4.0, 10.0, (x, y, -5.0)))
    return to_step(shape)


# The flat blank with a 40 × 20 × 1 pocket milled into it. Expected: the wall is
# still constant over most of the area, but a pocket cannot be produced by
# cutting sheet, so machining leads and sheet metal stays a candidate.
def thin_plate_with_pocket() -> bytes:
    shape = _box(100.0, 80.0, 2.0)
    for x, y in ((15.0, 15.0), (85.0, 15.0), (15.0, 65.0), (85.0, 65.0)):
        shape = _cut(shape, _cylinder(4.0, 10.0, (x, y, -5.0)))
    return to_step(_cut(shape, _box(40.0, 20.0, 5.0, at=(30.0, 30.0, 1.0))))


PROCESS_FIXTURES = {
    "stepped_shaft": stepped_shaft,
    "stepped_shaft_with_axial_hole": stepped_shaft_with_axial_hole,
    "stepped_shaft_with_cross_hole": stepped_shaft_with_cross_hole,
    "bent_sheet_bracket": bent_sheet_bracket,
    "flat_sheet_blank": flat_sheet_blank,
    "thin_plate_with_pocket": thin_plate_with_pocket,
}


# --- shapes for wall-thickness measurement ----------------------------------
# Kept out of ALL_FIXTURES: they exist for `features.wall_thickness`, whose
# expected values are stated next to each builder. Every wall here lies between
# two parallel planes or two coaxial cylinders, so a ray along the inward normal
# measures it exactly (an analytic line/plane or line/cylinder intersection).


# A 50 x 40 x 20 box with a 44 x 34 pocket sunk from the top, leaving a floor
# 2.0 mm thick and side walls 3.0 mm thick all round. Expected minimum 2.0 (the
# floor, against the bottom face); each pocket side face's own minimum 3.0.
def box_with_pocket_walls() -> bytes:
    return to_step(_cut(_box(50.0, 40.0, 20.0), _box(44.0, 34.0, 30.0, at=(3.0, 3.0, 2.0))))


# A tube: outer radius 20, bore radius 17, 50 long. Expected minimum 3.0, the
# radial wall between the two coaxial cylinders.
def tube_3mm_wall() -> bytes:
    return to_step(
        _cut(_cylinder(20.0, 50.0, (0.0, 0.0, 0.0)), _cylinder(17.0, 70.0, (0.0, 0.0, -10.0)))
    )


# An 80 x 60 x 10 plate with a centred Ø20 through hole. The hole leaves 20 mm
# or more to every edge, so expected minimum 10.0: the plate's own thickness.
# No thin wall anywhere.
def plate_with_centre_hole() -> bytes:
    return to_step(_cut(_box(80.0, 60.0, 10.0), _cylinder(10.0, 30.0, (40.0, 30.0, -10.0))))


# A 60 x 40 x 10 base with a 0.6 mm rib, 40 long and 20 tall, standing on it.
# Expected minimum 0.6, across the rib, between its two side faces.
def thin_rib_part() -> bytes:
    return to_step(_fuse(_box(60.0, 40.0, 10.0), _box(0.6, 40.0, 20.0, at=(29.7, 0.0, 10.0))))


WALL_FIXTURES = {
    "box_with_pocket_walls": (box_with_pocket_walls, 2.0),
    "tube_3mm_wall": (tube_3mm_wall, 3.0),
    "plate_with_centre_hole": (plate_with_centre_hole, 10.0),
    "thin_rib_part": (thin_rib_part, 0.6),
}


# --- shapes for the moulding governing wall ----------------------------------
# The governing wall is the diameter of the largest ball found inside the
# material at the middle of a sampled wall (`measure_governing_wall`). Each
# expected value below was worked out by hand and is what the sampler reports.


# A moulded box: 60 x 40 x 30 outside, open at the top, every wall and the floor
# 2.0 mm. Expected governing wall 2.0 — the corners, where an inscribed ball
# grows to about 1.17 x the wall, are not where any sampled wall's middle lies.
def moulded_box_2mm() -> bytes:
    return to_step(_cut(_box(60.0, 40.0, 30.0), _box(56.0, 36.0, 40.0, at=(2.0, 2.0, 2.0))))


# An 80 x 60 x 2 plate with a solid Ø6 boss, 10 tall, standing on it. Expected
# governing wall 6.0: the boss's top face casts a ray down its axis, through the
# plate, and the ball at that span's middle (6 mm below the top) is limited by
# the boss's radius, 3 mm. The plate alone would be 2.0.
def plate_2mm_with_6mm_boss() -> bytes:
    return to_step(_fuse(_box(80.0, 60.0, 2.0), _cylinder(3.0, 10.0, (40.0, 30.0, 2.0))))


# A plain 100 x 60 x 4 plate. Expected governing wall 4.0: a ray from a side
# face runs 60 or 100 mm across the plate, and the ball at its middle is
# limited by the top and bottom faces, 2 mm away — a length is not a thickness.
def plate_4mm() -> bytes:
    return to_step(_box(100.0, 60.0, 4.0))


GOVERNING_WALL_FIXTURES = {
    "moulded_box_2mm": (moulded_box_2mm, 2.0),
    "plate_2mm_with_6mm_boss": (plate_2mm_with_6mm_boss, 6.0),
    "plate_4mm": (plate_4mm, 4.0),
}
