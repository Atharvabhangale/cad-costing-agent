"""Deterministic STEP assemblies, generated locally with OpenCASCADE.

Built the same way as `step_fixtures.py` and for the same reasons: nothing is
downloaded, nothing binary is checked in, and every volume, quantity and
placement below is arithmetic rather than an observation, so a test can assert
the real number.

These are written through OCCT's **XCAF** STEP writer, which is what produces a
genuine product structure — `PRODUCT`, `NEXT_ASSEMBLY_USAGE_OCCURRENCE` and a
placement per occurrence — rather than a file with several solids in it. That
distinction is the whole point: a part made of several bodies is still a part,
and only a file with assembly entities is an assembly.

All dimensions are millimetres.
"""

from __future__ import annotations

import os
import tempfile

# --- the pump: two housings, a cover sub-assembly holding a cover and two pins
HOUSING = (60.0, 40.0, 20.0)
COVER = (60.0, 40.0, 5.0)
PIN_RADIUS = 4.0
PIN_HEIGHT = 30.0

HOUSING_VOLUME = HOUSING[0] * HOUSING[1] * HOUSING[2]          # 48 000
COVER_VOLUME = COVER[0] * COVER[1] * COVER[2]                  # 12 000
PIN_VOLUME = 3.141592653589793 * PIN_RADIUS**2 * PIN_HEIGHT    # ~1508

HOUSING_QUANTITY = 2
COVER_QUANTITY = 1
PIN_QUANTITY = 2

# One assembly's worth of material, which is what a roll-up must come to.
PUMP_TOTAL_VOLUME = (
    HOUSING_VOLUME * HOUSING_QUANTITY
    + COVER_VOLUME * COVER_QUANTITY
    + PIN_VOLUME * PIN_QUANTITY
)

COVER_ASSY_OFFSET = (0.0, 0.0, 20.0)
PIN_OFFSETS = ((10.0, 20.0, 5.0), (45.0, 20.0, 5.0))
SECOND_HOUSING_OFFSET = (100.0, 0.0, 0.0)


def _silence() -> None:
    from OCP.Message import Message, Message_PrinterOStream

    Message.DefaultMessenger_s().RemovePrinters(Message_PrinterOStream.get_type_descriptor_s())


def _document():
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application

    application = XCAFApp_Application.GetApplication_s()
    document = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    application.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), document)
    return document


def _named(tool, shape, name):
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    label = tool.AddShape(shape, False)
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
    return label


def _new_assembly(tool, name):
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    label = tool.NewShape()
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
    return label


def _at(offset):
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.TopLoc import TopLoc_Location

    transformation = gp_Trsf()
    transformation.SetTranslation(gp_Vec(*offset))
    return TopLoc_Location(transformation)


def _write(document) -> bytes:
    from OCP.STEPCAFControl import STEPCAFControl_Writer

    file_descriptor, path = tempfile.mkstemp(suffix=".step")
    os.close(file_descriptor)
    try:
        writer = STEPCAFControl_Writer()
        writer.Transfer(document)
        assert writer.Write(path)
        with open(path, "rb") as step_file:
            return step_file.read()
    finally:
        os.unlink(path)


def _box(x, y, z):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(x, y, z).Shape()


def _cylinder(radius, height):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), radius, height
    ).Shape()


def pump_assembly() -> bytes:
    """A nested assembly with a repeated part at two different depths.

        PUMP
        |-- HOUSING            at (0, 0, 0)
        |-- COVER_ASSY         at (0, 0, 20)
        |     |-- COVER        at (0, 0, 0)     -> world (0, 0, 20)
        |     |-- PIN          at (10, 20, 5)   -> world (10, 20, 25)
        |     +-- PIN          at (45, 20, 5)   -> world (45, 20, 25)
        +-- HOUSING            at (100, 0, 0)

    Five definitions, six occurrences, two of them the same HOUSING. That
    repetition is deliberate: it is what separates a definition from an
    occurrence, and a bill of materials that reported one housing would be
    wrong.
    """
    _silence()
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    document = _document()
    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())

    housing = _named(tool, _box(*HOUSING), "HOUSING")
    cover = _named(tool, _box(*COVER), "COVER")
    pin = _named(tool, _cylinder(PIN_RADIUS, PIN_HEIGHT), "PIN")

    cover_assembly = _new_assembly(tool, "COVER_ASSY")
    tool.AddComponent(cover_assembly, cover, TopLoc_Location())
    for offset in PIN_OFFSETS:
        tool.AddComponent(cover_assembly, pin, _at(offset))

    top = _new_assembly(tool, "PUMP")
    tool.AddComponent(top, housing, TopLoc_Location())
    tool.AddComponent(top, cover_assembly, _at(COVER_ASSY_OFFSET))
    tool.AddComponent(top, housing, _at(SECOND_HOUSING_OFFSET))
    tool.UpdateAssemblies()

    return _write(document)


def flat_assembly() -> bytes:
    """Three parts under one root, no nesting. Depth 1."""
    _silence()
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    document = _document()
    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())

    top = _new_assembly(tool, "PLATE_STACK")
    for index, name in enumerate(("PLATE_A", "PLATE_B", "PLATE_C")):
        part = _named(tool, _box(40.0, 30.0, 10.0), name)
        tool.AddComponent(top, part, _at((0.0, 0.0, index * 10.0)))
    tool.UpdateAssemblies()

    return _write(document)


def deep_assembly() -> bytes:
    """Four levels, to prove depth is preserved rather than flattened.

        L0_ROOT -> L1_SUB -> L2_SUB -> L3_SUB -> BLOCK
    """
    _silence()
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    document = _document()
    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())

    block = _named(tool, _box(10.0, 10.0, 10.0), "BLOCK")

    level3 = _new_assembly(tool, "L3_SUB")
    tool.AddComponent(level3, block, _at((1.0, 0.0, 0.0)))

    level2 = _new_assembly(tool, "L2_SUB")
    tool.AddComponent(level2, level3, _at((0.0, 2.0, 0.0)))

    level1 = _new_assembly(tool, "L1_SUB")
    tool.AddComponent(level1, level2, _at((0.0, 0.0, 4.0)))

    root = _new_assembly(tool, "L0_ROOT")
    tool.AddComponent(root, level1, _at((8.0, 0.0, 0.0)))
    tool.UpdateAssemblies()

    return _write(document)


# BLOCK's world position: 8 + 1 in x, 2 in y, 4 in z.
DEEP_BLOCK_WORLD = (9.0, 2.0, 4.0)


# A multi-body *part* — several solids under one PRODUCT, with no assembly
# entities — cannot be generated here: OpenCASCADE's STEP writer turns a
# compound into a genuine assembly, writing a PRODUCT and a
# NEXT_ASSEMBLY_USAGE_OCCURRENCE per solid. Reading such a file back as an
# assembly is therefore correct, not a misreading.
#
# The real case exists in `cadmodels/model2.stp`, a Siemens NX export with two
# MANIFOLD_SOLID_BREPs under a single PRODUCT and no assembly entities at all.
# `test_assembly.py` asserts against it when the file is present.


# --------------------------------------------------------------- committed plans
#
# A fully reviewed assembly, which several test files now need: an assembly
# whose manufactured components each carry a manufacturing plan with its times
# accepted. Generating a plan commits nothing — that is the point of the
# separation — so a test that wants a *costed* assembly has to accept the
# estimates the way an engineer does, and this is the one place that is done.


def commit_every_time(plan):
    """Accept the system's estimate on every operation and setup.

    Exactly what the operations table's "use all estimates" and the setup band's
    "use estimate" do, expressed on the JSON, so a test can produce a costable
    plan without reimplementing the three-branch resolution rule. Operations the
    system could not time are left alone: accepting an absent estimate commits
    nothing, and they stay out of the cost.
    """
    for operation in plan["operations"]:
        if operation["system_suggested_cycle_time"] is not None:
            operation["system_time_accepted"] = True
    for setup in plan["setups"]:
        if setup["system_setup_time_minutes"] is not None:
            setup["system_setup_time_accepted"] = True
    return plan


def committed_plans(client, *, rate_set_id, components=None, filename="pump.step", **extra):
    """Build every manufactured component's plan and accept its times.

    Returns the mapping `/cost/assembly` takes as `component_plans`. It goes
    through the real endpoint rather than constructing plans directly, so a test
    using it exercises the same pipeline the browser does.
    """
    import json as _json

    data = {"rate_set_id": rate_set_id}
    data.update({key: str(value) for key, value in extra.items()})
    if components is not None:
        data["components"] = _json.dumps(components)
    response = client.post(
        "/cad/assembly/plans",
        files={"file": (filename, pump_assembly(), "application/octet-stream")},
        data=data,
    )
    assert response.status_code == 200, response.text
    return {
        entry["definition_id"]: commit_every_time(entry["plan"])
        for entry in response.json()["plan"]["component_plans"]
        if entry["plan"]
    }
