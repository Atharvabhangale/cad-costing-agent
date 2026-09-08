"""One adapter per CAD format, each declaring what it can honestly deliver.

An adapter answers three questions and nothing else:

1. *Is this file mine?* — by extension, and by what the bytes actually contain.
2. *What can be done with it?* — a capability report per capability, refined by
   a cheap inspection of the file and then, if geometry was parsed, by what the
   parse turned out to yield.
3. *How do I get it?* — by delegating to the existing measurement, viewer and
   recognition code. An adapter implements no geometry of its own.

The declarations are the single source of truth for the whole application: the
API serves them, the upload UI renders them, and the endpoints gate on them. So
a format cannot appear supported in the interface while being unsupported in
fact — there is only one statement of what works, and everything reads it.

Inspection is deliberately cheap
--------------------------------
Nothing here parses geometry. Classification is a keyword count over the raw
bytes and a header read of the first few kilobytes, which is about a second on
a 198 MB export against roughly twenty for OpenCASCADE merely to open the same
file. Geometry is parsed once, only when asked for, and timed separately.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from cad_costing_agent.cad_geometry import (
    INCOMPLETE,
    GeometryProperties,
    extract_geometry_from_bytes,
)
from cad_costing_agent.cad_import.model import (
    AGGREGATE_ONLY,
    ASSEMBLY,
    BREP,
    COSTING,
    DRAWINGS,
    FACETED,
    FEATURES,
    GEOMETRY,
    MASS,
    MESH,
    NOT_PARSED,
    PARTIALLY_SUPPORTED,
    REQUIRES_USER_INPUT,
    SUPPORTED,
    SURFACE_AREA,
    STRUCTURE_AVAILABLE,
    SURFACE_MODEL,
    UNIT_FILE_DECLARED,
    UNIT_NOT_CARRIED_BY_FORMAT,
    UNIT_UNDECLARED,
    UNKNOWN,
    UNSUPPORTED,
    VIEWER,
    WIREFRAME,
    CADComponent,
    CADMetadata,
    CapabilityReport,
    ImportWarning,
)


class UnknownFormatError(ValueError):
    """The extension matches no adapter, and the bytes identify nothing known.

    A subclass of ValueError so existing handlers keep working unchanged.
    """


class UnsupportedFormatError(ValueError):
    """The format is recognized, but nothing here can parse it.

    Distinct from `UnknownFormatError` because the answer to the user is
    different: not "what is this?" but "this is a Creo part file, and reading
    one needs a licensed integration that is not present."
    """

    def __init__(self, message: str, *, source_format: str, format_label: str) -> None:
        super().__init__(message)
        self.source_format = source_format
        self.format_label = format_label


@dataclass
class Inspection:
    """What a cheap look at the file established."""

    representation: str = UNKNOWN
    representation_note: str = ""
    units: str | None = None
    unit_source: str = UNIT_UNDECLARED
    unit_note: str = ""
    metadata: CADMetadata = field(default_factory=CADMetadata)
    components: list[CADComponent] = field(default_factory=list)
    is_assembly: bool = False
    assembly_note: str = ""
    structure_state: str = AGGREGATE_ONLY
    warnings: list[ImportWarning] = field(default_factory=list)
    capability_overrides: dict[str, CapabilityReport] = field(default_factory=dict)


def _report(capability: str, support: str, detail: str = "") -> CapabilityReport:
    return CapabilityReport(capability=capability, support=support, detail=detail)


# --------------------------------------------------------------------- base


class CADAdapter:
    """Base class. A subclass declares its format and its capabilities."""

    source_format: str = ""
    label: str = ""
    extensions: tuple[str, ...] = ()
    headline: str = ""

    reads_assembly_structure: bool = False
    """Whether this format can carry a component hierarchy that a reader here
    can recover. Declared by the adapter so no caller has to ask "is this
    STEP?" — which is the question the registry exists to stop being asked in
    six places."""

    unit_source: str = UNIT_UNDECLARED
    """Where this format's unit comes from, known before any file is read. The
    upload UI uses it to decide whether to ask the user for a unit, so it must
    not be inferred from a file extension anywhere else."""

    unit_note: str = ""

    def declared_capabilities(self) -> list[CapabilityReport]:
        raise NotImplementedError

    def inspect(self, data: bytes, filename: str) -> Inspection:
        raise NotImplementedError

    def geometry(self, data: bytes) -> GeometryProperties:
        """Measure the file. Only called when `geometry` is usable."""
        return extract_geometry_from_bytes(data, self.source_format)

    def matches_content(self, data: bytes) -> bool:
        """Whether the bytes look like this format, regardless of extension."""
        return False


# --------------------------------------------------------------------- STEP

_STEP_HEADER_LIMIT = 8192
_FILE_NAME_PATTERN = re.compile(rb"FILE_NAME\s*\(", re.IGNORECASE)


def _step_header_strings(data: bytes) -> list[str]:
    """The quoted strings of the STEP FILE_NAME entity, in order.

    Comments are stripped first: real files put `/* originating_system */`
    labels between the fields, and those contain no quotes but do contain
    commas, which would otherwise be counted as field separators.
    """
    head = data[:_STEP_HEADER_LIMIT]
    match = _FILE_NAME_PATTERN.search(head)
    if not match:
        return []
    tail = head[match.end() : match.end() + 2048]
    tail = re.sub(rb"/\*.*?\*/", b"", tail, flags=re.DOTALL)
    return [value.decode("utf-8", "replace") for value in re.findall(rb"'([^']*)'", tail)]


def _step_originating_system(data: bytes) -> str | None:
    # FILE_NAME(name, time_stamp, author, organization, preprocessor_version,
    #           originating_system, authorisation) — author and organization
    # are lists, so positions shift; the last two non-empty strings before the
    # authorisation are what identify the system, and taking the 6th quoted
    # string works for the files this has been checked against.
    strings = _step_header_strings(data)
    for index in (5, 4):
        if len(strings) > index and strings[index].strip():
            return strings[index].strip()
    return None


class StepAdapter(CADAdapter):
    """STEP (ISO 10303-21), analytic or faceted.

    The one format where everything works, and the format the rest of the
    application was built around.
    """

    source_format = "step"
    label = "STEP / STP"
    extensions = ("step", "stp")
    headline = "Full geometry, feature recognition, 3D viewer and costing"
    reads_assembly_structure = True
    unit_source = UNIT_FILE_DECLARED
    unit_note = "STEP declares its own unit; the reader converts the geometry to millimetres."

    def matches_content(self, data: bytes) -> bool:
        return b"ISO-10303-21" in data[:_STEP_HEADER_LIMIT]

    def declared_capabilities(self) -> list[CapabilityReport]:
        return [
            _report(GEOMETRY, SUPPORTED),
            _report(MASS, SUPPORTED),
            _report(SURFACE_AREA, SUPPORTED),
            _report(FEATURES, SUPPORTED),
            _report(VIEWER, SUPPORTED),
            _report(
                ASSEMBLY,
                PARTIALLY_SUPPORTED,
                "Component structure is extracted on request: definitions, occurrences, "
                "quantities, placements and per-component geometry where the file carries it. "
                "What is not implemented is assembly-level manufacturing — no assembly "
                "labour, fastening time, purchased-part handling or make/buy classification. "
                "An assembly total is the sum of component cost x quantity and nothing else.",
            ),
            _report(
                DRAWINGS,
                UNSUPPORTED,
                "No drawing views, dimensions or tolerances are read from STEP. Only geometry "
                "is used.",
            ),
            _report(COSTING, SUPPORTED),
        ]

    def inspect(self, data: bytes, filename: str) -> Inspection:
        from cad_costing_agent.features.step_text import classify_step_bytes

        classification = classify_step_bytes(data)
        occurrences = data.count(b"NEXT_ASSEMBLY_USAGE_OCCURRENCE")
        products = data.count(b"PRODUCT(")

        metadata = CADMetadata(
            byte_size=len(data),
            schema_name=classification.schema,
            originating_system=_step_originating_system(data),
            entity_counts=dict(classification.counts),
            assembly_occurrence_count=occurrences,
            product_count=products,
        )

        warnings = [ImportWarning(code="step_classification", message=note) for note in classification.notes]
        overrides: dict[str, CapabilityReport] = {}

        representation = classification.representation
        if classification.is_faceted:
            representation = FACETED
            overrides[FEATURES] = _report(
                FEATURES,
                PARTIALLY_SUPPORTED,
                "This is a faceted export with no exact surfaces, so features are "
                "reconstructed by fitting geometry to polygons rather than read from the "
                "file. Diameters and axes are approximations and every suggestion from them "
                "is capped at medium confidence.",
            )
        elif classification.representation == WIREFRAME:
            representation = WIREFRAME
            for capability in (GEOMETRY, MASS, SURFACE_AREA, FEATURES, VIEWER, COSTING):
                overrides[capability] = _report(
                    capability,
                    UNSUPPORTED,
                    "This file contains points or curves but no surfaces, so there is no "
                    "geometry to measure, draw, analyse or cost.",
                )

        # Units: STEP states its own, and the OpenCASCADE reader converts the
        # geometry to millimetres while loading. A faceted export is scaled by
        # `cad_faceted` from the unit the file declares, and refuses if it
        # declares none.
        unit_note = (
            "STEP carries its own length unit. The reader converted this geometry to "
            "millimetres on load, so a file authored in inches is still reported in mm."
        )

        assembly_note = ""
        is_assembly = occurrences > 0
        structure_state = STRUCTURE_AVAILABLE if is_assembly else AGGREGATE_ONLY
        if is_assembly:
            assembly_note = (
                f"This STEP file describes an assembly: it contains {occurrences:,} component "
                f"placements across {products:,} product definitions. Uploaded as it stands it "
                "is measured as a single lump of material, so the volume and mass are the "
                "whole assembly's and not any one part's. Decompose it to cost the components "
                "individually."
            )
            warnings.append(ImportWarning(code="assembly_not_a_part", message=assembly_note))
        else:
            overrides[ASSEMBLY] = _report(
                ASSEMBLY,
                UNSUPPORTED,
                "This file contains no assembly structure — it describes a single part. A "
                "part made of several solid bodies is still a part, not an assembly.",
            )

        return Inspection(
            representation=representation,
            representation_note=" ".join(classification.notes),
            units="mm" if representation != WIREFRAME else None,
            unit_source=UNIT_FILE_DECLARED,
            unit_note=unit_note,
            metadata=metadata,
            is_assembly=is_assembly,
            assembly_note=assembly_note,
            structure_state=structure_state,
            warnings=warnings,
            capability_overrides=overrides,
        )


# --------------------------------------------------------------------- STL


class StlAdapter(CADAdapter):
    """STL: a triangle mesh, and only ever a triangle mesh."""

    source_format = "stl"
    label = "STL"
    extensions = ("stl",)
    headline = "Mesh geometry, 3D viewer and costing — no feature recognition"
    unit_source = UNIT_NOT_CARRIED_BY_FORMAT
    unit_note = "STL records no unit. Choose the unit the model was drawn in."

    def matches_content(self, data: bytes) -> bool:
        if data[:5].lower() == b"solid":
            return True
        # Binary STL: an 80-byte header, a uint32 triangle count, then exactly
        # 50 bytes per triangle. The size has to agree exactly.
        if len(data) >= 84:
            (count,) = struct.unpack("<I", data[80:84])
            return len(data) == 84 + count * 50
        return False

    def declared_capabilities(self) -> list[CapabilityReport]:
        no_units = (
            "STL records no length unit anywhere in the file, so the numbers are whatever "
            "unit the exporting CAD system used. Nothing here can verify it, and no scaling "
            "is applied. If the part was not exported in millimetres, the mass and therefore "
            "the cost are wrong by that factor."
        )
        return [
            _report(GEOMETRY, SUPPORTED),
            _report(MASS, PARTIALLY_SUPPORTED, no_units),
            _report(SURFACE_AREA, SUPPORTED),
            _report(
                FEATURES,
                UNSUPPORTED,
                "Feature recognition needs the exact faces and edges of a B-Rep model. In a "
                "triangle mesh a hole is indistinguishable from a many-sided prism, so no "
                "features are reported rather than guessed ones. Upload the STEP version of "
                "the part for feature analysis.",
            ),
            _report(VIEWER, SUPPORTED),
            _report(
                ASSEMBLY,
                UNSUPPORTED,
                "STL has no concept of a component, a name or an assembly — it is one "
                "unstructured set of triangles. Separate bodies in the mesh are not "
                "components and are not presented as any, because nothing in the file says "
                "they are.",
            ),
            _report(DRAWINGS, UNSUPPORTED, "STL carries no drawing information of any kind."),
            _report(COSTING, PARTIALLY_SUPPORTED, no_units),
        ]

    def inspect(self, data: bytes, filename: str) -> Inspection:
        binary = not data[:5].lower() == b"solid"
        return Inspection(
            representation=MESH,
            representation_note=(
                f"{'Binary' if binary else 'ASCII'} STL: a triangle mesh with no exact "
                "surfaces and no structure."
            ),
            units=None,
            unit_source=UNIT_NOT_CARRIED_BY_FORMAT,
            unit_note=(
                "STL has no field for a length unit, so none was read and none was assumed. "
                "The geometry is reported in the file's own numbers."
            ),
            metadata=CADMetadata(byte_size=len(data)),
        )


# --------------------------------------------------------------------- IGES


class IgesAdapter(CADAdapter):
    """IGES (ASME Y14.26M / IGES 5.3).

    Reads through OpenCASCADE's IGES reader, which is a genuinely different
    reader from the STEP one and behaves differently in two ways that matter —
    see `cad_iges` for both, and for why a volume is only reported once closure
    has been established.
    """

    source_format = "iges"
    label = "IGES / IGS"
    extensions = ("iges", "igs")
    headline = "Geometry, 3D viewer and costing — features only from a solid export"
    unit_source = UNIT_FILE_DECLARED
    unit_note = (
        "IGES declares its unit in its Global section; the reader converts to millimetres. "
        "A file that declares none is refused rather than assumed."
    )

    def matches_content(self, data: bytes) -> bool:
        from cad_costing_agent.cad_iges import looks_like_iges

        return looks_like_iges(data)

    def declared_capabilities(self) -> list[CapabilityReport]:
        surfaces_may_not_close = (
            "IGES normally stores a part as loose trimmed surfaces rather than as a solid. "
            "Those surfaces are sewn and tested for closure: when they close, the volume is "
            "integrated over the exact surfaces and is as good as STEP's. When they do not, "
            "no volume is reported and the missing figure is asked for, because integrating "
            "over an open surface set still returns a plausible-looking number."
        )
        return [
            _report(GEOMETRY, SUPPORTED),
            _report(MASS, PARTIALLY_SUPPORTED, surfaces_may_not_close),
            _report(SURFACE_AREA, SUPPORTED),
            _report(
                FEATURES,
                PARTIALLY_SUPPORTED,
                "Recognition needs solid topology to tell the inside of a hole from the "
                "outside of a boss, or a concave fillet from a convex one. When the file's "
                "surfaces close into a solid the results match STEP exactly; when they do "
                "not, only planar faces are reported and holes and fillets are missing — "
                "which is stated on the result rather than left to look like a part with no "
                "holes.",
            ),
            _report(VIEWER, SUPPORTED),
            _report(
                ASSEMBLY,
                UNSUPPORTED,
                "IGES has no product structure to extract: it is a flat collection of "
                "geometric entities with no part identity, no names and no placements. "
                "Separate solids in the file are bodies, not components, and are not "
                "presented as components. Export STEP to keep an assembly's structure.",
            ),
            _report(
                DRAWINGS,
                UNSUPPORTED,
                "IGES can carry annotation, dimension and view entities, but none of them are "
                "read here. Only surface geometry is used.",
            ),
            _report(COSTING, PARTIALLY_SUPPORTED, surfaces_may_not_close),
        ]

    def inspect(self, data: bytes, filename: str) -> Inspection:
        from cad_costing_agent.cad_iges import (
            SOLID_ENTITY,
            TRIMMED_SURFACE_ENTITY,
            detect_length_unit,
            looks_like_iges,
            read_header,
            scan_entity_types,
        )

        if not looks_like_iges(data):
            # Named `.iges` but structured like nothing. Capabilities are left
            # as declared so the parse still runs and fails with the reader's
            # own message, which says the content is not IGES — reporting this
            # as a missing unit would describe the wrong problem.
            return Inspection(
                representation=UNKNOWN,
                representation_note="Not classified: the content is not structured as IGES.",
                units=None,
                unit_source=UNIT_UNDECLARED,
                unit_note="No unit was read: the file is not readable as IGES.",
                metadata=CADMetadata(byte_size=len(data)),
                warnings=[
                    ImportWarning(
                        code="iges_content_not_recognized",
                        message=(
                            "This file is named as IGES but contains no IGES Start or Global "
                            "section record, so it is not an IGES file."
                        ),
                    )
                ],
            )

        header = read_header(data)
        unit = detect_length_unit(data)
        entities = scan_entity_types(data)
        metadata = CADMetadata(
            byte_size=len(data),
            schema_name=(
                f"IGES version flag {header['iges_version']}" if header["iges_version"] else None
            ),
            originating_system=header["originating_system"],
            entity_counts={f"entity_{number}": count for number, count in sorted(entities.items())},
        )

        warnings: list[ImportWarning] = []
        overrides: dict[str, CapabilityReport] = {}

        if unit is None:
            # Measured behaviour: OpenCASCADE reads an IGES file with no unit
            # declaration as inches, silently. A 40 mm part comes back as
            # 1016 mm and nothing in the result says so. Refusing is the only
            # honest option.
            undeclared = (
                "This IGES file does not declare a length unit that could be read from its "
                "Global section. The unit is mandatory in IGES, and the reader would default "
                "to inches, which would make every dimension 25.4 times too large and the "
                "mass and cost wrong by the same factor. Supply the geometry yourself or "
                "re-export the file with its unit declared."
            )
            warnings.append(ImportWarning(code="iges_unit_undeclared", message=undeclared))
            for capability in (GEOMETRY, MASS, SURFACE_AREA, VIEWER, FEATURES, COSTING):
                overrides[capability] = _report(capability, REQUIRES_USER_INPUT, undeclared)
            return Inspection(
                representation=UNKNOWN,
                representation_note="Not classified: the file's length unit is undeclared.",
                units=None,
                unit_source=UNIT_UNDECLARED,
                unit_note=undeclared,
                metadata=metadata,
                warnings=warnings,
                capability_overrides=overrides,
            )

        # Which kind of IGES export this is decides what recognition can do, and
        # it is readable from the Directory section without parsing anything.
        # Doing it here rather than after the parse means the upload panel can
        # tell the user before the heavy work starts — and means the answer is
        # the same whether or not geometry was asked for.
        has_solid = SOLID_ENTITY in entities
        if has_solid:
            representation = BREP
            representation_note = (
                f"The file declares {entities[SOLID_ENTITY] // 2 or 1} Manifold Solid B-Rep "
                "Object(s) (IGES entity 186), so it carries solid geometry with its analytic "
                "surfaces intact — cylinders are still cylinders."
            )
            overrides[FEATURES] = _report(
                FEATURES,
                SUPPORTED,
                "This file carries solid geometry with its analytic surfaces intact, so "
                "recognition works exactly as it does for STEP.",
            )
        else:
            representation = SURFACE_MODEL
            trimmed = entities.get(TRIMMED_SURFACE_ENTITY, 0) // 2
            representation_note = (
                f"The file declares no solid, only {trimmed or 'loose'} trimmed surface(s) "
                "(IGES entity 144). They are sewn and tested for closure to measure a volume; "
                "closure is never assumed."
            )
            overrides[FEATURES] = _report(
                FEATURES,
                PARTIALLY_SUPPORTED,
                "This file stores its surfaces individually rather than as a solid. They can "
                "still be sewn into a solid and measured, so the volume, mass and cost are "
                "sound — but a surface export also rewrites cylinders and cones as generic "
                "spline surfaces, and sewing cannot undo that. Holes, pockets, fillets and "
                "chamfers will be missing from the feature list even though the part has "
                "them. Export the IGES as solid/B-Rep geometry, or use STEP, to recognize "
                "them.",
            )
            warnings.append(
                ImportWarning(
                    code="iges_surface_export",
                    message=(
                        "This is a surface-mode IGES export with no solid. Its volume and mass "
                        "are derived by sewing the surfaces and confirming they close, but its "
                        "holes and fillets cannot be recognized — a cylinder has been rewritten "
                        "as a generic surface, and that is not recoverable."
                    ),
                )
            )

        return Inspection(
            representation=representation,
            representation_note=representation_note,
            units="mm",
            unit_source=UNIT_FILE_DECLARED,
            unit_note=(
                f"The file declares its unit as {unit.name} "
                f"({'unit flag ' + str(unit.flag) if unit.source == 'unit_flag' else 'units name field'})"
                f", and the reader converted the geometry to millimetres on load."
            ),
            metadata=metadata,
            warnings=warnings,
            capability_overrides=overrides,
        )


# --------------------------------------------------------------------- Creo


_CREO_NATIVE_DETAIL = (
    "Reading a native Creo file needs Creo itself or a licensed translator. The formats are "
    "proprietary and undocumented, no open-source parser reads them reliably, and none is "
    "present here: the OpenCASCADE build in use ships readers for STEP, IGES, STL, VRML and "
    "glTF only. Rather than accept the upload and report nothing, this format is refused with "
    "this explanation. Export the part as STEP or IGES from Creo, or plug a licensed native "
    "adapter into this same interface."
)

_PRT_AMBIGUITY = (
    "The .prt extension is not even unique to Creo — Siemens NX and Solid Edge both use it "
    "for their own unrelated formats — so the extension alone does not identify which CAD "
    "system wrote the file."
)


class _CreoAdapter(CADAdapter):
    """Recognized, and honestly refused.

    This exists so that native Creo support is a matter of implementing
    `inspect` and `geometry` behind an already-wired interface, rather than
    threading a new format back through geometry, the viewer, feature
    recognition, planning and costing. Everything downstream reads
    `CADDocument`, so it would not need to change at all.
    """

    kind_note: str = ""
    unit_note = "No unit is read, because the file is not parsed."

    def declared_capabilities(self) -> list[CapabilityReport]:
        return [
            _report(capability, UNSUPPORTED, f"{self.kind_note} {_CREO_NATIVE_DETAIL}".strip())
            for capability in (
                GEOMETRY,
                MASS,
                SURFACE_AREA,
                FEATURES,
                VIEWER,
                ASSEMBLY,
                DRAWINGS,
                COSTING,
            )
        ]

    def inspect(self, data: bytes, filename: str) -> Inspection:
        return Inspection(
            representation=NOT_PARSED,
            representation_note=self.kind_note,
            units=None,
            unit_source=UNIT_UNDECLARED,
            unit_note=(
                "No unit was read: the file was not parsed. A unit is never assumed for an "
                "unparsed file."
            ),
            metadata=CADMetadata(byte_size=len(data)),
            warnings=[
                ImportWarning(
                    code="native_creo_not_supported",
                    message=f"{self.kind_note} {_CREO_NATIVE_DETAIL}".strip(),
                )
            ],
        )

    def geometry(self, data: bytes) -> GeometryProperties:
        raise UnsupportedFormatError(
            f"{self.kind_note} {_CREO_NATIVE_DETAIL}".strip(),
            source_format=self.source_format,
            format_label=self.label,
        )


class CreoPartAdapter(_CreoAdapter):
    source_format = "creo_part"
    label = "Creo part (.prt)"
    extensions = ("prt",)
    headline = "Native Creo support: not available"
    kind_note = f"A .prt file is a native Creo (Pro/ENGINEER) part. {_PRT_AMBIGUITY}"


class CreoAssemblyAdapter(_CreoAdapter):
    source_format = "creo_assembly"
    label = "Creo assembly (.asm)"
    extensions = ("asm",)
    headline = "Native Creo support: not available"
    kind_note = (
        "An .asm file is a native Creo assembly. It references its member parts by name "
        "rather than containing them, so even a reader for it would need the whole set of "
        "referenced .prt files to resolve anything."
    )


class CreoDrawingAdapter(_CreoAdapter):
    source_format = "creo_drawing"
    label = "Creo drawing (.drw)"
    extensions = ("drw",)
    headline = "Native Creo support: not available"
    kind_note = (
        "A .drw file is a native Creo drawing: views, dimensions, notes and tolerances "
        "referring to a model held in a separate file. It contains no solid geometry, so no "
        "amount of parsing would yield a volume — a drawing is not a 3D model and is not "
        "treated as one here."
    )


ADAPTERS: tuple[CADAdapter, ...] = (
    StepAdapter(),
    StlAdapter(),
    IgesAdapter(),
    CreoPartAdapter(),
    CreoAssemblyAdapter(),
    CreoDrawingAdapter(),
)
