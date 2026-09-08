"""Deterministic IGES fixtures, generated locally with OpenCASCADE.

Nothing here is downloaded and nothing is checked in as binary, for the same
reasons as `step_fixtures.py`: each part is built from primitives and booleans,
written through OCCT's own IGES writer and read back through the same reader
the application uses, so expected volumes and feature lists can be stated
exactly and the fixtures cannot drift.

The two IGES flavours, which are the point of these fixtures
------------------------------------------------------------
`IGESControl_Writer` takes a *B-Rep mode* flag, and the two settings produce
genuinely different files from the same shape:

- **solid mode** (`brep_mode=1`) writes a Manifold Solid B-Rep Object (entity
  186). Closure is explicit and analytic surface types survive, so everything
  works exactly as it does for STEP.
- **surface mode** (`brep_mode=0`) writes loose trimmed parametric surfaces
  (entity 144) with no shell or solid above them. This is the common real-world
  IGES export, and it is lossy in two separate ways: there is no solid, so
  closure has to be re-established by sewing; and cylinders and cones come back
  as generic surfaces of revolution, so the hole and fillet recognizers cannot
  match them.

Both are legitimate IGES. The application has to tell them apart and say what
each can do, which is what these fixtures exist to test.

All dimensions are millimetres.
"""

from __future__ import annotations

import os
import tempfile

BLOCK = (40.0, 30.0, 20.0)
HOLE_RADIUS = 5.0

# 40 x 30 x 20 minus a 10 mm through hole, computed rather than measured:
# 24000 - pi * 25 * 20.
BLOCK_WITH_HOLE_VOLUME = 24000.0 - 3.141592653589793 * HOLE_RADIUS**2 * BLOCK[2]


def _silence() -> None:
    from OCP.Message import Message, Message_PrinterOStream

    Message.DefaultMessenger_s().RemovePrinters(Message_PrinterOStream.get_type_descriptor_s())


def to_iges(shape, unit: str = "MM", brep_mode: int = 1) -> bytes:
    """Write an OCCT shape out as IGES bytes.

    `brep_mode=1` writes a solid; `brep_mode=0` writes loose trimmed surfaces.
    """
    from OCP.IGESControl import IGESControl_Controller, IGESControl_Writer

    _silence()
    # The IGES controller is global OCCT state and has to be initialised before
    # a writer or reader can transfer anything. Doing it repeatedly is harmless.
    IGESControl_Controller.Init_s()

    file_descriptor, path = tempfile.mkstemp(suffix=".igs")
    os.close(file_descriptor)
    try:
        writer = IGESControl_Writer(unit, brep_mode)
        writer.AddShape(shape)
        writer.ComputeModel()
        assert writer.Write(path)
        with open(path, "rb") as iges_file:
            return iges_file.read()
    finally:
        os.unlink(path)


# ------------------------------------------------------------------ shapes


def _box(x=BLOCK[0], y=BLOCK[1], z=BLOCK[2]):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(x, y, z).Shape()


def _block_with_through_hole():
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    hole = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(BLOCK[0] / 2, BLOCK[1] / 2, -1.0), gp_Dir(0, 0, 1)),
        HOLE_RADIUS,
        BLOCK[2] + 2.0,
    ).Shape()
    return BRepAlgoAPI_Cut(_box(), hole).Shape()


def _open_shell(face_count: int = 3):
    """Some of a box's faces and not the rest: surfaces that cannot enclose it.

    The case that matters most for IGES, because `BRepGProp` will still return
    a number for it.
    """
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Compound

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)

    explorer = TopExp_Explorer(_box(), TopAbs_ShapeEnum.TopAbs_FACE)
    kept = 0
    while explorer.More() and kept < face_count:
        builder.Add(compound, explorer.Current())
        kept += 1
        explorer.Next()
    return compound


# ------------------------------------------------------------------ fixtures


def box_solid() -> bytes:
    """A 40x30x20 box as a solid. Volume 24000 mm3 exactly."""
    return to_iges(_box(), brep_mode=1)


def box_surfaces() -> bytes:
    """The same box as six loose trimmed surfaces, which do sew closed."""
    return to_iges(_box(), brep_mode=0)


def block_with_hole_solid() -> bytes:
    """A block with a through hole, as a solid: recognizable as STEP is."""
    return to_iges(_block_with_through_hole(), brep_mode=1)


def block_with_hole_surfaces() -> bytes:
    """The same block as loose surfaces: measurable, but its hole is not
    recognizable, because the export rewrote the cylinder as a surface of
    revolution."""
    return to_iges(_block_with_through_hole(), brep_mode=0)


def open_surfaces() -> bytes:
    """Three faces of a box: real surfaces that enclose nothing."""
    return to_iges(_open_shell(3), brep_mode=0)


# ------------------------------------------------------------------ unit edits

_MM_DECLARATION = ",2,2HMM,"
_INCH_DECLARATION = ",1,2HIN,"  # same length, so records stay 80 columns wide
_NO_DECLARATION = ",,,"


def _rewrite_global_section(data: bytes, before: str, after: str) -> bytes:
    """Edit the Global section, keeping every record exactly 80 columns.

    IGES is a fixed-format record file: columns 1-72 are content, column 73
    names the section and columns 74-80 are the sequence number. An edit that
    changed a record's length would produce a malformed file and prove nothing,
    so the content is re-padded to 72 columns and the section letter and
    sequence number are left untouched.
    """
    lines = data.decode("latin-1").splitlines()
    rewritten = []
    for line in lines:
        if len(line) > 72 and line[72] == "G" and before in line[:72]:
            body = line[:72].replace(before, after).ljust(72)[:72]
            line = body + line[72:]
        rewritten.append(line)
    return ("\n".join(rewritten) + "\n").encode("latin-1")


def declared_as_inches(data: bytes) -> bytes:
    """The same coordinates, declared as inches instead of millimetres.

    Used to prove the reader honours the declaration: a 40 mm box relabelled
    this way must come back as 1016 mm, not 40.
    """
    return _rewrite_global_section(data, _MM_DECLARATION, _INCH_DECLARATION)


def without_unit_declaration(data: bytes) -> bytes:
    """A well-formed IGES file that simply does not say what unit it is in.

    Not a corrupted file — every record is still 80 columns and every other
    field is intact. This is the dangerous real-world case: OpenCASCADE reads
    such a file as inches without complaint, so a 40 mm part silently becomes
    1016 mm.
    """
    return _rewrite_global_section(data, _MM_DECLARATION, _NO_DECLARATION)
