"""The application-level feature vocabulary.

Nothing in this module imports OpenCASCADE. It is the boundary the rest of the
application sees: recognizers produce these types, and the API, the UI and any
later step consume them without needing to understand B-Rep topology or OCP
classes.

Design notes
------------
**Dimensions are a dictionary, not a fixed set of fields.** A through hole has
a diameter and a depth; a pocket has a length, width and depth; a plane has an
area. Modelling every possible measurement as an optional column would produce
a wall of nulls and would have to change every time a recognizer learns to
measure something new. Instead each feature carries only the dimensions that
were actually determined, keyed by documented names ending in their unit
(`diameter_mm`, `depth_mm`, `area_mm2`, `angle_deg`). A missing key means "not
determinable from the geometry", which is information in itself.

**Every feature explains itself.** `detection_method` names the rule that fired
and `evidence` lists the individual geometric conditions that were checked.
There are no unexplained feature claims: if a recognizer cannot say why it
believes something, it does not emit it.

**Confidence is deterministic, not statistical.** See `Confidence`.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# Coordinates and measurements are rounded to this many decimals before being
# reported. The reader normalizes STEP to millimetres, so six decimals is a
# nanometre — far below any manufacturing relevance — and it keeps the JSON
# byte-identical between runs instead of exposing floating-point noise.
OUTPUT_DECIMALS = 6


class FeatureType(str, Enum):
    """The controlled vocabulary.

    Deliberately small. Every member here is something the recognizers can
    identify from explicit geometric evidence; speculative types are not added
    until a rule exists that can support them. New members can be appended
    without changing any consumer, because consumers switch on the string.
    """

    HOLE = "hole"
    CYLINDER = "cylinder"
    PLANE = "plane"
    POCKET = "pocket"
    SLOT = "slot"
    FILLET = "fillet"
    CHAMFER = "chamfer"


class Confidence(str, Enum):
    """How completely the geometric evidence supported the classification.

    This is **not** a probability and is not derived from any model. It is a
    three-way deterministic classification of how much of a recognizer's rule
    was satisfied directly by the B-Rep:

    - ``high``   — every condition the rule requires was checked and satisfied,
                   and every reported dimension was measured from the geometry.
    - ``medium`` — the feature matched, but at least one reported property was
                   *inferred* rather than measured (for example a depth taken
                   from a bounding extent because no capping face exists), or a
                   secondary condition held only approximately.
    - ``low``    — a candidate exists and the primary evidence holds, but the
                   classification itself remains ambiguous (for example a bore
                   whose ends could not be classified as open or closed).

    The same file always yields the same confidence for the same feature.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Point3D(BaseModel):
    """A point or direction in the model's own millimetre coordinates."""

    model_config = ConfigDict(frozen=True)

    x: float
    y: float
    z: float

    @classmethod
    def of(cls, x: float, y: float, z: float) -> "Point3D":
        return cls(
            x=round(x, OUTPUT_DECIMALS),
            y=round(y, OUTPUT_DECIMALS),
            z=round(z, OUTPUT_DECIMALS),
        )

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


class Feature(BaseModel):
    """One recognized feature, traceable back to the CAD faces it came from."""

    model_config = ConfigDict(frozen=True)

    feature_id: str = Field(
        description=(
            "Stable within one recognition run, e.g. 'H-001'. Inside an assembly it is "
            "prefixed with the component it belongs to — 'HOUSING/H-001' — so ids stay unique "
            "across components that each have their own first hole."
        )
    )
    component_id: str | None = Field(
        default=None,
        description=(
            "The assembly component this feature belongs to, or null for a single part. "
            "Carrying it here is what keeps component -> feature -> operation traceable once "
            "an assembly is decomposed."
        ),
    )
    feature_type: FeatureType
    subtype: str | None = Field(
        default=None,
        description=(
            "Refinement within the type, e.g. 'through', 'blind', 'counterbore', "
            "'countersink' for holes; 'bore'/'shaft' for cylinders; 'concave'/'convex' "
            "for fillets."
        ),
    )
    solid_index: int = Field(description="Which solid of the STEP file this belongs to, 0-based.")
    source_type: str = Field(
        default="brep",
        description=(
            "Which representation this was recovered from. 'brep' means exact surfaces were "
            "read from the file, so a diameter is a stored number. 'faceted' means it was "
            "reconstructed by fitting to polygons, so a diameter is a measurement."
        ),
    )
    dimensions: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Only the measurements that were determinable. Keys carry their unit: "
            "diameter_mm, radius_mm, depth_mm, length_mm, width_mm, area_mm2, angle_deg."
        ),
    )
    location: Point3D | None = Field(
        default=None, description="Representative point: hole entry, face centre, pocket floor centre."
    )
    axis: Point3D | None = Field(
        default=None, description="Unit direction: hole/cylinder axis, or the outward normal of a plane."
    )
    source_faces: list[int] = Field(
        default_factory=list,
        description=(
            "1-based indices into the STEP shape's face map, stable for a given file. "
            "These are what a later step needs to highlight the feature in the viewer."
        ),
    )
    source_edges: list[int] = Field(
        default_factory=list, description="1-based indices into the shape's edge map."
    )
    source_facets: list[int] = Field(
        default_factory=list,
        description=(
            "Facet indices within the body, for features reconstructed from a faceted file. "
            "The faceted counterpart of `source_faces`: a faceted model has no CAD faces to "
            "point at, but the polygons are still traceable."
        ),
    )
    confidence: Confidence
    detection_method: str = Field(
        description="Identifier of the rule that fired, e.g. 'cylindrical_bore_with_open_ends'."
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="The individual geometric conditions that were checked and held.",
    )


class SolidFeatures(BaseModel):
    """Features belonging to one solid, so a multi-solid STEP stays separable."""

    solid_index: int
    face_count: int
    edge_count: int
    volume_mm3: float
    features: list[Feature]
    feature_counts: dict[str, int]


class FeatureRecognitionResult(BaseModel):
    """Everything `/cad/features` returns for one uploaded file.

    `elapsed_ms` is the only field that varies between runs on the same input;
    everything else is deterministic, and the determinism test excludes it.
    """

    filename: str
    source_format: str
    component_id: str | None = Field(
        default=None, description="Set when this result is for one component of an assembly."
    )
    component_name: str | None = None
    representation: str = Field(
        default="brep",
        description=(
            "What the file actually contained: 'brep' for exact surfaces, 'faceted' for a "
            "polygon export whose features had to be reconstructed."
        ),
    )
    units: str = Field(default="mm", description="STEP is normalized to millimetres by the reader.")
    solid_count: int
    face_count: int
    edge_count: int
    facet_count: int = Field(
        default=0, description="Polygons, for a faceted file. Zero for an analytic B-Rep."
    )
    solids: list[SolidFeatures]
    features: list[Feature] = Field(description="Every feature across every solid, flattened.")
    feature_counts: dict[str, int]
    unclassified_face_count: int = Field(
        default=0,
        description=(
            "Faces no recognizer claimed. Reported rather than hidden, so the share of the "
            "model that was not understood is visible."
        ),
    )
    summary: list[str] = Field(
        default_factory=list, description="Human-readable one-line-per-group summary."
    )
    recognizers: list[str] = Field(
        default_factory=list, description="Which recognizers ran, in order."
    )
    warnings: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0.0
