"""Named, versioned rate sets — ILLUSTRATIVE POC VALUES ONLY.

Why rate sets exist
-------------------

Before this layer, every rate reached a costing calculator as an independent
per-request value: material price came from the module-level `MATERIALS`
catalog, machine and labor rates were fields on each `ManufacturingOperation`,
and overhead/scrap/tooling were fields on `AdditionalCostInputs`. Two estimates
could therefore differ purely because of rates, with nothing in either result
recording which rates were used. A rate set gives that collection of inputs a
stable identity (`rate_set_id`) and an explicit `version`, so an estimate can
state exactly which rate configuration produced it.

What this module does and does not do
-------------------------------------

It **configures inputs**; it does not calculate cost. `resolve_material` and
`resolve_operation` build the existing `Material` and `ManufacturingOperation`
input models, which are then handed to the existing, unmodified calculators in
`costing.py`, `operation_costing.py`, and `additional_costs.py`. No costing
formula is duplicated here.

Identity and versioning
-----------------------

A rate set is identified by (`rate_set_id`, `version`). The catalog may hold
several versions of the same id; resolving an id without a version selects the
highest version, and the exact version resolved is always echoed back in the
estimate, so a result is never ambiguous about what produced it. Pinning
`rate_set_version` gives full reproducibility.

This module stays free of any database dependency. `SEED_RATE_SETS` below is
**seed data**, not a live catalog: it is written into SQLite once at startup,
after which `RateSetRepository` is the authoritative source. Lookup therefore
lives in `repository.py`, and the costing calculators receive an already
resolved `RateSet` rather than reaching for one themselves.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cad_costing_agent.additional_costs import AdditionalCostInputs
from cad_costing_agent.materials import Material
from cad_costing_agent.money import CURRENCY_CODE_PATTERN, DEFAULT_CURRENCY
from cad_costing_agent.operation import ManufacturingOperation

RATE_SET_ID_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
VERSION_PATTERN = r"^\d+\.\d+\.\d+$"

PRECEDENCE_RULE = (
    "Explicit per-request values always win; the rate set only supplies what the request "
    "omits. Material rates come from the rate set when one is selected, otherwise from the "
    "built-in material catalog. Operations listed under 'operations' carry their own rates "
    "and are never overridden; operations listed under 'operation_requirements' take their "
    "rates from the rate set by process type. An explicit 'additional_costs' object replaces "
    "the rate set's additional costs wholesale; if it is omitted, the rate set's values are "
    "used, and with no rate set they default to zero."
)


class MaterialRate(BaseModel):
    """Price and density for one material in a rate set. ILLUSTRATIVE POC VALUE."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, description="Display name, e.g. 'Steel'.")
    cost_per_kg: float = Field(gt=0, description="ILLUSTRATIVE POC VALUE, not real pricing.")
    density_kg_m3: float = Field(gt=0)


def _absent(value) -> bool:
    """`exclude_if` predicate: an optional field nobody set is left out of a dump.

    That is what keeps a rate set version published before these fields existed
    serializing — into its stored row, its estimate snapshots and every response —
    exactly as it always has.
    """
    return value is None


class OperationRate(BaseModel):
    """Machine and labor rates for one process type. ILLUSTRATIVE POC VALUES.

    The two overhead rates are optional and per hour of **run** time — the
    machine's cutting hours, the same hours its labour is charged on. They are a
    shop's per-machine burden (aPriori calls them a machine's direct and indirect
    overhead): direct for what the machine itself consumes — power, maintenance,
    tooling wear — and indirect for its share of the building, supervision and
    support. Absent means none is charged, and every figure is what it was
    before the fields existed. Neither is converted or inferred from anything.
    """

    model_config = ConfigDict(frozen=True)

    machine_rate_per_hour: float = Field(gt=0, description="ILLUSTRATIVE POC VALUE.")
    labor_rate_per_hour: float = Field(gt=0, description="ILLUSTRATIVE POC VALUE.")
    direct_overhead_per_hour: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        exclude_if=_absent,
        description="Optional per-machine direct overhead per hour of run time.",
    )
    indirect_overhead_per_hour: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        exclude_if=_absent,
        description="Optional per-machine indirect overhead per hour of run time.",
    )


# ISO 3166-1 alpha-2 country ("IN") or ISO 3166-2 subdivision ("IN-MH"). Only the
# shape is checked: the code is a label someone stated, and nothing — currency,
# rates, emission factors — is converted or inferred from it.
REGION_CODE_PATTERN = r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$"


class FactoryProfile(BaseModel):
    """Which shop a rate set describes: a label, never an input to arithmetic.

    aPriori's "digital factories" are per-region labour, overhead and material
    data. Here a rate set already is one shop's rates; this says which shop and
    where, so rate sets can be listed and compared by factory. The currency stays
    the rate set's own and is never converted because of a region.
    """

    model_config = ConfigDict(frozen=True)

    region: str = Field(
        pattern=REGION_CODE_PATTERN,
        description="ISO 3166 code: a country ('IN') or a subdivision ('IN-MH').",
    )
    factory_name: str = Field(min_length=1, max_length=128)
    site_notes: str = Field(default="", max_length=1000)


PER_KG = "per_kg"
PER_PIECE = "per_piece"


class StageRate(BaseModel):
    """What a shop or supplier charges for one non-machining route stage.

    Casting, forging, heat treatment, plating, a bought-out part: these are not
    priced by the hour, so they do not fit `OperationRate`. A stage rate is a
    unit rate on a stated basis — per kg of a mass someone enters, or per piece
    — and/or a one-off charge per batch such as a pattern or a die. Keyed in the
    rate set by stage id ('casting', 'finishing', 'purchase', ...).
    """

    model_config = ConfigDict(frozen=True)

    unit_basis: Literal["per_kg", "per_piece"] | None = Field(
        default=None, description="What `unit_rate` is per. Required with a unit rate."
    )
    unit_rate: float | None = Field(
        default=None, gt=0, allow_inf_nan=False, description="Per kg or per piece."
    )
    batch_charge: float = Field(
        default=0.0,
        ge=0,
        allow_inf_nan=False,
        description="One-off charge for the whole batch, e.g. a pattern or a die.",
    )
    note: str = Field(default="", max_length=500, description="Where the rate came from.")

    @model_validator(mode="after")
    def _complete(self) -> "StageRate":
        if (self.unit_rate is None) != (self.unit_basis is None):
            raise ValueError(
                "A unit rate needs its basis (per_kg or per_piece), and a basis needs its rate."
            )
        if self.unit_rate is None and self.batch_charge == 0:
            raise ValueError("A stage rate needs a unit rate, a batch charge, or both.")
        return self


class OperationRequirement(BaseModel):
    """An operation whose *rates* are left to a rate set.

    Mirrors `ManufacturingOperation` minus the two rate fields: times and
    quantity describe the part and its process plan, while the rates describe
    the shop and therefore belong to the rate set.
    """

    operation_id: str = Field(min_length=1)
    operation_name: str = Field(min_length=1)
    process_type: str = Field(min_length=1, description="Looked up in the rate set, case-insensitively.")
    setup_time_minutes: float = Field(ge=0)
    cycle_time_minutes: float = Field(gt=0, description="Cycle time per part.")
    quantity: int = Field(gt=0, default=1)


class RateSetReference(BaseModel):
    """Compact provenance stamp recorded on an estimate."""

    rate_set_id: str
    name: str
    version: str
    currency: str
    is_illustrative: bool


class RateSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    rate_set_id: str = Field(
        pattern=RATE_SET_ID_PATTERN,
        description="Stable identity, lowercase slug, e.g. 'poc-default'.",
    )
    name: str = Field(min_length=1)
    version: str = Field(pattern=VERSION_PATTERN, description="MAJOR.MINOR.PATCH.")
    description: str = ""
    is_illustrative: bool = True
    currency: str = Field(
        default=DEFAULT_CURRENCY,
        pattern=CURRENCY_CODE_PATTERN,
        description=(
            "ISO-4217-style code the rates are denominated in. Defaults to INR for this "
            "POC. There is no conversion: an estimate is denominated in its rate set's "
            "currency."
        ),
    )
    materials: dict[str, MaterialRate] = Field(
        default_factory=dict, description="Keyed by lowercase material name."
    )
    operations: dict[str, OperationRate] = Field(
        default_factory=dict, description="Keyed by lowercase process type."
    )
    additional_costs: AdditionalCostInputs = Field(default_factory=AdditionalCostInputs)
    stage_rates: dict[str, StageRate] = Field(
        default_factory=dict,
        description=(
            "Rates for non-machining route stages, keyed by stage id ('casting', 'finishing', "
            "'purchase', ...). A version published before these existed has none, so a route "
            "containing such a stage cannot be priced with it."
        ),
    )
    factory: FactoryProfile | None = Field(
        default=None,
        exclude_if=_absent,
        description=(
            "Which factory and region these rates describe. A label only: nothing is converted "
            "or inferred from it. Absent on every version published before it existed."
        ),
    )

    def reference(self) -> RateSetReference:
        return RateSetReference(
            rate_set_id=self.rate_set_id,
            name=self.name,
            version=self.version,
            currency=self.currency,
            is_illustrative=self.is_illustrative,
        )


class RateSetVersionCreate(BaseModel):
    """Body for `POST /rates/{rate_set_id}/versions` — a RateSet minus its id.

    The id comes from the path, which is the only reason this exists. Field
    rules reuse the same constants as `RateSet`, and the constructed `RateSet`
    re-validates everything, so the two cannot drift apart in what they accept.
    """

    name: str = Field(min_length=1)
    version: str = Field(pattern=VERSION_PATTERN, description="MAJOR.MINOR.PATCH.")
    description: str = ""
    is_illustrative: bool = True
    currency: str = Field(default=DEFAULT_CURRENCY, pattern=CURRENCY_CODE_PATTERN)
    materials: dict[str, MaterialRate] = Field(default_factory=dict)
    operations: dict[str, OperationRate] = Field(default_factory=dict)
    additional_costs: AdditionalCostInputs = Field(default_factory=AdditionalCostInputs)
    stage_rates: dict[str, StageRate] = Field(default_factory=dict)
    factory: FactoryProfile | None = None


class RateSetSummary(BaseModel):
    """One row of the rate-set listing.

    The counts let a listing say "8 materials, 6 processes" without fetching
    every version in full, which is the difference between a profile picker a
    person can read and a developer-oriented list of version strings.
    """

    rate_set_id: str
    name: str
    version: str
    description: str
    currency: str
    is_illustrative: bool
    material_count: int = 0
    process_count: int = 0
    stage_rate_count: int = 0
    # The factory label, for grouping and filtering a picker. Absent (not null)
    # for a version that states none, so older listings read as they always did.
    region: str | None = Field(default=None, exclude_if=_absent)
    factory_name: str | None = Field(default=None, exclude_if=_absent)


class RateSetValidation(BaseModel):
    """Result of checking a candidate rate set without storing it.

    Lets the setup UI's review step ask the backend "would this publish?" and
    get an authoritative answer, rather than deciding for itself. Field-level
    problems never reach here — they are rejected as a 422 by `RateSet` itself,
    so this only reports what needs the stored catalog to know.
    """

    valid: bool
    rate_set_id: str
    name: str
    version: str
    currency: str
    is_illustrative: bool
    material_count: int
    process_count: int
    version_already_exists: bool
    is_new_profile: bool
    messages: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Seed data for the POC catalog.
#
# ILLUSTRATIVE POC VALUES ONLY. These are placeholders chosen to exercise the
# calculations and to keep the project's existing worked examples numerically
# stable. They are not real market or customer pricing and must not be used for
# any actual quotation.
#
# These are written into SQLite once, idempotently, by `db.init_db`. After that
# the database is authoritative; editing this tuple does not retroactively
# change rate set versions that are already stored.
# ---------------------------------------------------------------------------

_POC_MATERIALS: dict[str, MaterialRate] = {
    "steel": MaterialRate(name="Steel", cost_per_kg=0.80, density_kg_m3=7850),
    "aluminium": MaterialRate(name="Aluminium", cost_per_kg=2.20, density_kg_m3=2700),
}

SEED_RATE_SETS: tuple[RateSet, ...] = (
    RateSet(
        rate_set_id="poc-default",
        name="POC Default",
        version="1.0.0",
        description=(
            "Neutral illustrative baseline: material and operation rates matching the "
            "project's built-in values, with no overhead, scrap, or tooling applied."
        ),
        materials=dict(_POC_MATERIALS),
        operations={
            "milling": OperationRate(machine_rate_per_hour=1200.0, labor_rate_per_hour=600.0),
            "drilling": OperationRate(machine_rate_per_hour=1800.0, labor_rate_per_hour=500.0),
            "turning": OperationRate(machine_rate_per_hour=1500.0, labor_rate_per_hour=550.0),
        },
        additional_costs=AdditionalCostInputs(),
    ),
    RateSet(
        rate_set_id="poc-machine-shop",
        name="POC Machine Shop",
        version="1.0.0",
        description=(
            "Illustrative machine-shop rates that also apply overhead, a scrap allowance, "
            "and a one-time tooling charge."
        ),
        materials=dict(_POC_MATERIALS),
        operations={
            "milling": OperationRate(machine_rate_per_hour=1200.0, labor_rate_per_hour=600.0),
            "drilling": OperationRate(machine_rate_per_hour=1800.0, labor_rate_per_hour=500.0),
        },
        additional_costs=AdditionalCostInputs(
            overhead_rate_percent=10.0, scrap_rate_percent=5.0, tooling_cost_per_batch=250.0
        ),
    ),
    RateSet(
        rate_set_id="poc-machine-shop",
        name="POC Machine Shop",
        version="2.0.0",
        description=(
            "A later revision of the same rate set with higher illustrative rates, used to "
            "demonstrate that two versions of one id resolve to different rate inputs."
        ),
        materials={
            "steel": MaterialRate(name="Steel", cost_per_kg=0.95, density_kg_m3=7850),
            "aluminium": MaterialRate(name="Aluminium", cost_per_kg=2.60, density_kg_m3=2700),
        },
        operations={
            "milling": OperationRate(machine_rate_per_hour=1400.0, labor_rate_per_hour=700.0),
            "drilling": OperationRate(machine_rate_per_hour=2000.0, labor_rate_per_hour=600.0),
        },
        additional_costs=AdditionalCostInputs(
            overhead_rate_percent=12.5, scrap_rate_percent=6.0, tooling_cost_per_batch=300.0
        ),
    ),
)


def version_key(version: str) -> tuple[int, ...]:
    """Sort key for a MAJOR.MINOR.PATCH string, compared numerically.

    Lexical comparison would put 10.0.0 below 2.0.0, so the repository uses
    this rather than ordering in SQL.
    """
    return tuple(int(part) for part in version.split("."))


def resolve_material(rate_set: RateSet, material_name: str) -> Material:
    """Build the existing `Material` input from a rate set's material rates."""
    rate = rate_set.materials.get(material_name.strip().lower())
    if rate is None:
        known = ", ".join(sorted(entry.name for entry in rate_set.materials.values())) or "none"
        raise ValueError(
            f"Rate set '{rate_set.rate_set_id}' v{rate_set.version} has no rate for material "
            f"'{material_name}'. Materials priced by this rate set: {known}"
        )

    return Material(
        name=rate.name,
        density_kg_m3=rate.density_kg_m3,
        cost_per_kg=rate.cost_per_kg,
        illustrative=rate_set.is_illustrative,
    )


def resolve_operation(
    rate_set: RateSet, requirement: OperationRequirement
) -> ManufacturingOperation:
    """Build the existing `ManufacturingOperation` input from a requirement + rate set."""
    rate = rate_set.operations.get(requirement.process_type.strip().lower())
    if rate is None:
        known = ", ".join(sorted(rate_set.operations)) or "none"
        raise ValueError(
            f"Rate set '{rate_set.rate_set_id}' v{rate_set.version} has no rate for process "
            f"type '{requirement.process_type}'. Process types priced by this rate set: {known}"
        )

    return ManufacturingOperation(
        operation_id=requirement.operation_id,
        operation_name=requirement.operation_name,
        process_type=requirement.process_type,
        machine_rate_per_hour=rate.machine_rate_per_hour,
        labor_rate_per_hour=rate.labor_rate_per_hour,
        setup_time_minutes=requirement.setup_time_minutes,
        cycle_time_minutes=requirement.cycle_time_minutes,
        quantity=requirement.quantity,
        is_illustrative_rate=rate_set.is_illustrative,
        direct_overhead_per_hour=rate.direct_overhead_per_hour,
        indirect_overhead_per_hour=rate.indirect_overhead_per_hour,
    )
