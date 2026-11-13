"""A CO2e estimate for one stored estimate — kilograms of CO2e, never money.

    stored estimate snapshots + emission-factor catalog -> **CO2e lines**

What it reads, and only from what the estimate stored:

- **material** — the mass the estimate *charged* for material, as stored: the
  finished part's, the confirmed bar's or the sheet share's (`part_cost.material`
  `mass_kg x quantity`), or, on a route whose first stage supplies the material
  (casting, forging, moulding ...), that stage line's own per-kg mass. A material
  supplied by a stage priced per piece has no mass basis and is not estimated.
- **machine electricity** — committed run time per process type, from the costed
  operations (`cycle_time_minutes x quantity / 60`), times the catalog's kWh per
  run hour, times the grid factor of the stated region. Setup time is not
  included: a machine being fixtured is not cutting, and no factor for its idle
  draw exists here.
- **route stages** — a per-kg stage line's mass times the catalog's stage factor
  when it has one. A per-piece stage has no basis and is not estimated.

A line that cannot be estimated is `not_estimated` with its reason — never zero —
and the total is then marked `partial`. Nothing here touches money: this module
imports only `emission_factors`, holds no rate, adds nothing to any cost and
converts no currency. Only `main.py` imports it. Deterministic: the same
snapshots and catalog give the same lines in the same order.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import BaseModel, Field, PlainSerializer

from cad_costing_agent.emission_factors import EmissionFactorCatalog


def _round_kg(value: float | None) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


# Kilograms of CO2e: exact in memory, three decimals on the way out. Its own unit,
# deliberately not `Money`, so it can never be summed into a cost by type.
KgCO2e = Annotated[float | None, PlainSerializer(_round_kg, return_type=float | None)]

ESTIMATED = "estimated"
NOT_ESTIMATED = "not_estimated"

COMPLETE = "complete"
PARTIAL = "partial"
NONE_ESTIMATED = "not_estimated"

CO2E_DISCLAIMER = (
    "Illustrative CO2e, in kilograms — not money. It changes no cost, is added to no total and "
    "converts no currency. Factors come from the catalog named here, each with its source or a "
    "statement that none was found; they are not an ecoinvent or IPCC GWP100 inventory, and a "
    "line that could not be estimated is shown with its reason rather than as zero."
)

METHOD = (
    "material: mass charged x quantity x kg CO2e/kg; machine electricity: committed run hours "
    "(cycle x quantity / 60) x kWh per run hour x grid kg CO2e/kWh; route stage: stage mass x "
    "quantity x stage kg CO2e/kg. Setup time is not included."
)


class Co2eLine(BaseModel):
    category: str = Field(description="'material', 'machine_electricity' or 'route_stage'.")
    key: str
    label: str
    status: str = Field(description=f"'{ESTIMATED}' or '{NOT_ESTIMATED}'.")
    reason: str | None = None
    # The basis, as read from the stored estimate.
    mass_kg: float | None = Field(default=None, description="Per part, as stored.")
    mass_source: str | None = None
    quantity: int | None = None
    run_hours: float | None = None
    kwh: float | None = None
    # The factor(s) applied, and where each came from.
    factor: float | None = None
    factor_unit: str | None = None
    factor_basis: str | None = None
    factor_source_title: str | None = None
    factor_source_url: str | None = None
    grid_factor_kg_co2e_per_kwh: float | None = None
    grid_basis: str | None = None
    grid_source_title: str | None = None
    grid_source_url: str | None = None
    kg_co2e: KgCO2e = None


class Co2eEstimate(BaseModel):
    estimate_id: str
    unit: str = "kg CO2e"
    emission_catalog_id: str
    emission_catalog_version: str
    catalog_is_illustrative: bool
    region: str | None = None
    region_source: str | None = Field(
        default=None, description="'request', 'rate_set_factory', or null when none was stated."
    )
    status: str = Field(description=f"'{COMPLETE}', '{PARTIAL}' or '{NONE_ESTIMATED}'.")
    quantity: int | None = None
    total_kg_co2e: KgCO2e = Field(
        default=None, description="Sum of the estimated lines only; null when none was estimated."
    )
    per_unit_kg_co2e: KgCO2e = None
    estimated_line_count: int = 0
    not_estimated_line_count: int = 0
    lines: list[Co2eLine] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    method: str = METHOD
    disclaimer: str = CO2E_DISCLAIMER


def _not(category: str, key: str, label: str, reason: str, **extra) -> Co2eLine:
    return Co2eLine(
        category=category, key=key, label=label, status=NOT_ESTIMATED, reason=reason, **extra
    )


def _material_line(part_cost: dict, result: dict, catalog: EmissionFactorCatalog) -> Co2eLine:
    material = part_cost.get("material") if isinstance(part_cost.get("material"), dict) else {}
    named = (material.get("material") or {}).get("name") if isinstance(material.get("material"), dict) else None
    name = str(named or "").strip()
    label = f"Material — {name or 'unnamed'}"
    key = name.lower()

    if part_cost.get("material_charged") is False:
        supplier = next(
            (
                line
                for line in part_cost.get("stage_costs") or []
                if isinstance(line, dict) and line.get("supplies_material")
            ),
            None,
        )
        if supplier is None:
            return _not(
                "material", key, label,
                "The estimate did not charge material and stored no stage that supplies it.",
            )
        if supplier.get("unit_basis") != "per_kg" or supplier.get("mass_kg") is None:
            return _not(
                "material", key, label,
                f"The material is supplied by the {supplier.get('label') or supplier.get('stage')} "
                "stage, priced per piece; the estimate stored no mass for it.",
            )
        mass = float(supplier["mass_kg"])
        quantity = int(supplier.get("quantity") or 1)
        source = f"stage:{supplier.get('stage')}" + (
            f" ({supplier['mass_source']})" if supplier.get("mass_source") else ""
        )
    else:
        if material.get("mass_kg") is None:
            return _not("material", key, label, "The estimate stored no material mass.")
        mass = float(material["mass_kg"])
        quantity = int(material.get("quantity") or 1)
        source = result.get("material_basis") or "finished_part"

    factor = catalog.materials.get(key)
    if factor is None:
        return _not(
            "material", key, label,
            f"The catalog has no factor for material '{name}'.",
            mass_kg=mass, mass_source=source, quantity=quantity,
        )
    return Co2eLine(
        category="material",
        key=key,
        label=label,
        status=ESTIMATED,
        mass_kg=mass,
        mass_source=source,
        quantity=quantity,
        factor=factor.kg_co2e_per_kg,
        factor_unit="kg CO2e per kg",
        factor_basis=factor.basis,
        factor_source_title=factor.source_title,
        factor_source_url=factor.source_url,
        kg_co2e=mass * quantity * factor.kg_co2e_per_kg,
    )


def _electricity_lines(
    part_cost: dict, catalog: EmissionFactorCatalog, region: str | None
) -> list[Co2eLine]:
    hours: dict[str, float] = {}
    for operation in part_cost.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        process = str(operation.get("process_type") or "unspecified").strip().lower()
        minutes = float(operation.get("cycle_time_minutes") or 0.0) * int(
            operation.get("quantity") or 1
        )
        hours[process] = hours.get(process, 0.0) + minutes / 60.0

    grid = catalog.grid.get(region) if region else None
    lines: list[Co2eLine] = []
    for process, run_hours in hours.items():
        label = f"Machine electricity — {process}"
        draw = catalog.process_electricity.get(process)
        if draw is None:
            lines.append(
                _not("machine_electricity", process, label,
                     f"The catalog has no electricity factor for process '{process}'.",
                     run_hours=run_hours)
            )
            continue
        kwh = run_hours * draw.kwh_per_run_hour
        common = dict(
            run_hours=run_hours,
            kwh=kwh,
            factor=draw.kwh_per_run_hour,
            factor_unit="kWh per run hour",
            factor_basis=draw.basis,
            factor_source_title=draw.source_title,
            factor_source_url=draw.source_url,
        )
        if region is None:
            lines.append(
                _not("machine_electricity", process, label,
                     "No region was given and the estimate's rate set states no factory region, "
                     "so there is no grid factor to apply.", **common)
            )
            continue
        if grid is None:
            lines.append(
                _not("machine_electricity", process, label,
                     f"The catalog has no grid factor for region '{region}'.", **common)
            )
            continue
        lines.append(
            Co2eLine(
                category="machine_electricity",
                key=process,
                label=label,
                status=ESTIMATED,
                grid_factor_kg_co2e_per_kwh=grid.kg_co2e_per_kwh,
                grid_basis=grid.basis,
                grid_source_title=grid.source_title,
                grid_source_url=grid.source_url,
                kg_co2e=kwh * grid.kg_co2e_per_kwh,
                **common,
            )
        )
    return lines


def _stage_lines(part_cost: dict, catalog: EmissionFactorCatalog) -> list[Co2eLine]:
    lines: list[Co2eLine] = []
    for stage in part_cost.get("stage_costs") or []:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("stage") or "")
        label = f"Route stage — {stage.get('label') or stage_id}"
        factor = catalog.stage_factors.get(stage_id)
        if stage.get("unit_basis") != "per_kg" or stage.get("mass_kg") is None:
            lines.append(
                _not("route_stage", stage_id, label,
                     "Priced per piece or as a batch charge only: the estimate stored no mass or "
                     "time for this stage, so there is no basis for a CO2e figure.")
            )
            continue
        mass = float(stage["mass_kg"])
        quantity = int(stage.get("quantity") or 1)
        if factor is None:
            lines.append(
                _not("route_stage", stage_id, label,
                     f"The catalog states no process factor for route stage '{stage_id}'.",
                     mass_kg=mass, mass_source=stage.get("mass_source"), quantity=quantity)
            )
            continue
        lines.append(
            Co2eLine(
                category="route_stage",
                key=stage_id,
                label=label,
                status=ESTIMATED,
                mass_kg=mass,
                mass_source=stage.get("mass_source"),
                quantity=quantity,
                factor=factor.kg_co2e_per_kg,
                factor_unit="kg CO2e per kg",
                factor_basis=factor.basis,
                factor_source_title=factor.source_title,
                factor_source_url=factor.source_url,
                kg_co2e=mass * quantity * factor.kg_co2e_per_kg,
            )
        )
    return lines


def estimate_co2e(
    *,
    estimate_id: str,
    result_snapshot: dict | None,
    catalog: EmissionFactorCatalog,
    region: str | None,
    region_source: str | None,
) -> Co2eEstimate:
    """The CO2e lines of one stored estimate, read from its own result snapshot."""
    result = result_snapshot or {}
    base = dict(
        estimate_id=estimate_id,
        emission_catalog_id=catalog.emission_catalog_id,
        emission_catalog_version=catalog.version,
        catalog_is_illustrative=catalog.is_illustrative,
        region=region,
        region_source=region_source,
    )
    # `/cost/cad-part` nests the part cost under `part_cost`; a hand-entered
    # `/cost/part` estimate stores the same breakdown flat.
    part_cost = result.get("part_cost")
    if not isinstance(part_cost, dict) and isinstance(result.get("material"), dict):
        part_cost = result
    if not isinstance(part_cost, dict):
        return Co2eEstimate(
            **base,
            status=NONE_ESTIMATED,
            notes=[
                "This estimate stored no part breakdown (an assembly roll-up stores one per "
                "component estimate). Ask for each component's own estimate instead."
            ],
        )

    lines = [
        _material_line(part_cost, result, catalog),
        *_electricity_lines(part_cost, catalog, region),
        *_stage_lines(part_cost, catalog),
    ]
    estimated = [line for line in lines if line.status == ESTIMATED]
    missing = [line for line in lines if line.status != ESTIMATED]
    material = part_cost.get("material") if isinstance(part_cost.get("material"), dict) else {}
    quantity = material.get("quantity")
    total = sum(float(line.kg_co2e) for line in estimated) if estimated else None

    notes: list[str] = []
    if not (part_cost.get("operations") or []):
        notes.append("No costed operations, so there is no committed machine time to estimate.")
    if missing and estimated:
        notes.append(
            f"Partial: {len(missing)} line(s) could not be estimated and are not in the total. "
            "The true figure is higher by an unknown amount."
        )
    if catalog.is_illustrative:
        notes.append("The catalog is illustrative. Do not report these figures.")

    return Co2eEstimate(
        **base,
        status=COMPLETE if estimated and not missing else PARTIAL if estimated else NONE_ESTIMATED,
        quantity=quantity,
        total_kg_co2e=total,
        per_unit_kg_co2e=(total / quantity) if total is not None and quantity else None,
        estimated_line_count=len(estimated),
        not_estimated_line_count=len(missing),
        lines=lines,
        notes=notes,
    )
