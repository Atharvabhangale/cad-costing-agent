"""IGES reading: the surfaces, the units, and whether they enclose a solid.

IGES is not STEP with a different extension, and the differences are the whole
reason this module exists rather than a rename.

**1. IGES usually carries no solid.** IGES 5.3 has a Manifold Solid B-Rep
Object (entity 186), but the overwhelmingly common export is a bag of trimmed
parametric surfaces (entity 144) with no shell and no solid above them.
OpenCASCADE transfers those as loose faces — and `BRepGProp.VolumeProperties`
will still happily return a number for loose faces, because it integrates the
divergence theorem over whatever faces it is given. Measured here on three
faces of a box, it returns 10666.7 mm³, which is not the volume of anything.
So a volume is reported **only** after closure has been established: either the
file brought a solid, or the free faces sew into a shell that
`BRep_Tool.IsClosed` confirms. Otherwise the geometry is `incomplete` and the
user is asked, exactly as for a faceted STEP that does not close.

**2. IGES declares its unit in a fixed field, and OpenCASCADE guesses when it
is missing.** Measured: a file declaring INCH is converted correctly to
millimetres on load, but a file with the unit flag *and* units name removed is
read as inches anyway — 40 mm became 1016 mm, silently, with no warning. That
is a 25.4x error in mass and therefore in price. So the unit flag is read here,
from the file's own Global section, and an undeclared unit is refused rather
than handed to the reader's default.

Both points are the same principle the faceted STEP path already follows: a
volume is never invented, and a unit is never guessed.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# IGES fixed-format records are 80 columns; column 73 (index 72) names the
# section, and the Global section's parameters are the first 72 columns of its
# records concatenated.
_SECTION_COLUMN = 72
_GLOBAL_SECTION = "G"
_RECORD_WIDTH = 72

# Global section parameter positions, 1-based as the specification numbers them.
_UNIT_FLAG_FIELD = 14
_UNIT_NAME_FIELD = 15
_SENDING_SYSTEM_FIELD = 5
_PREPROCESSOR_FIELD = 6
_IGES_VERSION_FIELD = 23

# Unit flag values from the IGES 5.3 specification, with what each is worth in
# millimetres. Only these are accepted; an unrecognized flag is treated as no
# declaration at all rather than as an invitation to pick something.
UNIT_FLAGS: dict[int, tuple[str, float]] = {
    1: ("inch", 25.4),
    2: ("millimetre", 1.0),
    3: ("unit_named_in_file", 0.0),  # resolved from the units name field
    4: ("foot", 304.8),
    5: ("mile", 1_609_344.0),
    6: ("metre", 1000.0),
    7: ("kilometre", 1_000_000.0),
    8: ("mil", 0.0254),
    9: ("micron", 0.001),
    10: ("centimetre", 10.0),
    11: ("microinch", 0.0000254),
}

# Accepted spellings of the units *name*, used when the flag says 3 ("see the
# units name") and as a cross-check otherwise.
UNIT_NAMES: dict[str, tuple[str, float]] = {
    "IN": ("inch", 25.4),
    "INCH": ("inch", 25.4),
    "INCHES": ("inch", 25.4),
    "MM": ("millimetre", 1.0),
    "MILLIMETRE": ("millimetre", 1.0),
    "MILLIMETER": ("millimetre", 1.0),
    "FT": ("foot", 304.8),
    "MI": ("mile", 1_609_344.0),
    "M": ("metre", 1000.0),
    "KM": ("kilometre", 1_000_000.0),
    "MIL": ("mil", 0.0254),
    "UM": ("micron", 0.001),
    "MICRON": ("micron", 0.001),
    "CM": ("centimetre", 10.0),
    "UIN": ("microinch", 0.0000254),
}

# Sewing tolerance as a fraction of the model's bounding-box diagonal, so it
# behaves the same on a 10 mm insert and a 2 m frame. Deliberately tight, for
# the same reason `cad_faceted` welds tightly: a loose tolerance would close a
# shell that is not actually closed, and then report a volume for it.
RELATIVE_SEWING_TOLERANCE = 1e-7

SEWING_METHOD = "sewn_free_surfaces_into_closed_shell"

# IGES entity type numbers worth naming. The Directory section lists the type
# of every entity in the file, so which of these are present says what kind of
# export this is — before anything is parsed.
SOLID_ENTITY = 186
"""Manifold Solid B-Rep Object. Its presence means the file carries a solid,
with its analytic surface types intact."""

TRIMMED_SURFACE_ENTITY = 144
"""Trimmed (Parametric) Surface: what a surface-mode export writes instead."""

ANALYTIC_SURFACE_ENTITIES = (190, 192, 194, 196, 198)
"""Plane, right circular cylinder, cone, sphere, torus. A surface-mode export
rewrites these as generic parametric surfaces, which is why its holes and
fillets cannot be recognized."""

_DIRECTORY_SECTION = "D"

NOT_IGES_ERROR = (
    "Could not parse CAD file as 'iges': the content is not valid IGES data. No Start or "
    "Global section record was found, which every IGES file begins with."
)

UNDECLARED_UNIT_ERROR = (
    "This IGES file does not declare a length unit that could be read from its Global "
    "section. IGES requires one, and OpenCASCADE's reader defaults a file without one to "
    "inches, which would make every dimension 25.4 times too large and the mass and cost "
    "wrong by the same factor. No geometry is reported rather than a silently rescaled one."
)


@dataclass
class IgesUnit:
    """The length unit an IGES file declares for itself."""

    flag: int
    name: str
    millimetres_per_unit: float
    declared_name: str | None = None
    source: str = "unit_flag"


@dataclass
class IgesTopology:
    """What the file turned out to contain, and what could be measured from it."""

    volume: float = 0.0
    surface_area: float = 0.0
    bbox: tuple[float, float, float] = (0.0, 0.0, 0.0)
    solid_count: int = 0
    shell_count: int = 0
    face_count: int = 0
    edge_count: int = 0
    free_edge_count: int = 0
    closed_shell_count: int = 0
    open_shell_count: int = 0
    sewing_applied: bool = False
    sewing_tolerance_mm: float | None = None
    is_solid: bool = False
    notes: list[str] = field(default_factory=list)
    shape: object | None = field(default=None, repr=False, compare=False)
    """The shape the measurements were taken from: the solid when one was
    established, otherwise the surfaces as read. Kept so feature recognition
    can work from the *same* shape that was measured rather than sewing the
    file a second time."""

    @property
    def has_measurable_volume(self) -> bool:
        return self.is_solid and self.volume > 0.0


# ------------------------------------------------------------------ units


def looks_like_iges(data: bytes) -> bool:
    """Whether the bytes are structured like an IGES file at all.

    IGES is a fixed-format record file whose column 73 names the section each
    record belongs to. Finding an S (Start) or G (Global) marker in the first
    records identifies the format without parsing it, and costs a few
    kilobytes. Used to tell "this is not an IGES file" apart from "this is an
    IGES file with a problem", which are different messages to the user.
    """
    head = data[:4096].decode("latin-1", "replace")
    lines = head.splitlines()
    # Some writers emit no line breaks at all, leaving one long run of 80-column
    # records.
    if len(lines) == 1 and len(head) > 80:
        lines = [head[index : index + 80] for index in range(0, len(head) - 79, 80)]
    return any(len(line) > _SECTION_COLUMN and line[_SECTION_COLUMN] in "SG" for line in lines[:8])


def scan_entity_types(data: bytes) -> dict[int, int]:
    """Count the entity types the Directory section declares.

    A byte scan, not a parse: every entity in an IGES file has a two-record
    Directory entry whose first eight columns hold its type number, so counting
    them says what kind of file this is for the cost of reading it once.

    This is what lets the capability report distinguish a solid-mode export
    from a surface-mode one without loading either — which matters, because the
    two give genuinely different feature results and the upload panel has to
    say which one the user has before anything heavy runs.
    """
    text = data.decode("latin-1", "replace")
    lines = text.splitlines()
    if len(lines) == 1 and len(text) > 80:
        lines = [text[index : index + 80] for index in range(0, len(text) - 79, 80)]

    counts: dict[int, int] = {}
    for line in lines:
        if len(line) > _SECTION_COLUMN and line[_SECTION_COLUMN] == _DIRECTORY_SECTION:
            try:
                entity = int(line[:8].strip())
            except ValueError:
                continue
            counts[entity] = counts.get(entity, 0) + 1
    return counts


def carries_a_solid(data: bytes) -> bool:
    """Whether the file declares a Manifold Solid B-Rep Object.

    True means the analytic surfaces survived the export and recognition will
    match STEP exactly; False means the part came across as loose surfaces,
    which are measurable once sewn but no longer carry a cylinder's identity.
    """
    return SOLID_ENTITY in scan_entity_types(data)


def _global_section_text(data: bytes) -> str:
    """The Global section's parameter text, concatenated from its records.

    Tolerant by design: a conformant file marks the section in column 73, but
    files written by less careful tools are shorter or unpadded, so a fallback
    slices between the section terminators instead.
    """
    text = data[:200_000].decode("latin-1", "replace")
    lines = text.splitlines()

    marked = [line[:_RECORD_WIDTH] for line in lines if len(line) > _SECTION_COLUMN and line[_SECTION_COLUMN] == _GLOBAL_SECTION]
    if marked:
        return "".join(marked)

    # No column-73 markers: take everything between the end of the Start
    # section and the start of the Directory section, if those can be found.
    upper = text.upper()
    start = upper.find("S0000001")
    directory = upper.find("D0000001")
    if directory > 0:
        return text[(start if start > 0 else 0) : directory]
    return ""


def _split_global_fields(section: str) -> list[str]:
    """Split a Global section into its parameters.

    The first two parameters *are* the delimiters, and string parameters are
    Hollerith-counted (`4HINCH`), so a plain `split(",")` would corrupt any
    string containing a comma — which file names and system names routinely do.
    """
    if not section:
        return []

    position = 0
    delimiter = ","
    terminator = ";"

    # Parameters 1 and 2 may redefine the delimiters, as `1H,` and `1H;`.
    if section[:1].isdigit():
        for target in ("delimiter", "terminator"):
            marker = section.find("H", position)
            if marker < 0:
                break
            try:
                length = int(section[position:marker])
            except ValueError:
                break
            value = section[marker + 1 : marker + 1 + length]
            if target == "delimiter":
                delimiter = value or delimiter
            else:
                terminator = value or terminator
            position = marker + 1 + length
            if section[position : position + 1] == delimiter:
                position += 1
        fields = ["", ""]
    else:
        fields = []

    current = ""
    while position < len(section):
        character = section[position]

        if character == terminator:
            break

        if character == delimiter:
            fields.append(current.strip())
            current = ""
            position += 1
            continue

        # A Hollerith string: <count>H<count characters>, which may contain the
        # delimiter and must be consumed whole.
        if character.isdigit():
            marker = position
            while marker < len(section) and section[marker].isdigit():
                marker += 1
            if marker < len(section) and section[marker] == "H":
                try:
                    length = int(section[position:marker])
                except ValueError:
                    length = 0
                current += section[marker + 1 : marker + 1 + length]
                position = marker + 1 + length
                continue

        current += character
        position += 1

    fields.append(current.strip())
    return fields


def _field(fields: list[str], number: int) -> str:
    """One 1-based Global parameter, or empty if the file does not go that far."""
    index = number - 1
    return fields[index].strip() if 0 <= index < len(fields) else ""


def detect_length_unit(data: bytes) -> IgesUnit | None:
    """Read the unit an IGES file declares, or None if it declares none.

    Returning None is a real answer, not a failure to try: OpenCASCADE will
    silently read an undeclared file as inches, so the caller must refuse
    rather than accept a number that is 25.4x wrong.
    """
    fields = _split_global_fields(_global_section_text(data))
    if not fields:
        return None

    raw_flag = _field(fields, _UNIT_FLAG_FIELD)
    raw_name = _field(fields, _UNIT_NAME_FIELD)
    name_key = raw_name.upper().strip()

    flag: int | None = None
    if raw_flag:
        try:
            flag = int(float(raw_flag))
        except ValueError:
            flag = None

    # Flag 3 defers to the units name, and so does a missing or unknown flag:
    # a name we recognize is a genuine declaration by the file.
    if flag is None or flag == 3 or flag not in UNIT_FLAGS:
        if name_key in UNIT_NAMES:
            name, millimetres = UNIT_NAMES[name_key]
            return IgesUnit(
                flag=flag if flag is not None else 0,
                name=name,
                millimetres_per_unit=millimetres,
                declared_name=raw_name,
                source="units_name" if flag == 3 else "units_name_without_flag",
            )
        return None

    name, millimetres = UNIT_FLAGS[flag]
    return IgesUnit(
        flag=flag,
        name=name,
        millimetres_per_unit=millimetres,
        declared_name=raw_name or None,
        source="unit_flag",
    )


def read_header(data: bytes) -> dict[str, str | None]:
    """The sending system, preprocessor and IGES version, for provenance."""
    fields = _split_global_fields(_global_section_text(data))
    if not fields:
        return {"originating_system": None, "preprocessor": None, "iges_version": None}
    return {
        "originating_system": _field(fields, _SENDING_SYSTEM_FIELD) or None,
        "preprocessor": _field(fields, _PREPROCESSOR_FIELD) or None,
        "iges_version": _field(fields, _IGES_VERSION_FIELD) or None,
    }


# ------------------------------------------------------------------ reading


def ensure_readable(data: bytes) -> IgesUnit:
    """Check both preconditions for reading IGES, in order, and return the unit.

    One function so that geometry extraction and feature recognition apply the
    same two guards in the same order and produce the same message. They are
    different problems and are worded differently: "this is not an IGES file"
    is not "this IGES file does not say what unit it is in", and a user needs
    to know which one they have.

    Both cost a few kilobytes, so they run before the parse rather than after
    it.
    """
    if not looks_like_iges(data):
        raise ValueError(NOT_IGES_ERROR)

    unit = detect_length_unit(data)
    if unit is None:
        raise ValueError(UNDECLARED_UNIT_ERROR)
    return unit


def read_iges_shape(data: bytes):
    """Read IGES bytes into an OpenCASCADE shape.

    The single place IGES is parsed, mirroring `read_step_shape`, so geometry,
    the viewer and feature recognition all agree on what "a readable IGES file"
    means and share one set of failure messages.

    The returned geometry is in millimetres: the reader honours the file's
    declared unit and converts on load. Callers must have established that a
    unit *is* declared — see `detect_length_unit`.

    Raises ValueError for content that is not valid IGES or that carries no
    transferable geometry.
    """
    try:
        from OCP.IFSelect import IFSelect_ReturnStatus
        from OCP.IGESControl import IGESControl_Controller, IGESControl_Reader
    except ImportError as exc:
        raise ValueError(
            "IGES support requires the 'cadquery-ocp' package, which is not installed."
        ) from exc

    from cad_costing_agent.cad_geometry import _silence_occ_console

    _silence_occ_console()

    # The IGES controller has to be initialised before the reader can transfer
    # anything; doing it more than once is harmless.
    IGESControl_Controller.Init_s()

    file_descriptor, temp_path = tempfile.mkstemp(suffix=".igs")
    os.close(file_descriptor)
    try:
        Path(temp_path).write_bytes(data)

        reader = IGESControl_Reader()
        if reader.ReadFile(temp_path) != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise ValueError(
                "Could not parse CAD file as 'iges': the content is not valid IGES data."
            )
        reader.TransferRoots()
        shape = reader.OneShape()
    finally:
        os.unlink(temp_path)

    if shape is None or shape.IsNull():
        raise ValueError(
            "This IGES file was read but contains no transferable geometry. IGES files that "
            "hold only annotation, dimensions or drawing views carry no surfaces to measure."
        )
    return shape


# ------------------------------------------------------------------ solidity


def _count(shape, enum) -> int:
    from OCP.TopExp import TopExp_Explorer

    explorer = TopExp_Explorer(shape, enum)
    total = 0
    while explorer.More():
        total += 1
        explorer.Next()
    return total


def _free_edge_count(shape) -> int:
    """Edges belonging to exactly one face: the boundary of an open surface set.

    Zero free edges is necessary for a closed skin, and a large count is the
    plainest possible statement of how far a surface model is from being one.
    """
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    mapping = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(
        shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE, mapping
    )
    free = 0
    for index in range(1, mapping.Extent() + 1):
        if mapping.FindFromIndex(index).Extent() == 1:
            free += 1
    return free


def _measure(shape) -> tuple[float, float]:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    volume_properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, volume_properties)
    surface_properties = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, surface_properties)
    return abs(float(volume_properties.Mass())), float(surface_properties.Mass())


def _extents(shape) -> tuple[float, float, float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box)
    x_min, y_min, z_min, x_max, y_max, z_max = box.Get()
    return (float(x_max - x_min), float(y_max - y_min), float(z_max - z_min))


def analyse_iges_shape(shape) -> IgesTopology:
    """Decide whether an IGES shape encloses a solid, and measure it if it does.

    The order matters. A file that brought its own solid is measured directly.
    Only a file that did not is sewn, and even then a volume is reported solely
    when every resulting shell is closed — because the alternative,
    `VolumeProperties` over loose faces, returns a plausible number for an open
    surface set and there is no way to tell from the number that it is wrong.
    """
    import math

    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    face_count = _count(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    edge_count = _count(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    solid_count = _count(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    extents = _extents(shape)

    if face_count == 0:
        return IgesTopology(
            bbox=extents,
            edge_count=edge_count,
            shape=shape,
            notes=[
                "The file transferred curves or points but no surfaces, so there is nothing "
                "to measure."
            ],
        )

    # Case 1: the file carried a solid (IGES entity 186, a Manifold Solid
    # B-Rep Object). Closure is already established by the reader.
    if solid_count:
        volume, area = _measure(shape)
        return IgesTopology(
            volume=volume,
            surface_area=area,
            bbox=extents,
            solid_count=solid_count,
            shell_count=_count(shape, TopAbs_ShapeEnum.TopAbs_SHELL),
            closed_shell_count=_count(shape, TopAbs_ShapeEnum.TopAbs_SHELL),
            face_count=face_count,
            edge_count=edge_count,
            free_edge_count=_free_edge_count(shape),
            is_solid=True,
            shape=shape,
            notes=[
                f"The file carries {solid_count} solid(s) as IGES Manifold Solid B-Rep "
                "Objects, so the volume is integrated over exact surfaces bounding a closed "
                "solid, exactly as for STEP."
            ],
        )

    # Case 2: free trimmed surfaces. Sew them and ask whether the result closes.
    diagonal = math.dist((0.0, 0.0, 0.0), extents)
    tolerance = max(diagonal * RELATIVE_SEWING_TOLERANCE, 1e-9)

    sewing = BRepBuilderAPI_Sewing(tolerance)
    sewing.Add(shape)
    sewing.Perform()
    sewn = sewing.SewedShape()

    closed_shells = []
    open_shells = 0
    explorer = TopExp_Explorer(sewn, TopAbs_ShapeEnum.TopAbs_SHELL)
    while explorer.More():
        shell = TopoDS.Shell_s(explorer.Current())
        if BRep_Tool.IsClosed_s(shell):
            closed_shells.append(shell)
        else:
            open_shells += 1
        explorer.Next()

    free_edges = _free_edge_count(sewn)
    shell_count = len(closed_shells) + open_shells

    if closed_shells and open_shells == 0:
        solids = []
        for shell in closed_shells:
            maker = BRepBuilderAPI_MakeSolid(shell)
            if maker.IsDone():
                solids.append(maker.Shape())

        if solids:
            from OCP.BRep import BRep_Builder
            from OCP.TopoDS import TopoDS_Compound

            builder = BRep_Builder()
            compound = TopoDS_Compound()
            builder.MakeCompound(compound)
            for solid in solids:
                builder.Add(compound, solid)

            volume, area = _measure(compound)
            return IgesTopology(
                volume=volume,
                surface_area=area,
                bbox=extents,
                solid_count=len(solids),
                shell_count=shell_count,
                closed_shell_count=len(closed_shells),
                face_count=face_count,
                edge_count=edge_count,
                free_edge_count=free_edges,
                sewing_applied=True,
                sewing_tolerance_mm=tolerance,
                is_solid=True,
                shape=compound,
                notes=[
                    f"The file carried {face_count} free trimmed surfaces and no solid. "
                    f"Sewing them at {tolerance:.3e} mm produced {len(closed_shells)} closed "
                    f"shell(s) with no boundary edges left, which was then built into "
                    f"{len(solids)} solid(s). The volume is integrated over the exact "
                    "surfaces; the surfaces themselves are the file's own, unchanged."
                ],
            )

    # Case 3: open. Report the surface area, which is real, and refuse a volume.
    _, area = _measure(sewn if shell_count else shape)
    return IgesTopology(
        volume=0.0,
        surface_area=area,
        bbox=extents,
        solid_count=0,
        shell_count=shell_count,
        closed_shell_count=len(closed_shells),
        open_shell_count=open_shells,
        face_count=face_count,
        edge_count=edge_count,
        free_edge_count=free_edges,
        sewing_applied=True,
        sewing_tolerance_mm=tolerance,
        is_solid=False,
        shape=sewn if shell_count else shape,
        notes=[
            f"This IGES file carries {face_count} trimmed surfaces and no solid. Sewing them "
            f"at {tolerance:.3e} mm left {free_edges:,} boundary edge(s) across "
            f"{shell_count} shell(s), of which {len(closed_shells)} closed and {open_shells} "
            "did not, so the surfaces do not enclose a volume. Note that integrating over an "
            "open surface set still returns a number, which is why one is not reported here: "
            "it would look exactly like a measurement."
        ],
    )
