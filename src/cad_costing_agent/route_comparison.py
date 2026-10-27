"""Which routes a comparison previews, in what order, and how a result is reported.

A comparison puts the recommended route and the routes of the other families the
evidence is consistent with side by side, each priced as if the engineer had chosen
it. It is a preview of what each choice would cost; it is not an estimate, stores
nothing, and does not choose.

What is deliberately absent here:

- **No ranking.** The order is fixed by where a route came from (recommended first,
  then the candidates in catalog order). Nothing sorts by cost, and no row carries a
  "best" or "cheapest" flag. Choosing is the engineer's, through
  `/cad/process/decide`.
- **No arithmetic.** Every figure is copied from the result `price_cad_part`
  produced for that route. This module cannot price anything.
- **No dropped route.** A route that cannot be priced is a row with status `refused`
  and the reason in the pricing path's own words. A missing figure is `None`, never 0.

This module imports only `money` (so a figure is rounded exactly as an estimate's is): it is handed plain values and returns
plain values. `main.py` is the only caller, and is where the pricing happens.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field

from cad_costing_agent.money import Money

STATUS_PRICED = "priced"
STATUS_REFUSED = "refused"

ROLE_RECOMMENDED = "recommended"
ROLE_CANDIDATE = "candidate"

PREVIEW_NOTE = (
    "These are previews, not estimates. Each route was priced as if you had chosen it, with the "
    "same material, quantity, rate set, plan and entered masses; nothing was stored and no route "
    "was chosen. The routes are listed in a fixed order (the recommendation, then the other "
    "families the evidence is consistent with) and not ranked: a lower figure is not a better "
    "route, because shape does not determine process and a route's cost excludes what these "
    "inputs do not say. Operations without a committed time are omitted from a route's cost "
    "exactly as they are from an estimate, so a figure covers only the time that has been "
    "committed. A route that could not be priced is listed with the reason, not dropped or "
    "shown as zero."
)


def select_route_ids(
    recommended_route_id: str | None,
    candidate_families: Iterable[str],
    catalog: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    """The routes to compare, as `(route_id, role)`, in a stable order.

    The recommended route first, when there is one; then every catalog route whose
    family is a listed candidate, in catalog order, without repeating the
    recommendation. `catalog` is `(route_id, family)` pairs in catalog order, handed
    in so this module needs no import of it.
    """
    wanted = set(candidate_families)
    chosen: list[tuple[str, str]] = []
    if recommended_route_id is not None:
        chosen.append((recommended_route_id, ROLE_RECOMMENDED))
    for route_id, family in catalog:
        if family in wanted and route_id != recommended_route_id:
            chosen.append((route_id, ROLE_CANDIDATE))
    return chosen


def refusal_from_detail(status_code: int, detail) -> tuple[str, str, list]:
    """`(code, message, stages)` from an `HTTPException`, in its own words."""
    if isinstance(detail, dict):
        code = str(detail.get("error") or f"http_{status_code}")
        message = str(detail.get("message") or detail.get("detail") or code)
        stages = detail.get("stages") or []
        return code, message, list(stages) if isinstance(stages, list) else []
    return f"http_{status_code}", str(detail), []


class RouteStageSummary(BaseModel):
    stage: str
    label: str
    pricing: str = Field(description="'machining' (hourly rates) or 'stage_rate'.")


class RouteRefusal(BaseModel):
    code: str
    message: str
    stages: list = Field(default_factory=list, description="Per-stage problems, when given.")


class RouteComparisonRow(BaseModel):
    route_id: str
    route_label: str
    family: str
    role: str = Field(description="'recommended' or 'candidate'.")
    currently_decided: bool = Field(
        description="Whether this is the route the engineer has chosen now, if any."
    )
    stages: list[RouteStageSummary] = Field(default_factory=list)
    status: str = Field(description="'priced' or 'refused'.")
    refusal: RouteRefusal | None = None

    # Copied from the priced result; None on a refused row, never 0.
    currency: str | None = None
    total_part_cost: Money | None = None
    per_unit_cost: Money | None = None
    total_material_cost: Money | None = None
    total_operation_cost: Money | None = None
    total_stage_cost: Money | None = None
    total_additional_cost: Money | None = None
    material_charged: bool | None = None

    # What this route's cost leaves out of the plan, from the plan itself; None
    # when no plan was sent (operations then came as entered requirements).
    operations_costed: int | None = None
    operations_not_timed: int | None = None
    operations_out_of_route: int | None = None
    operations_as_formed: int | None = None
    # Per-kg stages as priced: the mass used and where it came from, so the page
    # can show it for editing without working anything out itself.
    stage_masses: list["StageMassView"] = Field(default_factory=list)


class StageMassView(BaseModel):
    stage: str
    label: str
    mass_kg: float
    mass_source: str | None = Field(
        default=None, description="'entered' or 'cad_finished_mass'."
    )
    mass_note: str = ""


RouteComparisonRow.model_rebuild()


class RouteComparison(BaseModel):
    kind: str = "preview"
    stored: bool = False
    chosen: str | None = Field(
        default=None, description="Always null: a comparison never chooses a route."
    )
    note: str = PREVIEW_NOTE
    rate_set: str | None = None
    quantity: int
    routes: list[RouteComparisonRow]
    not_compared: str | None = Field(
        default=None, description="Why there is nothing to compare, when there is not."
    )
