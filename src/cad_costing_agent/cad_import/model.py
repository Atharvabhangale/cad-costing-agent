"""The normalized representation of an imported CAD file.

Why this exists
---------------
Before this layer, every consumer asked its own question about the file
extension: geometry extraction branched on it, the viewer branched on it, and
feature recognition branched on it. Three places had to agree about what a
format could do, and a fourth — the upload UI — simply listed extensions and
implied they all worked equally well. They do not.

`CADDocument` is one answer to "what is this file, and what can honestly be
done with it", produced once per upload and consumed by everything downstream.
Nothing here exposes OpenCASCADE, trimesh or any other library type: an adapter
translates its library's output into these plain models, so a future adapter
for a format nobody has parsed yet — a native Creo reader, say — plugs in
without costing, the viewer, feature recognition or manufacturing planning
knowing it happened.

Honesty is the design constraint
--------------------------------
Accepting an upload is not the same as supporting it, so every capability is
reported at one of four levels and **anything short of `supported` must say
why**. That is enforced in `CapabilityReport`, not left to diligence: a
capability that quietly degraded with no reason attached is exactly the failure
this layer exists to prevent.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from cad_costing_agent.cad_geometry import GeometryProperties

# --------------------------------------------------------------------- capabilities

GEOMETRY = "geometry"
MASS = "mass"
SURFACE_AREA = "surface_area"
FEATURES = "features"
VIEWER = "viewer"
ASSEMBLY = "assembly"
DRAWINGS = "drawings"
COSTING = "costing"

CAPABILITIES: tuple[str, ...] = (
    GEOMETRY,
    MASS,
    SURFACE_AREA,
    FEATURES,
    VIEWER,
    ASSEMBLY,
    DRAWINGS,
    COSTING,
)

CAPABILITY_LABELS = {
    GEOMETRY: "Geometry (volume, bounding box)",
    MASS: "Mass",
    SURFACE_AREA: "Surface area",
    FEATURES: "Feature recognition",
    VIEWER: "3D viewer",
    ASSEMBLY: "Assembly / component structure",
    DRAWINGS: "Drawing information",
    COSTING: "Costing",
}

# --------------------------------------------------------------------- support levels

SUPPORTED = "supported"
PARTIALLY_SUPPORTED = "partially_supported"
REQUIRES_USER_INPUT = "requires_user_input"
UNSUPPORTED = "unsupported"

SUPPORT_LEVELS: tuple[str, ...] = (
    SUPPORTED,
    PARTIALLY_SUPPORTED,
    REQUIRES_USER_INPUT,
    UNSUPPORTED,
)

# Ordered so that a capability can be weakened by a later refinement but never
# silently strengthened.
_SUPPORT_RANK = {SUPPORTED: 0, PARTIALLY_SUPPORTED: 1, REQUIRES_USER_INPUT: 2, UNSUPPORTED: 3}


def weaker(first: str, second: str) -> str:
    """Whichever of two support levels promises less."""
    return first if _SUPPORT_RANK[first] >= _SUPPORT_RANK[second] else second


# --------------------------------------------------------------------- units

UNIT_FILE_DECLARED = "file_declared"
"""The file states its length unit and it was read. STEP and IGES both do."""

UNIT_NOT_CARRIED_BY_FORMAT = "not_carried_by_format"
"""The format has no place to record a unit at all. STL is that case."""

UNIT_UNDECLARED = "undeclared"
"""The file should have declared a unit and did not, or declared one that could
not be read. Never resolved by assuming millimetres: a wrong guess is a 25.4x
or 1000x error in the mass, and therefore in the price."""

UNIT_SOURCES: tuple[str, ...] = (
    UNIT_FILE_DECLARED,
    UNIT_NOT_CARRIED_BY_FORMAT,
    UNIT_UNDECLARED,
)

# --------------------------------------------------------------------- representations

BREP = "brep"
"""Exact analytic surfaces bounding a solid: cylinders, cones, splines."""

FACETED = "faceted"
"""Flat polygons only, as written by a tessellating exporter."""

MESH = "mesh"
"""A triangle mesh that never claimed to be anything else — STL."""

SURFACE_MODEL = "surface_model"
"""Exact surfaces with no solid: trimmed faces that may or may not enclose a
volume. The common case for IGES, and the reason IGES needs a closure test."""

WIREFRAME = "wireframe"
"""Curves and points, no surfaces. Nothing can be measured."""

NOT_PARSED = "not_parsed"
"""The format was recognized but no parser for it exists here."""

UNKNOWN = "unknown"


class CapabilityReport(BaseModel):
    """What one capability can do with one file, and why it cannot do more.

    `detail` is mandatory for anything other than `supported`. A degraded
    capability with no stated reason reads to a user exactly like a working
    one, which is the misrepresentation this layer exists to prevent.
    """

    capability: str
    support: str
    detail: str = ""

    @model_validator(mode="after")
    def _explain_anything_less_than_full_support(self) -> "CapabilityReport":
        if self.capability not in CAPABILITIES:
            raise ValueError(
                f"Unknown capability '{self.capability}'. Known: {', '.join(CAPABILITIES)}."
            )
        if self.support not in SUPPORT_LEVELS:
            raise ValueError(
                f"Unknown support level '{self.support}'. Known: {', '.join(SUPPORT_LEVELS)}."
            )
        if self.support != SUPPORTED and not self.detail.strip():
            raise ValueError(
                f"Capability '{self.capability}' is reported as '{self.support}' with no "
                "explanation. Every capability that is not fully supported must say why, so "
                "a limitation is never presented as a success."
            )
        return self

    @property
    def label(self) -> str:
        return CAPABILITY_LABELS[self.capability]


class ImportWarning(BaseModel):
    """Something the importer wants the user to know, with a stable code."""

    code: str
    message: str


class CADComponent(BaseModel):
    """One part or sub-assembly inside an imported document.

    A part file imports as a single component holding the document's geometry —
    that is the truth about a part file, not a placeholder. The tree exists so
    that an adapter which *can* resolve an assembly has somewhere to put it
    without changing this model or anything downstream of it.
    """

    component_id: str
    name: str
    kind: str = Field(default="part", description="'part', 'assembly' or 'occurrence'.")
    geometry: GeometryProperties | None = Field(
        default=None,
        description=(
            "This component's own geometry, when it was measured separately. A single-part "
            "import puts the document's geometry here; an unresolved assembly occurrence "
            "carries none."
        ),
    )
    children: list["CADComponent"] = Field(default_factory=list)
    note: str = ""


# --------------------------------------------------------------------- assemblies

STRUCTURE_AVAILABLE = "structure_available"
"""The file states which components it is made of, and they were read."""

AGGREGATE_ONLY = "aggregate_only"
"""The file is one body of geometry with no component identity in it. An STL is
always this; so is a single-part STEP, and so is a multi-solid part, where the
solids are bodies of one part rather than separate components."""

STRUCTURE_NOT_READABLE = "structure_not_readable"
"""The format could carry structure but this one does not expose it, or the
reader could not recover it. Never resolved by inventing components."""

STRUCTURE_STATES: tuple[str, ...] = (
    STRUCTURE_AVAILABLE,
    AGGREGATE_ONLY,
    STRUCTURE_NOT_READABLE,
)


class ComponentTransform(BaseModel):
    """Where a component sits, as plain numbers.

    Row-major 3x4 — nine rotation terms then three translation terms — for both
    the placement relative to the parent and the accumulated placement in the
    assembly. Both are kept because they answer different questions: the local
    one is what the file says, and the world one is what a viewer draws and what
    a future exploded view would displace from. No library type crosses this
    boundary.
    """

    local: list[float] = Field(
        default_factory=lambda: [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0],
        min_length=12,
        max_length=12,
    )
    world: list[float] = Field(
        default_factory=lambda: [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0],
        min_length=12,
        max_length=12,
    )

    @property
    def local_translation(self) -> tuple[float, float, float]:
        return (self.local[9], self.local[10], self.local[11])

    @property
    def world_translation(self) -> tuple[float, float, float]:
        return (self.world[9], self.world[10], self.world[11])

    @property
    def is_identity(self) -> bool:
        return self.local == [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]


class ComponentDefinition(BaseModel):
    """A part or sub-assembly, stored once however many times it is used.

    Geometry hangs here rather than on an occurrence. Twelve bolts are one
    definition measured once and twelve placements, which is both what the file
    says and the only sane thing to hold in memory.
    """

    definition_id: str
    name: str
    part_number: str | None = Field(
        default=None,
        description=(
            "The part number as the file states it, when it states one. Never derived from a "
            "file name or a shape."
        ),
    )
    is_assembly: bool = False
    quantity: int = Field(
        default=0,
        description=(
            "How many times this definition occurs in the assembly. This is an occurrence "
            "count, not a production quantity: how many assemblies are being made is a "
            "separate number the file does not contain."
        ),
    )
    geometry: GeometryProperties | None = Field(
        default=None, description="Measured for this component alone. Never the assembly's."
    )
    geometry_status: str = Field(
        default="not_measured",
        description=(
            "'measured', 'no_geometry_in_file', 'not_measured' or 'failed'. A component whose "
            "geometry the file does not carry is said to have none — the aggregate is never "
            "used in its place."
        ),
    )
    geometry_note: str = ""
    face_count: int = 0
    solid_count: int = 0
    material: str | None = Field(
        default=None,
        description=(
            "Material as the CAD file states it, or null. Never inferred from appearance, "
            "density, file name or geometry; an unresolved material stays unresolved and the "
            "existing rate-set material selection applies."
        ),
    )
    capabilities: list[CapabilityReport] = Field(default_factory=list)
    source_reference: str | None = Field(
        default=None, description="How to find this component in the source file."
    )


class ComponentOccurrence(BaseModel):
    """One placement of one definition inside the assembly."""

    occurrence_id: str
    definition_id: str
    parent_occurrence_id: str | None = None
    instance_name: str = ""
    depth: int = 0
    transform: ComponentTransform = Field(default_factory=ComponentTransform)


class CADAssembly(BaseModel):
    """An assembly, kept as a structure rather than flattened into one lump."""

    assembly_id: str
    name: str
    source_format: str
    structure_state: str = Field(description="One of the STRUCTURE_STATES.")
    definitions: list[ComponentDefinition] = Field(default_factory=list)
    occurrences: list[ComponentOccurrence] = Field(default_factory=list)
    root_definition_ids: list[str] = Field(default_factory=list)
    max_depth: int = 0
    truncated: bool = Field(
        default=False, description="Whether the component budget stopped the walk."
    )
    capabilities: list[CapabilityReport] = Field(default_factory=list)
    warnings: list["ImportWarning"] = Field(default_factory=list)
    read_ms: float = 0.0

    @property
    def definition_count(self) -> int:
        return len(self.definitions)

    @property
    def occurrence_count(self) -> int:
        return len(self.occurrences)

    @property
    def part_definitions(self) -> list[ComponentDefinition]:
        return [item for item in self.definitions if not item.is_assembly]

    @property
    def measured_count(self) -> int:
        return sum(1 for item in self.definitions if item.geometry_status == "measured")

    def definition(self, definition_id: str) -> ComponentDefinition:
        for item in self.definitions:
            if item.definition_id == definition_id:
                return item
        raise KeyError(f"No component definition '{definition_id}' in this assembly.")

    def occurrences_of(self, definition_id: str) -> list[ComponentOccurrence]:
        return [item for item in self.occurrences if item.definition_id == definition_id]

    def children_of(self, occurrence_id: str | None) -> list[ComponentOccurrence]:
        return [item for item in self.occurrences if item.parent_occurrence_id == occurrence_id]


class CADMetadata(BaseModel):
    """What the file says about itself, read from its header.

    All of this comes from the first few kilobytes or from counting keywords in
    the raw bytes, so it costs almost nothing even on a 200 MB export.
    """

    byte_size: int = 0
    schema_name: str | None = Field(
        default=None, description="STEP FILE_SCHEMA, or the IGES version flag."
    )
    originating_system: str | None = Field(
        default=None,
        description="The CAD system that wrote the file, as it names itself in the header.",
    )
    entity_counts: dict[str, int] = Field(
        default_factory=dict, description="Keyword counts used to classify the file."
    )
    assembly_occurrence_count: int = Field(
        default=0,
        description=(
            "STEP NEXT_ASSEMBLY_USAGE_OCCURRENCE entities: how many component placements the "
            "file describes. Non-zero means the file is an assembly."
        ),
    )
    product_count: int = Field(default=0, description="Distinct STEP PRODUCT entities.")


class CADDocument(BaseModel):
    """One imported CAD file, normalized.

    Everything downstream of import reads this and nothing else about the
    file's origin.
    """

    filename: str
    extension: str = Field(description="The uploaded file's extension, lowercased, no dot.")
    source_format: str = Field(description="Canonical format name, e.g. 'step', 'stl', 'iges'.")
    format_label: str = Field(description="How the format is named to a person.")
    representation: str = Field(description="What the file actually contains.")
    representation_note: str = ""

    units: str | None = Field(
        default=None, description="The unit the geometry is expressed in, or null if unknown."
    )
    unit_source: str = Field(description="Where that unit came from. Never a guess.")
    unit_note: str = ""

    components: list[CADComponent] = Field(default_factory=list)
    is_assembly: bool = Field(
        default=False, description="Whether the file describes more than one component."
    )
    assembly_note: str = ""
    structure_state: str = Field(
        default=AGGREGATE_ONLY,
        description=(
            "Whether component structure is available, the file is aggregate geometry only, "
            "or structure exists but could not be read. Stated at upload so an assembly is "
            "never silently treated as a part."
        ),
    )
    assembly: CADAssembly | None = Field(
        default=None,
        description=(
            "The decomposed structure, present only when it was asked for. Reading it is far "
            "more expensive than identifying the file, so it is never done by default."
        ),
    )

    geometry: GeometryProperties | None = Field(
        default=None,
        description=(
            "Null when geometry was not requested, or when the format cannot yield any. "
            "Absence is never reported as an empty measurement."
        ),
    )

    metadata: CADMetadata = Field(default_factory=CADMetadata)
    capabilities: list[CapabilityReport] = Field(default_factory=list)
    warnings: list[ImportWarning] = Field(default_factory=list)

    detect_ms: float = Field(
        default=0.0, description="Time to identify the file and report its capabilities."
    )
    parse_ms: float = Field(
        default=0.0,
        description=(
            "Time spent parsing geometry, measured separately from detection so the import "
            "layer's own cost is visible rather than hidden inside a single total."
        ),
    )

    def capability(self, name: str) -> CapabilityReport:
        for report in self.capabilities:
            if report.capability == name:
                return report
        raise KeyError(f"No capability '{name}' was reported for this document.")

    def supports(self, name: str) -> bool:
        """True only for full support. `partially_supported` deliberately fails."""
        return self.capability(name).support == SUPPORTED

    def usable_for(self, name: str) -> bool:
        """True when the capability can produce something, even if degraded."""
        return self.capability(name).support in (SUPPORTED, PARTIALLY_SUPPORTED)

    @property
    def capability_matrix(self) -> dict[str, str]:
        return {report.capability: report.support for report in self.capabilities}

    @property
    def any_support(self) -> bool:
        """Whether anything at all can be done with this file.

        False only for a format with no parser here, which is a different kind
        of refusal from "this format is readable but cannot do that one thing":
        an STL is read perfectly and simply carries no features, while a Creo
        part cannot be read at all.
        """
        return any(report.support != UNSUPPORTED for report in self.capabilities)


CADComponent.model_rebuild()
CADAssembly.model_rebuild()
