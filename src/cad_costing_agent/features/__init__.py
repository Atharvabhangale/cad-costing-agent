"""Deterministic CAD feature recognition.

    CAD file → B-Rep → topology scan → recognizers → normalized features

This branches from geometry; it does not replace it. The costing path is
untouched and independent:

    CAD → geometry → mass → costing        (unchanged)
    CAD → geometry → features              (this package)

**STEP-first.** Recognition needs exact faces, edges and surface types. STEP
carries them; STL is a bag of triangles with no notion of a cylinder, so a hole
in an STL is indistinguishable from a many-sided prism. Rather than guess,
feature recognition rejects STL outright. STL remains fully supported for
geometry, the viewer and costing.

**No operations are inferred.** A hole is reported as a hole, not as a drilling
operation. Turning features into manufacturing operations is a later step, and
doing it here would bury a modelling decision inside a geometry routine.

**Nothing is learned or guessed.** No model, no randomness, no external
service. Every feature names the rule that produced it and lists the geometric
conditions that were checked, and the same file always produces the same list.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from cad_costing_agent import limits
from cad_costing_agent.cad_geometry import (
    IGES_FORMATS,
    STEP_FORMATS,
    STL_FORMATS,
    SUPPORTED_FORMATS,
)
from cad_costing_agent.cad_faceted import parse_faceted_step
from cad_costing_agent.features.mesh_recognition import recognize_mesh_features
from cad_costing_agent.features.model import (
    Feature,
    FeatureRecognitionResult,
    FeatureType,
    SolidFeatures,
)
from cad_costing_agent.features.recognizers import DEFAULT_RECOGNIZERS, RecognitionContext
from cad_costing_agent.features.shape_loading import LoadedShape, NotExact, load_exact_shape
from cad_costing_agent.features.step_text import (
    EMPTY,
    WIREFRAME,
    StepRepresentation,
)
from cad_costing_agent.features.topology import TopologyModel, build_topology

# Recognition is O(faces) per recognizer plus one grouping pass, but a model
# with tens of thousands of faces is beyond what this POC is meant to analyse
# and would produce a feature list nobody could read. Both budgets are
# configurable and are deliberately separate from the upload and viewer limits.
MAX_FACES = limits.MAX_BREP_FACES
MAX_FACETS = limits.MAX_FACETS

_ID_PREFIXES = {
    FeatureType.HOLE: "H",
    FeatureType.CYLINDER: "CYL",
    FeatureType.PLANE: "PL",
    FeatureType.POCKET: "PKT",
    FeatureType.SLOT: "SLT",
    FeatureType.FILLET: "FIL",
    FeatureType.CHAMFER: "CHM",
}

# The order features are listed in, most manufacturing-relevant first. Within a
# type the sort falls back to solid then lowest source face, both of which are
# stable for a given file, so the whole list is deterministic.
_TYPE_ORDER = {
    FeatureType.HOLE: 0,
    FeatureType.POCKET: 1,
    FeatureType.SLOT: 2,
    FeatureType.CYLINDER: 3,
    FeatureType.FILLET: 4,
    FeatureType.CHAMFER: 5,
    FeatureType.PLANE: 6,
}


def _sort_key(feature: Feature):
    return (
        _TYPE_ORDER.get(feature.feature_type, 99),
        feature.solid_index,
        feature.source_faces[0] if feature.source_faces else 0,
    )


def _assign_ids(features: list[Feature]) -> list[Feature]:
    """Number features after sorting, so ids are stable for a given file."""
    counters: dict[FeatureType, int] = {}
    numbered: list[Feature] = []
    for feature in features:
        counters[feature.feature_type] = counters.get(feature.feature_type, 0) + 1
        prefix = _ID_PREFIXES.get(feature.feature_type, "F")
        numbered.append(
            feature.model_copy(update={"feature_id": f"{prefix}-{counters[feature.feature_type]:03d}"})
        )
    return numbered


def describe_feature(feature: Feature) -> str:
    """One readable line for a feature, e.g. "hole (through) Ø10 mm depth 20 mm".

    Used to group identical features into a summary line, and reused by the
    operation-suggestion layer so a feature is described the same way wherever
    it appears.
    """
    parts = [feature.feature_type.value]
    if feature.subtype:
        parts.append(f"({feature.subtype})")
    dimensions = feature.dimensions
    if "diameter_mm" in dimensions:
        parts.append(f"Ø{dimensions['diameter_mm']:g} mm")
    if "length_mm" in dimensions and "width_mm" in dimensions:
        parts.append(f"{dimensions['length_mm']:g} × {dimensions['width_mm']:g} mm")
    elif "length_mm" in dimensions:
        parts.append(f"length {dimensions['length_mm']:g} mm")
    if "depth_mm" in dimensions:
        parts.append(f"depth {dimensions['depth_mm']:g} mm")
    if "radius_mm" in dimensions and "diameter_mm" not in dimensions:
        parts.append(f"R{dimensions['radius_mm']:g} mm")
    if "angle_deg" in dimensions:
        parts.append(f"{dimensions['angle_deg']:g}°")
    if "area_mm2" in dimensions and len(parts) <= 2:
        parts.append(f"area {dimensions['area_mm2']:g} mm²")
    return " ".join(parts)


def _summarize(features: list[Feature]) -> list[str]:
    """Group identical descriptions into "4 × hole (through) Ø10 mm" lines."""
    grouped: dict[tuple[str, str], int] = {}
    order: list[tuple[str, str]] = []
    for feature in features:
        key = (describe_feature(feature), feature.confidence.value)
        if key not in grouped:
            order.append(key)
        grouped[key] = grouped.get(key, 0) + 1
    return [
        f"{count} × {description} — confidence {confidence}"
        for (description, confidence) in order
        for count in [grouped[(description, confidence)]]
    ]


def recognize_features(
    shape,
    *,
    filename: str = "",
    source_format: str = "step",
    recognizers=DEFAULT_RECOGNIZERS,
    topology: TopologyModel | None = None,
) -> FeatureRecognitionResult:
    """Run the recognizers over an already-parsed B-Rep shape.

    A recognizer that raises is recorded as a warning and skipped rather than
    aborting the run: a model the pocket rule chokes on should still report its
    holes. `topology` is the shape's scan when the caller already made one, so
    the shape is not traversed twice.
    """
    started = time.perf_counter()
    if topology is None:
        topology = build_topology(shape)
    warnings = list(topology.warnings)

    if not topology.faces:
        # Reached by a real file: a 198 MB Creo export made entirely of
        # FACETED_BREP entities reads without error but transfers no faces at
        # all. Returning an empty feature list would look like "your part has
        # no features" when the truth is that there was no B-Rep to look at.
        raise ValueError(
            "This STEP file was read successfully but contains no B-Rep faces to analyse. "
            "The usual cause is a faceted or tessellated export (FACETED_BREP) rather than "
            "solid geometry: such a file describes the part as loose polygons, which carry "
            "no cylinders, planes or edges for a recognizer to work from. Re-export from "
            "your CAD system as solid/B-Rep geometry rather than a facet or visualisation "
            "export."
        )

    if len(topology.faces) > MAX_FACES:
        raise ValueError(
            f"This model has {len(topology.faces):,} faces, above the {MAX_FACES:,} that "
            "feature recognition will analyse. Geometry, the 3D preview and costing are "
            "unaffected."
        )

    context = RecognitionContext(topology=topology)
    collected: list[Feature] = []
    ran: list[str] = []

    for recognizer in recognizers:
        try:
            collected.extend(recognizer.recognize(context))
            ran.append(recognizer.name)
        except Exception as exc:  # noqa: BLE001 - one rule must not sink the rest
            warnings.append(
                f"The {recognizer.name} recognizer failed on this model "
                f"({type(exc).__name__}); its features are missing from this result."
            )

    features = _assign_ids(sorted(collected, key=_sort_key))

    counts: dict[str, int] = {}
    for feature in features:
        counts[feature.feature_type.value] = counts.get(feature.feature_type.value, 0) + 1

    solids: list[SolidFeatures] = []
    for solid in topology.solids:
        mine = [feature for feature in features if feature.solid_index == solid.index]
        per_type: dict[str, int] = {}
        for feature in mine:
            per_type[feature.feature_type.value] = per_type.get(feature.feature_type.value, 0) + 1
        solids.append(
            SolidFeatures(
                solid_index=solid.index,
                face_count=len(solid.face_indices),
                edge_count=len(solid.edge_indices),
                volume_mm3=round(solid.volume, 6),
                features=mine,
                feature_counts=per_type,
            )
        )

    return FeatureRecognitionResult(
        filename=filename,
        source_format=source_format,
        solid_count=len(topology.solids),
        face_count=len(topology.faces),
        edge_count=len(topology.edges),
        solids=solids,
        features=features,
        feature_counts=counts,
        unclassified_face_count=len(topology.faces) - len(context.claimed_faces),
        summary=_summarize(features),
        recognizers=ran,
        warnings=warnings,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
    )


def recognize_faceted_features(
    data: bytes, filename: str, representation: StepRepresentation
) -> FeatureRecognitionResult:
    """Reconstruct features from a faceted STEP export.

    A different file format needs a different rule set, but not a different
    output: this returns the same `Feature` objects the analytic path does,
    distinguished only by `source_type="faceted"` and by what the rules were
    honestly able to recover.
    """
    started = time.perf_counter()

    if representation.facet_count > MAX_FACETS:
        # The diagnosis is the useful part of the refusal: it tells the user
        # what their file is and what to do about it, which a bare size limit
        # would not.
        raise ValueError(
            f"This faceted model has {representation.facet_count:,} polygons across "
            f"{representation.shell_count:,} bodies, above the {MAX_FACETS:,} that feature "
            "reconstruction will analyse. A model of this size is an assembly rather than a "
            "single part, and fitting features across every body would produce a dump rather "
            "than a feature list. Geometry and costing are unaffected. "
            + " ".join(representation.notes)
        )

    model = parse_faceted_step(data, max_facets=MAX_FACETS)
    warnings = list(representation.notes) + list(model.warnings)

    if not model.shells:
        raise ValueError(
            "No polygons could be read from this faceted STEP file, so there is nothing to "
            "reconstruct features from."
        )

    features = _assign_ids(sorted(recognize_mesh_features(model.shells), key=_sort_key))

    counts: dict[str, int] = {}
    for feature in features:
        counts[feature.feature_type.value] = counts.get(feature.feature_type.value, 0) + 1

    solids: list[SolidFeatures] = []
    for shell in model.shells:
        mine = [feature for feature in features if feature.solid_index == shell.index]
        per_type: dict[str, int] = {}
        for feature in mine:
            per_type[feature.feature_type.value] = per_type.get(feature.feature_type.value, 0) + 1
        solids.append(
            SolidFeatures(
                solid_index=shell.index,
                face_count=0,
                edge_count=0,
                # A faceted shell is not guaranteed closed, so no volume is
                # claimed from it rather than reporting a meaningless number.
                volume_mm3=0.0,
                features=mine,
                feature_counts=per_type,
            )
        )

    claimed = {index for feature in features for index in feature.source_facets}
    warnings.append(
        "Pockets, slots, fillets and chamfers are not reconstructed from faceted geometry: on "
        "polygons they cannot be told apart from ordinary flat regions and coarse curves "
        "reliably enough to claim."
    )

    return FeatureRecognitionResult(
        filename=filename,
        source_format="step",
        representation="faceted",
        units="as written in the file",
        solid_count=len(model.shells),
        face_count=0,
        edge_count=0,
        facet_count=model.facet_count,
        solids=solids,
        features=features,
        feature_counts=counts,
        unclassified_face_count=max(0, model.triangle_count - len(claimed)),
        summary=_summarize(features),
        recognizers=["faceted_cylinder", "faceted_plane"],
        warnings=warnings,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
    )


SURFACE_MODEL_WARNING = (
    "This IGES file stores its geometry as loose trimmed surfaces rather than as a solid. "
    "Such an export loses analytic surface identity as well as topology: a cylinder is "
    "rewritten as a generic surface of revolution, which the hole and fillet recognizers "
    "cannot match. Holes, pockets, fillets and chamfers are therefore MISSING FROM THIS "
    "RESULT rather than absent from the part, and only planar faces can be reported. "
    "Re-export the IGES with solid/B-Rep geometry, or export STEP instead, to analyse them."
)

NO_SOLID_WARNING = (
    "The surfaces in this IGES file do not close into a solid either, so the recognizers had "
    "no inside or outside to work from at all."
)


def _recognize_iges_features(loaded: LoadedShape, filename: str) -> "RecognizedShape":
    """Recognize features in an IGES file, from the shape that was measured.

    Recognition runs on whatever `analyse_iges_shape` established: the solid,
    if the file carried one or its surfaces sewed into one, and the loose
    surfaces otherwise. That matters because the two give genuinely different
    answers — measured on a real 43-face part, the solid form yields 39
    features including six through holes and their fillets, and the
    surface-only form yields 22 planes and nothing else. The degraded case is
    therefore *reported* as degraded rather than left looking like a part with
    no holes in it.
    """
    topology = loaded.iges
    if loaded.reason in (NotExact.IGES_NO_SHAPE, NotExact.IGES_NO_FACES):
        raise ValueError(
            "This IGES file was read but contains no surfaces to analyse. "
            + " ".join(topology.notes)
        )

    result = recognize_features(loaded.shape, filename=filename, source_format="iges")

    # A file that brought its own solid keeps every analytic surface, and
    # recognition matches STEP exactly. Anything else came in as loose
    # surfaces, and the warning goes first — whether or not sewing managed to
    # close them, because closing them does not bring a cylinder back.
    if loaded.reason is not NotExact.IGES_SEWN:
        return RecognizedShape(
            result.model_copy(
                update={
                    "representation": "brep",
                    "warnings": list(result.warnings) + list(topology.notes),
                }
            ),
            loaded.shape,
        )

    warnings = [SURFACE_MODEL_WARNING]
    if not topology.is_solid:
        warnings.append(NO_SOLID_WARNING)

    return RecognizedShape(
        result.model_copy(
            update={
                "representation": "surface_model",
                "warnings": warnings + list(result.warnings) + list(topology.notes),
            }
        ),
        loaded.shape,
    )


def recognize_component_features(
    shape, *, component_id: str, component_name: str = "", source_format: str = "step"
) -> FeatureRecognitionResult:
    """Recognize features on one component of an assembly.

    The recognizers are unchanged and run on the component's own shape, so a
    component gets the same analysis it would get as a standalone part. What
    this adds is identity: every feature is stamped with its component and its
    id is prefixed with it, because two components each having an `H-001` would
    make an assembly-wide feature list ambiguous and would break the link back
    from an operation to the hole that caused it.
    """
    result = recognize_features(shape, filename=component_name or component_id, source_format=source_format)

    features = [
        feature.model_copy(
            update={
                "component_id": component_id,
                "feature_id": f"{component_id}/{feature.feature_id}",
            }
        )
        for feature in result.features
    ]
    solids = [
        solid.model_copy(
            update={
                "features": [
                    feature
                    for feature in features
                    if feature.solid_index == solid.solid_index
                ]
            }
        )
        for solid in result.solids
    ]
    return result.model_copy(
        update={
            "component_id": component_id,
            "component_name": component_name or component_id,
            "features": features,
            "solids": solids,
        }
    )


@dataclass(frozen=True)
class RecognizedShape:
    """A recognition result and the exact B-Rep it was recognized from.

    For an analysis that must run on the same shape recognition read (wall
    thickness), so the file is parsed once and by the one reader. `shape` is
    None when no exact B-Rep was read: a faceted export is reconstructed from
    polygons instead.
    """

    recognition: FeatureRecognitionResult
    shape: object | None


def recognize_features_from_bytes(data: bytes, filename: str) -> FeatureRecognitionResult:
    """Recognize features in an uploaded STEP or IGES file (see `recognize_features_with_shape`)."""
    return recognize_features_with_shape(data, filename).recognition


def _canonical_format(file_format: str) -> str:
    """The loader's name for a file suffix: `step`, `iges`, or the suffix itself."""
    if file_format in STEP_FORMATS:
        return "step"
    if file_format in IGES_FORMATS:
        return "iges"
    return file_format


def recognize_features_with_shape(data: bytes, filename: str) -> RecognizedShape:
    """Recognize features in an uploaded STEP or IGES file, keeping the shape read.

    The shape comes from `load_exact_shape`, which classifies a STEP file
    before anything heavy is attempted, because what it contains decides which
    rules can apply — and because a 200 MB export that cannot be used takes a
    second to rule out this way and twenty to rule out by loading it.

    Raises ValueError for an unsupported format, empty input, or content that
    its own format's reader cannot read.
    """
    file_format = Path(filename or "").suffix.strip().lower().lstrip(".")
    source_format = _canonical_format(file_format)

    if source_format in ("step", "iges") and not data:
        raise ValueError("CAD file is empty.")

    loaded = load_exact_shape(data, source_format)

    if loaded.reason is NotExact.NOT_A_BREP_FORMAT:
        if file_format in STL_FORMATS:
            raise ValueError(
                f"Feature recognition needs the exact faces and edges of a B-Rep model, and "
                f"'{file_format}' is a triangle mesh that does not carry them — a hole in a "
                "mesh is indistinguishable from a many-sided prism. Upload the STEP version "
                "of this part. Geometry, the 3D preview and costing still work for this file."
            )
        supported = ", ".join(sorted(STEP_FORMATS | IGES_FORMATS))
        raise ValueError(
            f"Unsupported CAD file format '{file_format or filename}' for feature "
            f"recognition. Supported formats: {supported}"
        )

    if source_format == "iges":
        return _recognize_iges_features(loaded, filename)

    if loaded.reason is NotExact.FACETED_STEP:
        return RecognizedShape(
            recognize_faceted_features(data, filename, loaded.step_representation), None
        )

    if loaded.reason is NotExact.WIREFRAME_STEP:
        raise ValueError(
            "This STEP file contains no surface geometry to analyse — "
            + " ".join(loaded.step_representation.notes)
        )

    return RecognizedShape(
        recognize_features(loaded.shape, filename=filename, source_format="step"), loaded.shape
    )


def analyse_sheet_metal_from_bytes(
    data: bytes,
    filename: str,
    *,
    k_factor: float | None = None,
    sheet_excluders=None,
):
    """Measure an uploaded part as sheet metal: thickness, bends, flat pattern.

    Exact B-Rep only. The shape is read once, by `load_exact_shape`, and
    scanned once; that scan feeds both the recognizers and the sheet
    measurement. A faceted export, an IGES surface model and an STL carry no
    exact cylinders to measure a bend radius from, so each is refused with the
    reason rather than approximated.

    `sheet_excluders` is a function from the recognized features to those that
    cutting and bending sheet cannot produce. The caller hands in the route
    classifier's own rule, so the two cannot disagree and this package imports
    nothing above it.
    """
    from cad_costing_agent.features.sheet_metal import analyse_sheet_metal, validate_k_factor

    k = validate_k_factor(k_factor)
    file_format = Path(filename or "").suffix.strip().lower().lstrip(".")
    if not data:
        raise ValueError("CAD file is empty.")

    source_format = _canonical_format(file_format)
    loaded = load_exact_shape(data, source_format)

    if loaded.reason is NotExact.FACETED_STEP:
        raise ValueError(
            "This STEP file is a faceted export: flat polygons, with no exact cylinders to "
            "measure a bend radius from or unfold. Sheet-metal measurement needs analytic "
            "B-Rep; re-export the part as solid geometry. "
            + " ".join(loaded.step_representation.notes)
        )
    if loaded.reason is NotExact.WIREFRAME_STEP:
        raise ValueError(
            "This STEP file contains no surface geometry to analyse — "
            + " ".join(loaded.step_representation.notes)
        )
    if loaded.reason in (NotExact.IGES_NO_SHAPE, NotExact.IGES_SEWN):
        raise ValueError(
            "This IGES file stores loose trimmed surfaces rather than a solid. Such an export "
            "rewrites cylinders as generic surfaces, so a bend radius cannot be measured from "
            "it. Re-export with solid/B-Rep geometry, or export STEP."
        )
    if loaded.reason is NotExact.NOT_A_BREP_FORMAT:
        if file_format in STL_FORMATS:
            raise ValueError(
                f"Sheet-metal measurement needs the exact faces of a B-Rep model, and '{file_format}' "
                "is a triangle mesh: a bend in a mesh is a run of flat facets with no radius. Upload "
                "the STEP version of this part."
            )
        supported = ", ".join(sorted(STEP_FORMATS | IGES_FORMATS))
        raise ValueError(
            f"Unsupported CAD file format '{file_format or filename}' for sheet-metal "
            f"measurement. Supported formats: {supported}"
        )

    # `IGES_NO_FACES` is left to the scan below, which words a shape with no
    # faces the way it always has.
    shape = loaded.shape
    topology = build_topology(shape)
    recognition = recognize_features(
        shape, filename=filename, source_format=source_format, topology=topology
    )
    excluders = (
        [(feature.feature_id, describe_feature(feature)) for feature in sheet_excluders(recognition.features)]
        if sheet_excluders is not None
        else []
    )
    return analyse_sheet_metal(
        topology,
        k_factor=k,
        sheet_excluders=excluders,
        filename=filename,
        source_format=source_format,
    )


__all__ = [
    "LoadedShape",
    "NotExact",
    "analyse_sheet_metal_from_bytes",
    "FeatureRecognitionResult",
    "MAX_FACES",
    "NO_SOLID_WARNING",
    "RecognizedShape",
    "SURFACE_MODEL_WARNING",
    "describe_feature",
    "load_exact_shape",
    "recognize_component_features",
    "recognize_features",
    "recognize_features_from_bytes",
    "recognize_features_with_shape",
]
