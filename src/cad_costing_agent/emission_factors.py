"""Versioned, immutable emission-factor catalogs for a CO2e estimate.

    stored estimate -> **emission factors** -> co2e (kg CO2e, never money)

A catalog holds three kinds of factor, and nothing else:

- **material** — kg CO2e per kg of material, keyed by material name exactly as a
  rate set keys it (`steel`, `aluminium`);
- **process electricity** — kWh per hour of machine *run* time, keyed by process
  type (`milling`, `turning`, ...);
- **grid** — kg CO2e per kWh of electricity, keyed by an ISO 3166 region code
  (`IN`, `GB`);

plus optional per-kg factors for non-machining **route stages** (casting, heat
treatment ...), of which the seeded catalog has none.

Every factor says where it came from, the way a DFM threshold does: either a
published source with its title, URL and the words quoted from it, or, in words,
`no_published_source_found_illustrative`. Publishing a factor marked published
without a title, a URL and a quote is refused. A factor is never presented as
an IPCC or ecoinvent value unless it is one, and none in the seed is.

Like rate sets, time profiles and DFM profiles, a catalog version is immutable:
it is seeded once and new versions are published, never edited. This module
imports nothing from the package. `co2e.py` (the arithmetic) reads it, as do
`repository.py`, `db.py` and `main.py`.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

EMISSION_CATALOG_ID_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
VERSION_PATTERN = r"^\d+\.\d+\.\d+$"
# The same shape a rate set's factory region takes (a test holds the two equal):
# an ISO 3166-1 country or ISO 3166-2 subdivision code. Only the shape is checked.
REGION_CODE_PATTERN = r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$"

BASIS_PUBLISHED = "published_source"
BASIS_NONE_FOUND = "no_published_source_found_illustrative"

DEFAULT_EMISSION_CATALOG_ID = "illustrative-co2e"


class FactorSource(BaseModel):
    """Where one factor came from. A published one quotes its source."""

    model_config = ConfigDict(frozen=True)

    basis: str = Field(description=f"'{BASIS_PUBLISHED}' or '{BASIS_NONE_FOUND}'.")
    source_title: str | None = None
    source_url: str | None = None
    source_quote: str | None = Field(
        default=None, description="The figure as the source states it, quoted."
    )
    note: str = Field(
        default="",
        max_length=1000,
        description="What the figure covers and how it differs from what it is applied to.",
    )

    @model_validator(mode="after")
    def _basis_is_honest(self):
        if self.basis not in (BASIS_PUBLISHED, BASIS_NONE_FOUND):
            raise ValueError(f"basis must be '{BASIS_PUBLISHED}' or '{BASIS_NONE_FOUND}'.")
        if self.basis == BASIS_PUBLISHED and not (
            self.source_title and self.source_url and self.source_quote
        ):
            raise ValueError(
                f"A factor with basis '{BASIS_PUBLISHED}' must carry source_title, source_url "
                f"and source_quote. If there is no source, say so with '{BASIS_NONE_FOUND}' "
                "instead of presenting it as published."
            )
        return self


class MaterialFactor(FactorSource):
    kg_co2e_per_kg: float = Field(gt=0, allow_inf_nan=False)


class ProcessElectricity(FactorSource):
    kwh_per_run_hour: float = Field(gt=0, allow_inf_nan=False)


class GridFactor(FactorSource):
    kg_co2e_per_kwh: float = Field(ge=0, allow_inf_nan=False)


class StageFactor(FactorSource):
    kg_co2e_per_kg: float = Field(gt=0, allow_inf_nan=False)


class EmissionFactorCatalog(BaseModel):
    """One immutable version of a set of emission factors."""

    model_config = ConfigDict(frozen=True)

    emission_catalog_id: str = Field(pattern=EMISSION_CATALOG_ID_PATTERN)
    name: str = Field(min_length=1)
    version: str = Field(pattern=VERSION_PATTERN)
    description: str = ""
    is_illustrative: bool = True
    materials: dict[str, MaterialFactor] = Field(
        default_factory=dict, description="Keyed by lowercase material name."
    )
    process_electricity: dict[str, ProcessElectricity] = Field(
        default_factory=dict, description="Keyed by lowercase process type."
    )
    grid: dict[str, GridFactor] = Field(
        default_factory=dict, description="Keyed by ISO 3166 region code."
    )
    stage_factors: dict[str, StageFactor] = Field(
        default_factory=dict, description="Keyed by route stage id; per kg of the stage's mass."
    )

    @model_validator(mode="after")
    def _keys(self):
        for region in self.grid:
            if not re.fullmatch(REGION_CODE_PATTERN, region):
                raise ValueError(
                    f"Grid region '{region}' must be an ISO 3166 code such as 'IN' or 'IN-MH'."
                )
        for name, table in (("materials", self.materials), ("process_electricity", self.process_electricity)):
            for key in table:
                if key != key.strip().lower() or not key:
                    raise ValueError(f"{name} keys are lowercase names; got '{key}'.")
        return self


class EmissionFactorCatalogVersionCreate(BaseModel):
    """Body for `POST /emission-factors/{id}/versions` — a catalog minus its id."""

    name: str = Field(min_length=1)
    version: str = Field(pattern=VERSION_PATTERN)
    description: str = ""
    is_illustrative: bool = True
    materials: dict[str, MaterialFactor] = Field(default_factory=dict)
    process_electricity: dict[str, ProcessElectricity] = Field(default_factory=dict)
    grid: dict[str, GridFactor] = Field(default_factory=dict)
    stage_factors: dict[str, StageFactor] = Field(default_factory=dict)


class EmissionFactorCatalogSummary(BaseModel):
    emission_catalog_id: str
    name: str
    version: str
    description: str
    is_illustrative: bool
    material_count: int = 0
    process_count: int = 0
    regions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# The one seeded catalog. ILLUSTRATIVE.
#
# The material and grid figures below are quoted from the sources named, which
# were fetched for this purpose; what each figure covers is written beside it,
# because none of them is exactly "kg CO2e per kg of this part's material":
# worldsteel's is CO2 per tonne of *crude* steel, the IAI's is cradle-to-gate for
# *primary* aluminium, and the Indian grid figure is CO2 only. The per-process
# electricity draws have no published source that was found, and say so.
# ---------------------------------------------------------------------------

_WORLDSTEEL_TITLE = "worldsteel — Sustainability Indicators 2023 report (2022 data)"
_WORLDSTEEL_URL = "https://worldsteel.org/wp-content/uploads/Sustainability-indicators-report-2023.pdf"
_IAI_TITLE = (
    "International Aluminium Institute — Global aluminium industry greenhouse gas emissions "
    "intensity reduction continues, with total emissions below 2020 peak (press release)"
)
_IAI_URL = (
    "https://international-aluminium.org/global-aluminium-industry-greenhouse-gas-emissions-"
    "intensity-reduction-continues-with-total-emissions-below-2020-peak/"
)
_DESNZ_TITLE = (
    "UK Government GHG Conversion Factors for Company Reporting 2024 — condensed set v1.1, "
    "sheet 'UK electricity'"
)
_DESNZ_URL = (
    "https://www.gov.uk/government/publications/greenhouse-gas-reporting-conversion-factors-2024"
)
_CEA_SECONDARY_TITLE = (
    "Saur Energy — report of the CEA CO2 Baseline Database for the Indian Power Sector, "
    "Version 20.0 (secondary source)"
)
_CEA_SECONDARY_URL = (
    "https://www.saurenergy.com/solar-energy-news/"
    "ceas-co2-baseline-database-impact-of-re-on-indian-grid-emissions-intensity"
)

_NO_SOURCE_ELECTRICITY = (
    "Illustrative average electrical draw of a machine of this kind while cutting. No "
    "published source for a per-process figure was found; a real one is the machine's own "
    "metered draw."
)

SEED_EMISSION_CATALOGS: tuple[EmissionFactorCatalog, ...] = (
    EmissionFactorCatalog(
        emission_catalog_id=DEFAULT_EMISSION_CATALOG_ID,
        name="Illustrative CO2e factors",
        version="1.0.0",
        description=(
            "ILLUSTRATIVE. Two material figures and two grid figures quoted from published "
            "sources, and per-process electrical draws for which no source was found. Not "
            "ecoinvent, not an IPCC GWP100 inventory, and not for reporting."
        ),
        is_illustrative=True,
        materials={
            "steel": MaterialFactor(
                kg_co2e_per_kg=1.91,
                basis=BASIS_PUBLISHED,
                source_title=_WORLDSTEEL_TITLE,
                source_url=_WORLDSTEEL_URL,
                source_quote=(
                    "CO2 emissions intensity   tonnes CO2 per tonne crude steel cast   "
                    "1.88 1.91 1.91** [2020, 2021, 2022]. It is calculated with the worldsteel "
                    "CO2 Data Collection methodology, which includes all scopes (1, 2, and some "
                    "scope 3)."
                ),
                note=(
                    "Global average CO2 (not CO2e) per tonne of crude steel as cast, 2022. "
                    "Applied here to the steel the estimate charged, which is downstream of "
                    "crude steel: a proxy, not a product footprint."
                ),
            ),
            "aluminium": MaterialFactor(
                kg_co2e_per_kg=14.8,
                basis=BASIS_PUBLISHED,
                source_title=_IAI_TITLE,
                source_url=_IAI_URL,
                source_quote=(
                    "The emissions intensity of primary aluminium production fell from 15.1 "
                    "tonnes of CO2e per tonne in 2022 to 14.8 tonnes of CO2e per tonne in 2023."
                ),
                note=(
                    "Global average for primary aluminium, 2023. Recycled content lowers it a "
                    "great deal (the same release gives 10.04 for primary and recycled "
                    "together); nothing here knows a material's recycled content."
                ),
            ),
        },
        process_electricity={
            "milling": ProcessElectricity(
                kwh_per_run_hour=7.5, basis=BASIS_NONE_FOUND, note=_NO_SOURCE_ELECTRICITY
            ),
            "turning": ProcessElectricity(
                kwh_per_run_hour=6.0, basis=BASIS_NONE_FOUND, note=_NO_SOURCE_ELECTRICITY
            ),
            "drilling": ProcessElectricity(
                kwh_per_run_hour=4.0, basis=BASIS_NONE_FOUND, note=_NO_SOURCE_ELECTRICITY
            ),
            "welding": ProcessElectricity(
                kwh_per_run_hour=5.0, basis=BASIS_NONE_FOUND, note=_NO_SOURCE_ELECTRICITY
            ),
        },
        grid={
            "GB": GridFactor(
                kg_co2e_per_kwh=0.20705,
                basis=BASIS_PUBLISHED,
                source_title=_DESNZ_TITLE,
                source_url=_DESNZ_URL,
                source_quote=(
                    "Electricity generated | Electricity: UK | kWh | 2024 | kg CO2e 0.20705"
                ),
                note="UK location-based grid average for the 2024 reporting year, Scope 2.",
            ),
            "IN": GridFactor(
                kg_co2e_per_kwh=0.727,
                basis=BASIS_PUBLISHED,
                source_title=_CEA_SECONDARY_TITLE,
                source_url=_CEA_SECONDARY_URL,
                source_quote=(
                    "As a result, the weighted average emission factor adjusted for renewable "
                    "energy was calculated at 0.727 tCO2/MWh."
                ),
                note=(
                    "Indian grid weighted average for 2023-24, CO2 only (0.727 t/MWh = "
                    "0.727 kg/kWh). Quoted from a trade report of the CEA database: the "
                    "CEA's own user guide returned 404 when it was fetched."
                ),
            ),
        },
    ),
)
