"""Advisory design-for-manufacturing (DFM) flags, from features already recognized.

    CAD -> features -> **DFM flags** (advice only)

A flag says "this measured geometry is past a general machining guideline, which
usually makes it slower or harder to machine". It is read-only advice:

- It changes **no** cost, time, suggestion or classification. This module imports
  `features.model` and nothing else from the package, so it cannot reach any of
  them; a structural test holds that, and holds that only `main.py`,
  `repository.py` and `db.py` import it.
- Every threshold lives in a **versioned, immutable `DfmProfile`**, seeded at
  startup and published (never edited) through the API, exactly like a time
  profile. A threshold either cites a published source (title + URL) or says, in
  words, that no published source was found. Nothing is presented as a standard.
- A rule runs only on a dimension something actually measures. Rules that
  would need internal pocket-corner radii (a pocket's rounded corners are
  claimed by the pocket and carry no radius) or feature-size limits for
  chamfers have **no rule** here, because there is no measurement. The minimum
  wall rule reads the wall-thickness measurement (`features.wall_thickness`),
  which `main.py` hands in as a plain `WallEvidence` carrier — this module never
  measures anything and never imports the measuring module.
- There is no severity score and no confidence. A flag's only "outcome" is the
  comparison it states: measured value, comparison, threshold. Feature evidence
  from polygons (`faceted`) is weaker than from exact surfaces, and a flag
  built on it says so.
- Deterministic: same features and profile, same flags in the same order.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from cad_costing_agent.features.model import Feature, FeatureType, Point3D

DFM_PROFILE_ID_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
VERSION_PATTERN = r"^\d+\.\d+\.\d+$"

DFM_DISCLAIMER = (
    "Advisory only. These flags compare measured geometry with general machining rules of "
    "thumb. They change no cost, no time, no suggested operation and no classification, and "
    "a flag is not a statement that the part cannot be made."
)

FACETED_NOTE = (
    "This feature was reconstructed by fitting to polygons, not read from an exact surface, "
    "so the measured value is weaker evidence than a B-Rep measurement."
)

BASIS_PUBLISHED = "published_guideline"
BASIS_NONE_FOUND = "no_published_source_found_illustrative"


class RuleId(str, Enum):
    DEEP_HOLE = "dfm_deep_hole"
    DEEP_NARROW_RECESS = "dfm_deep_narrow_pocket_or_slot"
    SMALL_INTERNAL_RADIUS = "dfm_small_internal_radius"
    MANY_HOLE_DIAMETERS = "dfm_many_distinct_hole_diameters"
    MIN_WALL = "dfm_min_wall"


RULE_NAMES = {
    RuleId.DEEP_HOLE: "Deep holes",
    RuleId.DEEP_NARROW_RECESS: "Deep, narrow pockets or slots",
    RuleId.SMALL_INTERNAL_RADIUS: "Small internal radii",
    RuleId.MANY_HOLE_DIAMETERS: "Many different hole sizes",
    RuleId.MIN_WALL: "Thin walls",
}


class DfmRuleSetting(BaseModel):
    """One threshold of a profile, with where it came from."""

    model_config = ConfigDict(frozen=True)

    rule_id: RuleId
    threshold: float = Field(gt=0)
    unit: str = Field(min_length=1, description="e.g. 'ratio', 'mm', 'count'.")
    basis: str = Field(description=f"'{BASIS_PUBLISHED}' or '{BASIS_NONE_FOUND}'.")
    source_title: str | None = None
    source_url: str | None = None
    source_quote: str | None = Field(
        default=None, description="The guideline as the source states it, for the record."
    )

    @model_validator(mode="after")
    def _basis_is_honest(self) -> "DfmRuleSetting":
        if self.basis not in (BASIS_PUBLISHED, BASIS_NONE_FOUND):
            raise ValueError(f"basis must be '{BASIS_PUBLISHED}' or '{BASIS_NONE_FOUND}'.")
        if self.basis == BASIS_PUBLISHED:
            if not (self.source_title and self.source_url):
                raise ValueError(
                    "A threshold with basis 'published_guideline' must carry source_title and "
                    "source_url. If there is no source, say so with "
                    f"'{BASIS_NONE_FOUND}' instead of presenting it as published."
                )
        return self


class DfmProfile(BaseModel):
    """One immutable version of a set of DFM thresholds."""

    model_config = ConfigDict(frozen=True)

    dfm_profile_id: str = Field(pattern=DFM_PROFILE_ID_PATTERN)
    name: str = Field(min_length=1)
    version: str = Field(pattern=VERSION_PATTERN)
    description: str = ""
    is_illustrative: bool = True
    rules: list[DfmRuleSetting]

    @model_validator(mode="after")
    def _unique_rules(self) -> "DfmProfile":
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("A profile may set each rule at most once.")
        return self

    def setting(self, rule_id: RuleId) -> DfmRuleSetting | None:
        return next((rule for rule in self.rules if rule.rule_id == rule_id), None)


class DfmProfileVersionCreate(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(pattern=VERSION_PATTERN)
    description: str = ""
    is_illustrative: bool = True
    rules: list[DfmRuleSetting]


class DfmProfileSummary(BaseModel):
    dfm_profile_id: str
    name: str
    version: str
    description: str
    is_illustrative: bool


class WallEvidence(BaseModel):
    """What the wall-thickness measurement found, handed in by `main.py`.

    A plain carrier so this module never reaches the measuring code (which
    reads OpenCASCADE). Absent is not thin: when nothing was measured,
    `not_measured_reason` says why and the rule is reported as not evaluated.
    """

    model_config = ConfigDict(frozen=True)

    measured: bool
    not_measured_reason: str | None = None
    minimum_mm: float | None = None
    from_face: int | None = None
    to_face: int | None = None
    location: Point3D | None = Field(default=None, description="Halfway through the thinnest wall.")
    direction: Point3D | None = Field(default=None, description="Across the wall, into the material.")
    face_minima: list[tuple[int, float]] = Field(
        default_factory=list,
        description="(face id, thinnest wall sampled on it) for every face that produced one.",
    )
    method: str = ""
    method_description: str = ""
    limitations: list[str] = Field(default_factory=list)
    grid_side: int = 0
    budget_limited: bool = False
    budget_note: str | None = None


class DfmMarker(BaseModel):
    """Where to point at a flag that names no recognized feature.

    Shaped like a feature's `location` and `axis` so the viewer's existing
    marker draws it. Taken verbatim from the measurement: nothing is derived.
    """

    label: str
    location: Point3D
    axis: Point3D | None = None


class DfmFlag(BaseModel):
    """One advisory observation, with everything needed to check it."""

    rule_id: RuleId
    feature_ids: list[str]
    marker: DfmMarker | None = Field(
        default=None,
        description=(
            "Set only for a flag raised on a measurement rather than on recognized features "
            "(the minimum wall): the measured point and direction to mark on the model."
        ),
    )
    measured_value: float
    measured_unit: str
    comparison: str = Field(description="'greater_than' or 'less_than'; the flag fires when it holds.")
    threshold: float
    dfm_profile_id: str
    dfm_profile_version: str
    basis: str
    source_title: str | None
    source_url: str | None
    evidence_type: str = Field(description="'brep' or 'faceted' (any faceted input makes it faceted).")
    evidence_note: str | None = None
    explanation: str


class DfmRuleReport(BaseModel):
    rule_id: RuleId
    evaluated: bool
    features_checked: int
    flags: int
    skipped_reason: str | None = None

    @computed_field
    @property
    def label(self) -> str:
        return RULE_NAMES.get(self.rule_id, str(self.rule_id))


class DfmReport(BaseModel):
    filename: str
    dfm_profile_id: str
    dfm_profile_version: str
    profile_is_illustrative: bool
    disclaimer: str = DFM_DISCLAIMER
    flags: list[DfmFlag]
    flagged_features: list[Feature] = Field(
        default_factory=list,
        description=(
            "Every recognized feature some flag names in `feature_ids`, once each, in "
            "recognition order and exactly as recognition reported it. It is here so a flag "
            "can be pointed at on the 3D model from this response alone: `location` and "
            "`axis` are the recognizer's own, and a feature with no `location` cannot be "
            "drawn. Nothing is added to or derived from these features."
        ),
    )
    rules: list[DfmRuleReport]
    wall: WallEvidence | None = Field(
        default=None,
        description=(
            "The wall-thickness measurement the minimum-wall rule read, present only when the "
            "profile has that rule, with its method and what it can miss."
        ),
    )
    warnings: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def measurements_checked(self) -> int:
        """Features compared with a threshold, over the rules that were evaluated."""
        return sum(rule.features_checked for rule in self.rules if rule.evaluated)

    @computed_field
    @property
    def summary(self) -> str:
        """One plain sentence for the panel, so the browser works nothing out."""
        evaluated = [rule for rule in self.rules if rule.evaluated]
        if self.flags:
            return f"{len(self.flags)} flag(s) from {len(evaluated)} rule(s) checked."
        if not self.measurements_checked:
            return (
                f"No flags — but nothing on this part could be measured by the {len(evaluated)} "
                "rule(s), so this is not a clean bill of health."
            )
        return (
            f"No flags: {self.measurements_checked} measurement(s) were checked by "
            f"{len(evaluated)} rule(s), and none exceeded its threshold."
        )


def _distinct(values: list[float]) -> int:
    return len({round(v, 2) for v in values})


def _flag(
    profile: DfmProfile,
    setting: DfmRuleSetting,
    features: list[Feature],
    measured: float,
    unit: str,
    comparison: str,
    explanation: str,
) -> DfmFlag:
    faceted = any(f.source_type == "faceted" for f in features)
    return DfmFlag(
        rule_id=setting.rule_id,
        feature_ids=[f.feature_id for f in features],
        measured_value=round(measured, 4),
        measured_unit=unit,
        comparison=comparison,
        threshold=setting.threshold,
        dfm_profile_id=profile.dfm_profile_id,
        dfm_profile_version=profile.version,
        basis=setting.basis,
        source_title=setting.source_title,
        source_url=setting.source_url,
        evidence_type="faceted" if faceted else "brep",
        evidence_note=FACETED_NOTE if faceted else None,
        explanation=explanation,
    )


WALL_NOT_SUPPLIED = "wall thickness was not measured for this request."


def evaluate_dfm(
    features: list[Feature],
    profile: DfmProfile,
    filename: str = "",
    wall: WallEvidence | None = None,
) -> DfmReport:
    """Apply the profile's thresholds to recognized features and, for the
    minimum-wall rule, to the measured wall. Pure and deterministic."""
    flags: list[DfmFlag] = []
    reports: list[DfmRuleReport] = []

    for rule_id in RuleId:
        setting = profile.setting(rule_id)
        if setting is None:
            reports.append(
                DfmRuleReport(
                    rule_id=rule_id,
                    evaluated=False,
                    features_checked=0,
                    flags=0,
                    skipped_reason="This profile version sets no threshold for the rule.",
                )
            )
            continue

        found: list[DfmFlag] = []
        checked = 0

        if rule_id is RuleId.MIN_WALL:
            if wall is None or not wall.measured or wall.minimum_mm is None:
                reason = WALL_NOT_SUPPLIED if wall is None else (wall.not_measured_reason or WALL_NOT_SUPPLIED)
                reports.append(
                    DfmRuleReport(
                        rule_id=rule_id,
                        evaluated=False,
                        features_checked=0,
                        flags=0,
                        skipped_reason=reason,
                    )
                )
                continue
            # What was compared is each measured face's thinnest sampled wall.
            checked = len(wall.face_minima)
            if wall.minimum_mm < setting.threshold:
                thin = sum(1 for _, value in wall.face_minima if value < setting.threshold)
                found.append(
                    DfmFlag(
                        rule_id=rule_id,
                        feature_ids=[],
                        marker=(
                            DfmMarker(
                                label=f"Thinnest wall {wall.minimum_mm:g} mm",
                                location=wall.location,
                                axis=wall.direction,
                            )
                            if wall.location is not None
                            else None
                        ),
                        measured_value=round(wall.minimum_mm, 4),
                        measured_unit="mm",
                        comparison="less_than",
                        threshold=setting.threshold,
                        dfm_profile_id=profile.dfm_profile_id,
                        dfm_profile_version=profile.version,
                        basis=setting.basis,
                        source_title=setting.source_title,
                        source_url=setting.source_url,
                        evidence_type="brep",
                        explanation=(
                            f"The thinnest wall measured is {wall.minimum_mm:g} mm, from face "
                            f"{wall.from_face} through the material to face {wall.to_face}; "
                            f"{thin} measured face(s) have a sampled wall below "
                            f"{setting.threshold:g} mm. A thin wall deflects and chatters under "
                            "cutting forces, so it is slower to machine and harder to hold to "
                            "size. Each face is sampled on a fixed grid, so a thinner spot "
                            "between samples is not seen: the part may be thinner somewhere, "
                            "never thicker than this where it was measured."
                        ),
                    )
                )

        elif rule_id is RuleId.DEEP_HOLE:
            for f in features:
                if f.feature_type is not FeatureType.HOLE:
                    continue
                d = f.dimensions.get("diameter_mm")
                depth = f.dimensions.get("depth_mm")
                if not d or not depth or d <= 0:
                    continue
                checked += 1
                ratio = depth / d
                if ratio > setting.threshold:
                    found.append(
                        _flag(
                            profile, setting, [f], ratio, "depth/diameter", "greater_than",
                            f"Hole {f.feature_id} is {depth:g} mm deep at {d:g} mm diameter "
                            f"({ratio:.2f} x diameter). Deep holes need peck drilling or special "
                            "drills, evacuate chips poorly and tend to cost more cycle time.",
                        )
                    )

        elif rule_id is RuleId.DEEP_NARROW_RECESS:
            for f in features:
                if f.feature_type not in (FeatureType.POCKET, FeatureType.SLOT):
                    continue
                width = f.dimensions.get("width_mm")
                depth = f.dimensions.get("depth_mm")
                if not width or not depth or width <= 0:
                    continue
                checked += 1
                ratio = depth / width
                if ratio > setting.threshold:
                    found.append(
                        _flag(
                            profile, setting, [f], ratio, "depth/width", "greater_than",
                            f"{f.feature_type.value.capitalize()} {f.feature_id} is {depth:g} mm "
                            f"deep and {width:g} mm wide ({ratio:.2f} x its width). A long, thin "
                            "cutter deflects and vibrates, so deep narrow recesses are slower to "
                            "cut and harder to hold to tolerance. Width is the shorter side of "
                            "the floor.",
                        )
                    )

        elif rule_id is RuleId.SMALL_INTERNAL_RADIUS:
            for f in features:
                if f.feature_type is not FeatureType.FILLET or f.subtype != "concave":
                    continue
                radius = f.dimensions.get("radius_mm")
                if not radius or radius <= 0:
                    continue
                checked += 1
                if radius < setting.threshold:
                    found.append(
                        _flag(
                            profile, setting, [f], radius, "mm", "less_than",
                            f"Internal (concave) radius {f.feature_id} is {radius:g} mm. A cutter "
                            "cannot leave a sharper inside corner than its own radius, so very "
                            "small internal radii need very small, fragile cutters. Whether this "
                            "blend is a machined corner is a drawing question.",
                        )
                    )

        elif rule_id is RuleId.MANY_HOLE_DIAMETERS:
            holes = [
                f
                for f in features
                if f.feature_type is FeatureType.HOLE and f.dimensions.get("diameter_mm")
            ]
            checked = len(holes)
            count = _distinct([f.dimensions["diameter_mm"] for f in holes])
            if holes and count > setting.threshold:
                found.append(
                    _flag(
                        profile, setting, holes, float(count), "distinct diameters", "greater_than",
                        f"The part has {count} distinct hole diameters (rounded to 0.01 mm). "
                        "Each different size is generally another tool in the changer or "
                        "another tool change.",
                    )
                )

        flags.extend(found)
        reports.append(
            DfmRuleReport(rule_id=rule_id, evaluated=True, features_checked=checked, flags=len(found))
        )

    # The features the flags name, handed back unchanged so the panel can point at them
    # without a second recognition request. Membership is by id; order is recognition's.
    named = {feature_id for flag in flags for feature_id in flag.feature_ids}
    seen: set[str] = set()
    flagged_features: list[Feature] = []
    for f in features:
        if f.feature_id in named and f.feature_id not in seen:
            seen.add(f.feature_id)
            flagged_features.append(f)

    return DfmReport(
        filename=filename,
        dfm_profile_id=profile.dfm_profile_id,
        dfm_profile_version=profile.version,
        profile_is_illustrative=profile.is_illustrative,
        flags=flags,
        flagged_features=flagged_features,
        rules=reports,
        wall=wall if profile.setting(RuleId.MIN_WALL) is not None else None,
    )


# ---------------------------------------------------------------------------
# Seed data. Thresholds are taken from one published guide, quoted below
# (fetched 2026-10-27; the minimum-wall passage on 2026-11-13); the guide itself
# calls them general guidelines. The distinct-hole-diameter count has no
# published figure and says so.
#
# 1.0.0 is immutable and stays exactly as first seeded. 1.1.0 is 1.0.0 plus the
# minimum-wall rule, added as a new version because a published version is never
# edited. `seed` inserts only the versions a database is missing, so a fresh
# database and one first seeded with 1.0.0 alone end up holding the same rows.
# ---------------------------------------------------------------------------

GUIDE_TITLE = "A Practical Design Guide for CNC Machining (Yijin Solution)"
GUIDE_URL = "https://yijinsolution.com/cnc-guides/design-for-cnc-machining/"

DEFAULT_DFM_PROFILE_ID = "dfm-general-guidelines"

_GENERAL_1_0_0 = (
    DfmProfile(
        dfm_profile_id=DEFAULT_DFM_PROFILE_ID,
        name="General CNC machining guidelines",
        version="1.0.0",
        description=(
            "Thresholds from a published general CNC design guide, plus one rule with no "
            "published figure, labelled as such. Illustrative: a vendor's rules of thumb, not a "
            "standard and not your shop's capability. Publish a version with your own limits."
        ),
        is_illustrative=True,
        rules=[
            DfmRuleSetting(
                rule_id=RuleId.DEEP_HOLE,
                threshold=4.0,
                unit="ratio",
                basis=BASIS_PUBLISHED,
                source_title=GUIDE_TITLE,
                source_url=GUIDE_URL,
                source_quote=(
                    "Standard depth-to-diameter ratio for drilled holes is 4:1 or less. Deep "
                    "drilling up to 10:1 is possible with gun drills or high-pressure coolant."
                ),
            ),
            DfmRuleSetting(
                rule_id=RuleId.DEEP_NARROW_RECESS,
                threshold=4.0,
                unit="ratio",
                basis=BASIS_PUBLISHED,
                source_title=GUIDE_TITLE,
                source_url=GUIDE_URL,
                source_quote="As a general guideline, cavity depth should not exceed four times the feature width.",
            ),
            DfmRuleSetting(
                rule_id=RuleId.SMALL_INTERNAL_RADIUS,
                threshold=0.5,
                unit="mm",
                basis=BASIS_PUBLISHED,
                source_title=GUIDE_TITLE,
                source_url=GUIDE_URL,
                source_quote=(
                    "internal corner radius should be at least one-third of the pocket depth, "
                    "with a recommended minimum of R0.5 mm. (Only the R0.5 mm minimum is applied: "
                    "the recognizers do not measure a pocket's corner radius, so the one-third "
                    "of depth rule has nothing to compare.)"
                ),
            ),
            DfmRuleSetting(
                rule_id=RuleId.MANY_HOLE_DIAMETERS,
                threshold=6.0,
                unit="count",
                basis=BASIS_NONE_FOUND,
                source_quote=(
                    "The cited guide says a standard-size hole is a one-tool operation but gives no "
                    "count at which variety becomes a problem; 6 is an illustrative choice."
                ),
            ),
        ],
    )
)

MIN_WALL_SETTING = DfmRuleSetting(
    rule_id=RuleId.MIN_WALL,
    threshold=0.8,
    unit="mm",
    basis=BASIS_PUBLISHED,
    source_title=GUIDE_TITLE,
    source_url=GUIDE_URL,
    source_quote=(
        "Minimum recommended wall thickness for metals is 0.8 mm, though 1.5 mm or more is "
        "preferred for structural rigidity. Engineering plastics need at least 1.5 mm because "
        "they deflect more readily under tool pressure. (The metal minimum is applied: a profile "
        "is not material-aware, so a shop machining plastics should publish a version with "
        "1.5 mm.)"
    ),
)

SEED_DFM_PROFILES: tuple[DfmProfile, ...] = (
    _GENERAL_1_0_0,
    DfmProfile(
        dfm_profile_id=DEFAULT_DFM_PROFILE_ID,
        name=_GENERAL_1_0_0.name,
        version="1.1.0",
        description=(
            "1.0.0 plus a minimum-wall rule, read from the measured wall thickness (exact B-Rep "
            "only). Thresholds from a published general CNC design guide, plus one rule with no "
            "published figure, labelled as such. Illustrative: a vendor's rules of thumb, not a "
            "standard and not your shop's capability. Publish a version with your own limits."
        ),
        is_illustrative=True,
        rules=[*_GENERAL_1_0_0.rules, MIN_WALL_SETTING],
    ),
)


__all__ = [
    "BASIS_NONE_FOUND",
    "BASIS_PUBLISHED",
    "DEFAULT_DFM_PROFILE_ID",
    "DFM_DISCLAIMER",
    "DfmFlag",
    "DfmMarker",
    "DfmProfile",
    "DfmProfileSummary",
    "DfmProfileVersionCreate",
    "DfmReport",
    "DfmRuleSetting",
    "RuleId",
    "SEED_DFM_PROFILES",
    "WallEvidence",
    "evaluate_dfm",
]
