"""Named, versioned time-assumption profiles — ILLUSTRATIVE POC VALUES ONLY.

    Rate set          = money      (what an hour costs)
    Time profile      = duration   (how long the work takes)

Two separate, separately versioned things, and they must stay separate. Merging
them would mean editing a feed rate silently changed a price, and it would make
"which rates produced this quote?" and "which machining assumptions produced
this time?" the same question when they are not — a shop can revise its feeds
and speeds without its labour rate moving, and vice versa.

This module is the exact counterpart of `rate_sets.py`, deliberately so:

- Identity is `(time_profile_id, version)`. Resolving an id without a version
  selects the highest version, compared numerically rather than lexically.
- **A published version is immutable.** There is no update path anywhere in the
  repository; changing an assumption means publishing a new version, and an
  estimate already made keeps pointing at the version that produced it.
- It **configures inputs and calculates nothing**. The parameters it carries are
  `TimeAssumptions`, which `operation_time` consumes; no formula is duplicated
  here, and this module imports no costing arithmetic and no estimator logic.
- It has no database dependency. `SEED_TIME_PROFILES` is seed data written into
  SQLite once at startup, after which `TimeProfileRepository` is authoritative.

Why the parameters are not redefined here
-----------------------------------------
`TimeAssumptions` lives in `operation_time.py`, next to the formulas that read
it, so a parameter and the arithmetic that consumes it cannot drift apart. A
profile is that model plus an identity, a version and a name — exactly as a
`RateSet` is material and operation rates plus an identity and a version.
"""

from pydantic import BaseModel, ConfigDict, Field

from cad_costing_agent.operation_time import TimeAssumptions

TIME_PROFILE_ID_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
VERSION_PATTERN = r"^\d+\.\d+\.\d+$"

PRECEDENCE_RULE = (
    "A time profile supplies the parameters the deterministic estimator runs on. It never "
    "supplies a time: an operation's time is estimated from these parameters and the feature's "
    "own dimensions, and it reaches a cost only when a person accepts it or types their own. "
    "Explicit per-request assumptions, where an API caller sends them, win over a profile."
)


class TimeProfile(BaseModel):
    """One immutable version of a set of machining-time assumptions.

    Deliberately has **no currency**. Nothing here is money: these values decide
    how long an operation takes, and what that duration costs comes from the
    rate set, resolved separately by process type.
    """

    model_config = ConfigDict(frozen=True)

    time_profile_id: str = Field(
        pattern=TIME_PROFILE_ID_PATTERN,
        description="Stable identity, lowercase slug, e.g. 'pune-shop-time'.",
    )
    name: str = Field(min_length=1)
    version: str = Field(pattern=VERSION_PATTERN, description="MAJOR.MINOR.PATCH.")
    description: str = ""
    is_illustrative: bool = Field(
        default=True,
        description=(
            "True for the built-in baseline. Times estimated with an illustrative profile carry "
            "an unmissable warning and must not be quoted, exactly as illustrative rates do."
        ),
    )
    assumptions: TimeAssumptions = Field(
        default_factory=TimeAssumptions,
        description="The cutting parameters, validated by `TimeAssumptions` itself.",
    )

    def reference(self) -> "TimeProfileReference":
        return TimeProfileReference(
            time_profile_id=self.time_profile_id,
            name=self.name,
            version=self.version,
            is_illustrative=self.is_illustrative,
        )


class TimeProfileReference(BaseModel):
    """Compact provenance stamp recorded on every estimated time.

    Small enough to sit on each operation without bloating a plan, while the
    full parameter set is snapshotted once on the estimate — so "which profile
    produced this prediction?" is answerable per operation, and "what exactly
    was in it?" is answerable from the estimate.
    """

    model_config = ConfigDict(frozen=True)

    time_profile_id: str
    name: str
    version: str
    is_illustrative: bool = True

    @property
    def label(self) -> str:
        return f"{self.name} v{self.version}"


class TimeProfileVersionCreate(BaseModel):
    """Body for `POST /time-profiles/{id}/versions` — a profile minus its id.

    The id comes from the path, which is the only reason this exists. The
    constructed `TimeProfile` re-validates everything, so the two cannot drift
    apart in what they accept.
    """

    name: str = Field(min_length=1)
    version: str = Field(pattern=VERSION_PATTERN, description="MAJOR.MINOR.PATCH.")
    description: str = ""
    is_illustrative: bool = True
    assumptions: TimeAssumptions = Field(default_factory=TimeAssumptions)


class TimeProfileSummary(BaseModel):
    """One row of the profile listing, readable without fetching the parameters."""

    time_profile_id: str
    name: str
    version: str
    description: str
    is_illustrative: bool


class TimeProfileValidation(BaseModel):
    """Result of checking a candidate profile without storing it.

    Field-level problems never reach here: `TimeAssumptions` rejects them as a
    422 first. What is left is what needs the stored catalog to know — whether
    this version already exists, which would make publishing a conflict — plus
    the advisory notes below, which flag values that are *legal but surprising*
    and are therefore worth a second look rather than a refusal.
    """

    valid: bool
    time_profile_id: str
    name: str
    version: str
    is_illustrative: bool
    version_already_exists: bool
    is_new_profile: bool
    messages: list[str] = Field(default_factory=list)


def advisory_notes(profile: TimeProfile) -> list[str]:
    """Legal values that are unusual enough to be worth querying.

    These are **not** validation failures. The parameters are the user's to
    choose and the estimator will use them exactly as given; refusing an unusual
    but deliberate value would be the app overruling the person who knows the
    shop. Saying "this is 20x the usual" and moving on is the honest middle.
    """
    a = profile.assumptions
    notes: list[str] = []

    if profile.is_illustrative:
        notes.append(
            "This profile is marked illustrative, so times estimated with it will carry a "
            "warning and must not be quoted."
        )

    for label, value, typical in [
        ("drilling cutting speed", a.drilling_cutting_speed_m_per_min, (5.0, 200.0)),
        ("milling cutting speed", a.milling_cutting_speed_m_per_min, (10.0, 500.0)),
        ("turning cutting speed", a.turning_cutting_speed_m_per_min, (10.0, 600.0)),
        ("drilling feed", a.drilling_feed_mm_per_rev, (0.02, 1.0)),
        ("milling feed per tooth", a.milling_feed_per_tooth_mm, (0.01, 1.0)),
        ("turning feed", a.turning_feed_mm_per_rev, (0.02, 1.5)),
    ]:
        low, high = typical
        if value < low or value > high:
            notes.append(
                f"The {label} of {value:g} is outside the usual {low:g}–{high:g} range for "
                "general machining. It will be used exactly as entered — check it is intended."
            )

    if a.milling_stepover_ratio > 0.9:
        notes.append(
            f"A stepover of {a.milling_stepover_ratio:g} × cutter diameter is nearly a full-width "
            "cut, which will produce shorter times than a typical finishing pass."
        )
    if a.turning_stock_allowance_mm > 0 and a.turning_depth_of_cut_mm > 0:
        passes = a.turning_stock_allowance_mm / a.turning_depth_of_cut_mm
        if passes > 20:
            notes.append(
                f"The stock allowance implies about {passes:.0f} turning passes, which will make "
                "turning times very long. Check the allowance and depth of cut."
            )
    return notes


# ---------------------------------------------------------------------------
# Seed data for the POC catalog.
#
# ILLUSTRATIVE POC VALUES ONLY — plausible for general steel machining on a
# modest CNC, chosen to exercise the formulas and keep the project's worked
# examples numerically stable. They are not any real shop's feeds and speeds and
# must not be used for a quotation.
#
# Written into SQLite once, idempotently, by `db.init_db`. After that the
# database is authoritative; editing this tuple does not retroactively change a
# profile version that is already stored.
# ---------------------------------------------------------------------------

BASELINE_PROFILE_ID = "poc-baseline-time"

SEED_TIME_PROFILES: tuple[TimeProfile, ...] = (
    TimeProfile(
        time_profile_id=BASELINE_PROFILE_ID,
        name="POC Baseline Machining",
        version="1.0.0",
        description=(
            "The project's built-in illustrative machining assumptions: general steel on a "
            "modest CNC. Publish your own profile with your shop's real feeds and speeds "
            "before treating any estimated time as usable."
        ),
        is_illustrative=True,
        assumptions=TimeAssumptions(),
    ),
)


__all__ = [
    "BASELINE_PROFILE_ID",
    "PRECEDENCE_RULE",
    "SEED_TIME_PROFILES",
    "TIME_PROFILE_ID_PATTERN",
    "TimeProfile",
    "TimeProfileReference",
    "TimeProfileSummary",
    "TimeProfileValidation",
    "TimeProfileVersionCreate",
    "advisory_notes",
]
