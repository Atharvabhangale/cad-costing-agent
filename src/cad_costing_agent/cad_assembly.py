"""Reading a STEP file's assembly structure: definitions, occurrences, placements.

Why this is a separate reader
-----------------------------
`read_step_shape` returns one shape for the whole file, which is exactly what
geometry, the viewer and feature recognition have always wanted. It is also
what makes an assembly look like a part: 43 components arrive as one lump of
material, and the volume that comes back is the whole assembly's.

OpenCASCADE's *other* STEP reader, `STEPCAFControl_Reader`, populates an XCAF
document instead — a labelled tree that keeps the product structure the file
actually contains. This module is the single place that reader is used, and it
translates its labels into plain values before anything else sees them.

Definitions and occurrences are not the same thing
--------------------------------------------------
A STEP assembly stores each part **once** and then places it as many times as
it is used. Twelve bolts are one definition and twelve occurrences, each with
its own placement. Conflating the two would either lose eleven bolts from the
bill of materials or store the same geometry twelve times, so they are kept
apart here: geometry hangs off the **definition**, and an **occurrence** is an
id, a parent, a name and a transform.

Structure survives even when geometry does not
----------------------------------------------
Measured on the real 198 MB Creo export: OpenCASCADE transfers **no faces at
all** from it, because its `FACETED_BREP` entities sit on an `OPEN_SHELL` which
ISO 10303-42 forbids — the defect `features/step_text.py` already detects. The
XCAF reader nonetheless recovers the whole product structure from the same
file: 43 definitions, 102 occurrences, four levels deep, every placement
present. So an assembly can be decomposed, named and counted even when not one
of its components can be measured, and that is worth reporting rather than
collapsing into "unreadable".

The reverse is not assumed. A definition that yielded no shape is marked as
having no geometry; the aggregate is never quietly used in its place.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cad_costing_agent import limits

# XCAF names a label by its path in the document tree — "0:1:1:3" — which is
# stable for a given file and readable, so it is used directly as the
# definition id rather than inventing a parallel numbering.
ROOT_ENTRY = "0:1:1"


@dataclass
class Placement:
    """Where an occurrence sits, relative to its parent and to the assembly.

    Both are kept. The local transform is what the file states; the world
    transform is the accumulation down the tree, which is what a viewer needs
    and what an exploded view would displace from. Deriving one from the other
    later would mean re-walking the tree, and losing either would lose
    orientation, which this step is explicitly required not to do.
    """

    # Row-major 3x4: the rotation's nine terms followed by the translation.
    local: tuple[float, ...] = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)
    world: tuple[float, ...] = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)

    @property
    def local_translation(self) -> tuple[float, float, float]:
        return (self.local[9], self.local[10], self.local[11])

    @property
    def world_translation(self) -> tuple[float, float, float]:
        return (self.world[9], self.world[10], self.world[11])

    @property
    def is_identity(self) -> bool:
        return self.local == (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)


@dataclass
class Definition:
    """One part or sub-assembly, stored once however often it is used."""

    definition_id: str
    name: str
    is_assembly: bool
    shape: object | None = field(default=None, repr=False, compare=False)
    face_count: int = 0
    solid_count: int = 0
    occurrence_count: int = 0

    @property
    def has_geometry(self) -> bool:
        return self.shape is not None and self.face_count > 0


@dataclass
class Occurrence:
    """One placement of a definition inside the assembly."""

    occurrence_id: str
    definition_id: str
    parent_occurrence_id: str | None
    name: str
    depth: int
    placement: Placement = field(default_factory=Placement)


@dataclass
class StepAssembly:
    """What the XCAF reader found: the tree, flattened into two lists."""

    definitions: dict[str, Definition] = field(default_factory=dict)
    occurrences: list[Occurrence] = field(default_factory=list)
    root_definition_ids: list[str] = field(default_factory=list)
    root_name: str = ""
    max_depth: int = 0
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def is_assembly(self) -> bool:
        """Whether the file describes more than a single part.

        A file with one definition and no placements is a part, whatever the
        reader calls it.
        """
        return len(self.occurrences) > 0

    @property
    def part_definitions(self) -> list[Definition]:
        """Definitions that are parts. A sub-assembly is not one of them.

        Counting sub-assemblies among the parts would double-count material: a
        sub-assembly's shape is its components' shapes put together.
        """
        return [value for value in self.definitions.values() if not value.is_assembly]

    @property
    def definitions_with_geometry(self) -> int:
        return sum(1 for value in self.part_definitions if value.has_geometry)

    def occurrences_of(self, definition_id: str) -> list[Occurrence]:
        return [item for item in self.occurrences if item.definition_id == definition_id]


# ------------------------------------------------------------------ helpers


def _entry(label) -> str:
    from OCP.TCollection import TCollection_AsciiString
    from OCP.TDF import TDF_Tool

    text = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, text)
    return text.ToCString()


def _name(label) -> str:
    """The label's name, or "" when it has none worth using.

    XCAF gives a *reference* label a synthetic name of the form `=>[0:1:1:3]`,
    which points at the label it refers to rather than naming anything. Treating
    that as an instance name would fill a bill of materials with strings no
    engineer has ever seen, so it is discarded and the referred definition's
    real name is used instead.
    """
    from OCP.TDataStd import TDataStd_Name

    attribute = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attribute):
        text = str(attribute.Get().ToExtString())
        if not text.startswith("=>"):
            return text
    return ""


def _matrix(transformation) -> tuple[float, ...]:
    """A gp_Trsf as a row-major 3x4 tuple of plain floats.

    Plain floats on purpose: nothing outside this module should ever hold an
    OpenCASCADE handle, so the transform crosses the boundary as numbers.
    """
    return tuple(
        float(transformation.Value(row, column)) for row in (1, 2, 3) for column in (1, 2, 3)
    ) + tuple(float(transformation.Value(row, 4)) for row in (1, 2, 3))


def _compose(parent: tuple[float, ...], child: tuple[float, ...]) -> tuple[float, ...]:
    """Parent world transform composed with a child's local transform."""
    rotation = []
    for row in range(3):
        for column in range(3):
            rotation.append(
                sum(parent[row * 3 + k] * child[k * 3 + column] for k in range(3))
            )
    translation = []
    for row in range(3):
        translation.append(
            sum(parent[row * 3 + k] * child[9 + k] for k in range(3)) + parent[9 + row]
        )
    return tuple(rotation) + tuple(translation)


def _shape_counts(shape) -> tuple[int, int]:
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer

    if shape is None or shape.IsNull():
        return (0, 0)

    counts = []
    for kind in (TopAbs_ShapeEnum.TopAbs_FACE, TopAbs_ShapeEnum.TopAbs_SOLID):
        explorer = TopExp_Explorer(shape, kind)
        total = 0
        while explorer.More():
            total += 1
            explorer.Next()
        counts.append(total)
    return (counts[0], counts[1])


# ------------------------------------------------------------------ reading


def read_step_assembly(data: bytes) -> StepAssembly:
    """Read a STEP file's product structure into plain values.

    The single place `STEPCAFControl_Reader` is used, mirroring the rule that
    `read_step_shape` is the single place ordinary STEP is parsed.

    This is deliberately *not* part of the default import. Measured on the
    198 MB export it costs about 19 seconds, against 1.4 for identifying the
    file, so it runs only when the structure is actually asked for.

    Raises ValueError for content that is not readable STEP.
    """
    try:
        from OCP.IFSelect import IFSelect_ReturnStatus
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.TDF import TDF_Label, TDF_LabelSequence
        from OCP.TDocStd import TDocStd_Document
        from OCP.XCAFApp import XCAFApp_Application
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
    except ImportError as exc:  # pragma: no cover - the package is a dependency
        raise ValueError(
            "Assembly structure requires the 'cadquery-ocp' package, which is not installed."
        ) from exc

    from cad_costing_agent.cad_geometry import _silence_occ_console

    _silence_occ_console()

    application = XCAFApp_Application.GetApplication_s()
    document = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    application.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), document)

    file_descriptor, temp_path = tempfile.mkstemp(suffix=".step")
    os.close(file_descriptor)
    try:
        Path(temp_path).write_bytes(data)

        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        # Colours, layers and properties are not used anywhere in this
        # application, and reading them on a very large file is not free.
        reader.SetColorMode(False)
        reader.SetLayerMode(False)
        reader.SetPropsMode(False)

        if reader.ReadFile(temp_path) != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise ValueError(
                "Could not parse CAD file as 'step': the content is not valid "
                "STEP (ISO 10303-21) data."
            )
        reader.Transfer(document)
    finally:
        os.unlink(temp_path)

    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())

    assembly = StepAssembly()
    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)

    if roots.Length() == 0:
        assembly.notes.append(
            "The file was read but contains no shape structure at all: no products, no "
            "components and no geometry."
        )
        return assembly

    # Every label in the document is a definition, whether it is ever placed or
    # not. Reading them first means an occurrence can always resolve its target.
    labels = TDF_LabelSequence()
    tool.GetShapes(labels)
    for index in range(1, labels.Length() + 1):
        label = labels.Value(index)
        # A reference label is an occurrence, not a definition; it is reached
        # through the walk below.
        if tool.IsReference_s(label):
            continue
        shape = tool.GetShape_s(label)
        faces, solids = _shape_counts(shape)
        identifier = _entry(label)
        assembly.definitions[identifier] = Definition(
            definition_id=identifier,
            name=_name(label) or identifier,
            is_assembly=bool(tool.IsAssembly_s(label)),
            shape=shape if shape is not None and not shape.IsNull() else None,
            face_count=faces,
            solid_count=solids,
        )

    budget = limits.MAX_ASSEMBLY_COMPONENTS

    def walk(label, parent_occurrence_id: str | None, parent_world, depth: int) -> None:
        if len(assembly.occurrences) >= budget:
            assembly.truncated = True
            return

        referred = TDF_Label()
        target = label
        local = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
        occurrence_id = None

        if tool.IsReference_s(label):
            tool.GetReferredShape_s(label, referred)
            target = referred
            local = _matrix(tool.GetLocation_s(label).Transformation())
            world = _compose(parent_world, local)

            definition_id = _entry(target)
            # The component label's own entry is unique within the document, so
            # prefixing it with the parent's occurrence keeps ids unique even
            # when the same definition is placed many times at many depths.
            occurrence_id = (
                f"{parent_occurrence_id}/{_entry(label)}"
                if parent_occurrence_id
                else _entry(label)
            )
            if definition_id not in assembly.definitions:
                shape = tool.GetShape_s(target)
                faces, solids = _shape_counts(shape)
                assembly.definitions[definition_id] = Definition(
                    definition_id=definition_id,
                    name=_name(target) or definition_id,
                    is_assembly=bool(tool.IsAssembly_s(target)),
                    shape=shape if shape is not None and not shape.IsNull() else None,
                    face_count=faces,
                    solid_count=solids,
                )

            assembly.definitions[definition_id].occurrence_count += 1
            assembly.occurrences.append(
                Occurrence(
                    occurrence_id=occurrence_id,
                    definition_id=definition_id,
                    parent_occurrence_id=parent_occurrence_id,
                    name=_name(label) or assembly.definitions[definition_id].name,
                    depth=depth,
                    placement=Placement(local=local, world=world),
                )
            )
            assembly.max_depth = max(assembly.max_depth, depth)
        else:
            world = parent_world

        children = TDF_LabelSequence()
        tool.GetComponents_s(target, children)
        for index in range(1, children.Length() + 1):
            walk(children.Value(index), occurrence_id or parent_occurrence_id, world, depth + 1)

    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
    for index in range(1, roots.Length() + 1):
        root = roots.Value(index)
        assembly.root_definition_ids.append(_entry(root))
        if not assembly.root_name:
            assembly.root_name = _name(root) or _entry(root)
        walk(root, None, identity, 0)

    if assembly.truncated:
        assembly.notes.append(
            f"Stopped after {budget:,} occurrences, which is the configured budget "
            "(CAD_COSTING_MAX_ASSEMBLY_COMPONENTS). The structure shown is incomplete and is "
            "reported as such rather than presented as the whole assembly."
        )

    without_geometry = [
        definition
        for definition in assembly.definitions.values()
        if not definition.is_assembly and not definition.has_geometry
    ]
    if without_geometry:
        assembly.notes.append(
            f"{len(without_geometry)} of {len(assembly.definitions)} definitions carry no "
            "geometry that OpenCASCADE could transfer, so they can be named, counted and "
            "placed but not measured. The usual cause is a faceted export whose FACETED_BREP "
            "entities sit on an OPEN_SHELL, which ISO 10303-42 forbids."
        )

    return assembly
