"""The CAD adapter registry: one place where formats are identified.

    CAD file
       |
    format detection            (extension, cross-checked against the bytes)
       |
    CAD adapter                 (declares its capabilities)
       |
    normalized CADDocument      (geometry, metadata, components, capabilities)
       |
    downstream systems          (mass, viewer, features, planning, costing)

What this replaced
------------------
Three modules each asked their own question about the file extension, and the
upload UI listed extensions with no statement of what any of them could do. A
format's capabilities were therefore spread across four places that had to
agree, and nothing checked that they did.

Now the registry answers "what is this file and what can be done with it" once.
`main.py` parses no extensions, and the upload UI is rendered from
`format_catalog()` — the same declarations the endpoints gate on — so the
interface cannot advertise a capability the backend does not have.

Detection is by extension, cross-checked
----------------------------------------
The extension chooses the adapter, but the bytes get the final say: a STEP file
saved as `.stl` is processed as the STEP file it is, with a warning naming the
mismatch. Refusing it would be unhelpful when the content is unambiguous, and
parsing it as STL would fail with a message about triangles that explains
nothing.
"""

from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel, Field

from cad_costing_agent.cad_geometry import COMPLETE, INCOMPLETE, GeometryProperties
from cad_costing_agent.cad_import.adapters import (
    ADAPTERS,
    CADAdapter,
    CreoAssemblyAdapter,
    CreoDrawingAdapter,
    CreoPartAdapter,
    IgesAdapter,
    Inspection,
    StepAdapter,
    StlAdapter,
    UnknownFormatError,
    UnsupportedFormatError,
)
from cad_costing_agent.cad_import.model import (
    AGGREGATE_ONLY,
    ASSEMBLY,
    CAPABILITIES,
    COSTING,
    DRAWINGS,
    FEATURES,
    GEOMETRY,
    MASS,
    PARTIALLY_SUPPORTED,
    REQUIRES_USER_INPUT,
    SUPPORTED,
    STRUCTURE_AVAILABLE,
    STRUCTURE_NOT_READABLE,
    SURFACE_AREA,
    UNSUPPORTED,
    VIEWER,
    CADAssembly,
    CADComponent,
    CADDocument,
    CapabilityReport,
    ComponentDefinition,
    ComponentOccurrence,
    ComponentTransform,
    ImportWarning,
    weaker,
)

__all__ = [
    "ADAPTERS",
    "CADAdapter",
    "CADAssembly",
    "CADComponent",
    "CADDocument",
    "ComponentDefinition",
    "ComponentOccurrence",
    "ComponentTransform",
    "decompose_assembly",
    "CapabilityReport",
    "FormatDescription",
    "ImportWarning",
    "Inspection",
    "UnknownFormatError",
    "UnsupportedFormatError",
    "adapter_for",
    "detect_format",
    "format_catalog",
    "import_cad_document",
    "require",
    "supported_extensions",
]


class FormatDescription(BaseModel):
    """One row of the capability matrix, for the API and the upload UI."""

    source_format: str
    label: str
    extensions: list[str]
    headline: str
    unit_source: str = Field(
        description=(
            "Where this format's length unit comes from, before any file is read. Static per "
            "format: STEP and IGES declare their own, STL has nowhere to record one."
        )
    )
    unit_note: str = ""
    capabilities: list[CapabilityReport]
    any_support: bool = Field(
        description="Whether anything at all can be done with this format today."
    )
    native_integration_required: bool = Field(
        description="Whether support waits on a licensed or proprietary integration."
    )

    @property
    def capability_matrix(self) -> dict[str, str]:
        return {report.capability: report.support for report in self.capabilities}


def _extension_of(filename: str) -> str:
    return Path(filename or "").suffix.strip().lower().lstrip(".")


def adapter_for(extension: str) -> CADAdapter | None:
    """The adapter that claims an extension, or None."""
    normalized = extension.strip().lower().lstrip(".")
    for adapter in ADAPTERS:
        if normalized in adapter.extensions:
            return adapter
    return None


def supported_extensions() -> list[str]:
    """Every extension the registry recognizes, supported or not.

    Recognizing an extension is not the same as supporting it — `.prt` is
    recognized precisely so that it can be refused with an explanation instead
    of a shrug.
    """
    return sorted({extension for adapter in ADAPTERS for extension in adapter.extensions})


def _identify_by_content(data: bytes) -> CADAdapter | None:
    for adapter in ADAPTERS:
        if adapter.matches_content(data):
            return adapter
    return None


def detect_format(filename: str, data: bytes | None = None) -> tuple[CADAdapter, list[ImportWarning]]:
    """Choose the adapter for an upload, with the bytes as the tie-breaker.

    Raises `UnknownFormatError` when neither the extension nor the content
    identifies anything the registry knows.
    """
    extension = _extension_of(filename)
    by_extension = adapter_for(extension)
    warnings: list[ImportWarning] = []

    by_content = _identify_by_content(data) if data else None

    if by_extension is None:
        if by_content is not None:
            warnings.append(
                ImportWarning(
                    code="extension_unrecognized",
                    message=(
                        f"The extension '{extension or '(none)'}' is not one this application "
                        f"recognizes, but the file's contents are {by_content.label}, so it was "
                        "read as that."
                    ),
                )
            )
            return by_content, warnings
        known = ", ".join(f".{value}" for value in supported_extensions())
        # Worded to match the phrase every other unsupported-input message in
        # this application uses, so one failure does not read as a different
        # kind of failure from the rest.
        raise UnknownFormatError(
            f"Unsupported CAD file format '{extension or filename}'. This application reads "
            f"{known}, and the contents of this file do not identify any of them either."
        )

    if by_content is not None and by_content.source_format != by_extension.source_format:
        warnings.append(
            ImportWarning(
                code="extension_content_mismatch",
                message=(
                    f"This file is named '.{extension}' but its contents are "
                    f"{by_content.label}. It was read as {by_content.label}, which is what it "
                    "actually is — the extension is wrong, not the file."
                ),
            )
        )
        return by_content, warnings

    return by_extension, warnings


def _resolved_capabilities(
    adapter: CADAdapter, inspection: Inspection
) -> list[CapabilityReport]:
    """The format's declarations, refined by what this particular file contains."""
    resolved: list[CapabilityReport] = []
    for declared in adapter.declared_capabilities():
        override = inspection.capability_overrides.get(declared.capability)
        resolved.append(override if override is not None else declared)
    return resolved


def _weaken(
    capabilities: list[CapabilityReport], names: tuple[str, ...], support: str, detail: str
) -> list[CapabilityReport]:
    """Lower named capabilities, never raise them.

    Used after a parse: a file whose geometry turned out to be unmeasurable
    cannot be reported as costable, but a capability that was already worse
    than this must not be quietly improved either.
    """
    updated = []
    for report in capabilities:
        if report.capability in names:
            level = weaker(report.support, support)
            updated.append(
                CapabilityReport(
                    capability=report.capability,
                    support=level,
                    detail=detail if level == support else report.detail,
                )
            )
        else:
            updated.append(report)
    return updated


def _replace(
    capabilities: list[CapabilityReport], name: str, support: str, detail: str
) -> list[CapabilityReport]:
    """State a capability outright, because the file settled the question.

    Not a silent upgrade: this is only used where the parse produced a definite
    answer that the format-level declaration could only describe as conditional
    — an IGES file whose surfaces did close, for instance.
    """
    return [
        CapabilityReport(capability=name, support=support, detail=detail)
        if report.capability == name
        else report
        for report in capabilities
    ]


def import_cad_document(
    data: bytes,
    filename: str,
    *,
    with_geometry: bool = True,
    with_assembly: bool = False,
    assembly_structure=None,
) -> CADDocument:
    """Import one CAD file into the normalized representation.

    `with_geometry=False` skips the parse entirely and returns identification,
    units, metadata and capabilities only. That is what the upload UI and the
    capability endpoints use, and it is why a 198 MB export can be described in
    about a second: nothing heavy runs unless a measurement was actually asked
    for.

    Raises `UnknownFormatError` for a file nothing recognizes,
    `UnsupportedFormatError` for a recognized format with no parser, and
    `ValueError` for content that its own format's reader rejects.
    """
    started = time.perf_counter()

    adapter, warnings = detect_format(filename, data)

    if not data:
        raise ValueError("CAD file is empty.")

    inspection = adapter.inspect(data, filename)
    capabilities = _resolved_capabilities(adapter, inspection)
    warnings = warnings + list(inspection.warnings)
    detect_ms = (time.perf_counter() - started) * 1000.0

    geometry: GeometryProperties | None = None
    parse_ms = 0.0

    geometry_support = next(
        report.support for report in capabilities if report.capability == GEOMETRY
    )

    if with_geometry and geometry_support in (SUPPORTED, PARTIALLY_SUPPORTED):
        parse_started = time.perf_counter()
        geometry = adapter.geometry(data)
        parse_ms = (time.perf_counter() - parse_started) * 1000.0

        if geometry.geometry_status == INCOMPLETE:
            detail = (
                geometry.incomplete.explanation
                if geometry.incomplete is not None
                else "This file does not contain enough geometry to measure a volume."
            )
            capabilities = _weaken(
                capabilities, (GEOMETRY, MASS, COSTING), REQUIRES_USER_INPUT, detail
            )
        elif adapter.source_format == "iges":
            # The volume question the format-level declaration could not answer
            # is now settled: the surfaces enclose a solid, so mass and cost
            # rest on a verified volume.
            sewn = geometry.repair_applied
            for capability in (MASS, COSTING):
                capabilities = _replace(
                    capabilities,
                    capability,
                    SUPPORTED,
                    "The file's surfaces were sewn into a closed solid, so the volume is "
                    "integrated over exact surfaces bounding a verified solid."
                    if sewn
                    else "The file carried a solid outright, so this works exactly as it does "
                    "for STEP.",
                )

            # Features are deliberately *not* refined here. Closure does not
            # settle that question — a surface-mode export loses analytic
            # surface identity as well as topology, and sewing restores the
            # topology but cannot make a spline a cylinder again. The adapter
            # settles it during inspection instead, by reading which entity
            # types the file declares, so the answer is the same whether or not
            # geometry was parsed.

    assembly = None
    if with_assembly:
        assembly = decompose_assembly(data, filename, adapter, structure=assembly_structure)
        capabilities = [
            assembly.capabilities[0]
            if report.capability == ASSEMBLY and assembly.capabilities
            else report
            for report in capabilities
        ]
        warnings = warnings + list(assembly.warnings)

    components = list(inspection.components)
    if not components and geometry is not None:
        # A part file is one component, and that is the truth about it rather
        # than a placeholder. An adapter that can resolve an assembly fills
        # this in itself.
        components = [
            CADComponent(
                component_id="root",
                name=Path(filename or "part").stem or "part",
                kind="assembly" if inspection.is_assembly else "part",
                geometry=geometry,
                note=(
                    inspection.assembly_note
                    if inspection.is_assembly
                    else "A single part: the document's geometry is this component's geometry."
                ),
            )
        ]

    return CADDocument(
        filename=filename,
        extension=_extension_of(filename),
        source_format=adapter.source_format,
        format_label=adapter.label,
        representation=inspection.representation,
        representation_note=inspection.representation_note,
        units=inspection.units,
        unit_source=inspection.unit_source,
        unit_note=inspection.unit_note,
        components=components,
        is_assembly=inspection.is_assembly,
        assembly_note=inspection.assembly_note,
        structure_state=inspection.structure_state,
        assembly=assembly,
        geometry=geometry,
        metadata=inspection.metadata,
        capabilities=capabilities,
        warnings=warnings,
        detect_ms=detect_ms,
        parse_ms=parse_ms,
    )


def decompose_assembly(
    data: bytes,
    filename: str,
    adapter: CADAdapter,
    *,
    measure_components: bool = True,
    structure=None,
) -> CADAssembly:
    """Read a file's component structure into the normalized representation.

    Separate from `import_cad_document` because it costs a different order of
    magnitude: identifying the 198 MB export takes about 1.4 seconds and
    decomposing it about 20, so it runs when it is asked for and not before.

    Component geometry is measured **per component**, from that component's own
    shape. Where the file carries no shape for a component — the real Creo
    export is entirely like this, because OpenCASCADE rejects its
    schema-invalid `FACETED_BREP` entities — the component is named, counted and
    placed with `geometry_status='no_geometry_in_file'` and no measurements at
    all. The assembly's aggregate figures are never substituted for a
    component's, because that would report every one of 43 components as
    weighing what the whole assembly weighs.
    """
    from cad_costing_agent.cad_assembly import read_step_assembly
    from cad_costing_agent.cad_geometry import geometry_from_shape

    started = time.perf_counter()

    if not adapter.reads_assembly_structure:
        return CADAssembly(
            assembly_id=Path(filename or "model").stem or "model",
            name=Path(filename or "model").stem or "model",
            source_format=adapter.source_format,
            structure_state=AGGREGATE_ONLY,
            capabilities=[
                report
                for report in adapter.declared_capabilities()
                if report.capability == ASSEMBLY
            ],
            warnings=[
                ImportWarning(
                    code="no_structure_in_format",
                    message=(
                        f"{adapter.label} carries no component structure, so there is nothing "
                        "to decompose. The file is one body of geometry and is measured and "
                        "costed as one part."
                    ),
                )
            ],
            read_ms=(time.perf_counter() - started) * 1000.0,
        )

    # A caller that already read the structure passes it in — the viewer mesh
    # and the component feature list both need the shapes, and reading a 198 MB
    # export twice in one request would double a twenty-second cost for nothing.
    if structure is None:
        structure = read_step_assembly(data)

    definitions: list[ComponentDefinition] = []
    for definition in structure.definitions.values():
        geometry = None
        status = "no_geometry_in_file"
        note = (
            "This component has no geometry the reader could transfer, so it is named, "
            "counted and placed but not measured."
        )
        if definition.is_assembly:
            status = "not_measured"
            note = (
                "A sub-assembly is not measured in its own right; its components are, and "
                "measuring it as well would count the same material twice."
            )
        elif definition.has_geometry and measure_components:
            try:
                geometry = geometry_from_shape(definition.shape)
                status = "measured"
                note = "Measured from this component's own exact surfaces."
            except Exception as exc:  # noqa: BLE001 - one bad component must not sink the rest
                status = "failed"
                note = (
                    f"This component's geometry could not be measured ({type(exc).__name__}). "
                    "It is reported as unmeasured rather than given the assembly's figures."
                )
        elif definition.has_geometry:
            status = "not_measured"
            note = "Geometry is present but measurement was not requested."

        definitions.append(
            ComponentDefinition(
                definition_id=definition.definition_id,
                name=definition.name,
                part_number=definition.name or None,
                is_assembly=definition.is_assembly,
                quantity=definition.occurrence_count,
                geometry=geometry,
                geometry_status=status,
                geometry_note=note,
                face_count=definition.face_count,
                solid_count=definition.solid_count,
                material=None,
                source_reference=definition.definition_id,
                capabilities=_component_capabilities(definition, status),
            )
        )

    occurrences = [
        ComponentOccurrence(
            occurrence_id=occurrence.occurrence_id,
            definition_id=occurrence.definition_id,
            parent_occurrence_id=occurrence.parent_occurrence_id,
            instance_name=occurrence.name,
            depth=occurrence.depth,
            transform=ComponentTransform(
                local=list(occurrence.placement.local), world=list(occurrence.placement.world)
            ),
        )
        for occurrence in structure.occurrences
    ]

    warnings = [
        ImportWarning(code="assembly_structure", message=note) for note in structure.notes
    ]

    state = STRUCTURE_AVAILABLE if structure.is_assembly else AGGREGATE_ONLY
    if not structure.is_assembly and not structure.definitions:
        state = STRUCTURE_NOT_READABLE

    assembly_capability = _assembly_capability(structure, state)

    return CADAssembly(
        assembly_id=structure.root_definition_ids[0] if structure.root_definition_ids else "root",
        name=structure.root_name or (Path(filename or "model").stem or "model"),
        source_format=adapter.source_format,
        structure_state=state,
        definitions=definitions,
        occurrences=occurrences,
        root_definition_ids=list(structure.root_definition_ids),
        max_depth=structure.max_depth,
        truncated=structure.truncated,
        capabilities=[assembly_capability],
        warnings=warnings,
        read_ms=(time.perf_counter() - started) * 1000.0,
    )


def _component_capabilities(definition, status: str) -> list[CapabilityReport]:
    """What can be done with one component, on the same four-level vocabulary."""
    if status == "measured":
        return [
            CapabilityReport(capability=GEOMETRY, support=SUPPORTED),
            CapabilityReport(capability=MASS, support=SUPPORTED),
            CapabilityReport(capability=SURFACE_AREA, support=SUPPORTED),
            CapabilityReport(capability=FEATURES, support=SUPPORTED),
            CapabilityReport(capability=VIEWER, support=SUPPORTED),
            CapabilityReport(capability=COSTING, support=SUPPORTED),
        ]

    if definition.is_assembly:
        reason = (
            "This is a sub-assembly, not a part. It is not measured or costed in its own "
            "right; its components are."
        )
    else:
        reason = (
            "The file carries no geometry for this component that the reader could transfer, "
            "so it cannot be measured, drawn, analysed or costed. It is still named, counted "
            "and placed. The usual cause is a faceted export whose FACETED_BREP entities sit "
            "on an OPEN_SHELL, which ISO 10303-42 forbids."
        )
    return [
        CapabilityReport(capability=capability, support=UNSUPPORTED, detail=reason)
        for capability in (GEOMETRY, MASS, SURFACE_AREA, FEATURES, VIEWER, COSTING)
    ]


def _assembly_capability(structure, state: str) -> CapabilityReport:
    if state != STRUCTURE_AVAILABLE:
        return CapabilityReport(
            capability=ASSEMBLY,
            support=UNSUPPORTED,
            detail=(
                "This file contains no assembly structure — it describes a single part. A "
                "part made of several solid bodies is still a part."
            ),
        )

    measurable = structure.definitions_with_geometry
    total = len(structure.part_definitions)
    if measurable == total and total:
        return CapabilityReport(
            capability=ASSEMBLY,
            support=PARTIALLY_SUPPORTED,
            detail=(
                f"All {total} component definitions were measured individually. What remains "
                "unimplemented is assembly-level manufacturing: no assembly labour, fastening "
                "time, purchased-part handling or make/buy classification. An assembly total "
                "is the sum of component cost x quantity and nothing more."
            ),
        )
    return CapabilityReport(
        capability=ASSEMBLY,
        support=PARTIALLY_SUPPORTED,
        detail=(
            f"The structure was recovered in full, but only {measurable} of {total} component "
            "definitions carry geometry the reader could transfer, so the rest can be named, "
            "counted and placed but not measured or costed. They are reported as uncosted "
            "with that reason rather than counted as zero."
        ),
    )


def require(document: CADDocument, capability: str) -> CapabilityReport:
    """Assert that a document can do something, or refuse with its own reason.

    The refusal carries the adapter's explanation rather than a generic one, so
    a user who uploads a Creo drawing is told what a `.drw` file is and why it
    has no volume, instead of "unsupported format".
    """
    report = document.capability(capability)
    if report.support == UNSUPPORTED:
        raise UnsupportedFormatError(
            report.detail,
            source_format=document.source_format,
            format_label=document.format_label,
        )
    return report


def format_catalog() -> list[FormatDescription]:
    """The capability matrix for every format, with no file involved.

    Serves `/cad/formats`, which the upload UI renders. Because the UI reads
    this rather than its own list, an extension can never be advertised in the
    interface without a matching declaration here.
    """
    catalog: list[FormatDescription] = []
    for adapter in ADAPTERS:
        capabilities = adapter.declared_capabilities()
        supports = {report.capability: report.support for report in capabilities}
        catalog.append(
            FormatDescription(
                source_format=adapter.source_format,
                label=adapter.label,
                extensions=list(adapter.extensions),
                headline=adapter.headline,
                unit_source=adapter.unit_source,
                unit_note=adapter.unit_note,
                capabilities=capabilities,
                any_support=any(level != UNSUPPORTED for level in supports.values()),
                native_integration_required=adapter.source_format.startswith("creo_"),
            )
        )
    return catalog
