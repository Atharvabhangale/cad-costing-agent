import json
import math
from dataclasses import dataclass
import time
import mimetypes
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy.orm import Session
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from cad_costing_agent.db import get_session, init_db
from cad_costing_agent.dfm import (
    DEFAULT_DFM_PROFILE_ID,
    DfmProfile,
    DfmProfileSummary,
    DfmProfileVersionCreate,
    DfmReport,
    evaluate_dfm,
)
from cad_costing_agent.estimates import (
    EstimateAssumptions,
    EstimateComparison,
    EstimateDetail,
    EstimateListPage,
    compare_estimates,
    extract_assumptions,
)
from cad_costing_agent.cost_actuals import (
    ActualCostInput,
    ActualCostRecord,
    CostVariance,
    EstimateFigures,
    SupplierQuoteComparison,
    compare_supplier_quotes,
    compute_variance,
    figures_from_estimate,
)
from cad_costing_agent.repository import (
    DfmProfileRepository,
    CostActualRepository,
    EstimateRepository,
    IssuedQuotation,
    QuotationRepository,
    RateSetRepository,
    StoredEstimate,
    TimeProfileRepository,
)
from cad_costing_agent.quotation import (
    CUSTOMER,
    ENGINEERING,
    VIEWS,
    QuoteMetadata,
    QuoteRevision,
    Quotation,
    build_quotation,
)

from cad_costing_agent.additional_costs import AdditionalCostInputs
from cad_costing_agent.cad_geometry import (
    COMPLETE,
    MISSING_VOLUME,
    TOTAL_VOLUME,
    GeometryCompletion,
    GeometryProperties,
    complete_geometry,
    extract_geometry_from_bytes,
)
from cad_costing_agent.assembly_costing import (
    AssemblyCostSummary,
    ComponentEstimate,
    roll_up,
)
from cad_costing_agent.component_classification import (
    CLASSIFICATIONS,
    MANUFACTURED,
    ComponentCostingProfile,
    build_profiles,
    classification_feedback,
)
from cad_costing_agent.assembly_plan import (
    AssemblyManufacturingPlan,
    apply_edited_plans,
    build_assembly_plan,
)
from cad_costing_agent import feedback_analytics
from cad_costing_agent.feedback_analytics import (
    ClassificationAnalytics,
    ConfidenceAnalytics,
    DatasetReadiness,
    FeedbackFilters,
    FeedbackSummary,
    FeedbackWindow,
    OperationAnalytics,
    RecordPage,
    SetupAnalytics,
    TimeAnalytics,
)
from cad_costing_agent.limits import MAX_FEEDBACK_ESTIMATES, MIN_FEEDBACK_SAMPLE
from cad_costing_agent.cad_import import (
    CADDocument,
    FormatDescription,
    UnknownFormatError,
    UnsupportedFormatError,
    format_catalog,
    import_cad_document,
    require,
)
from cad_costing_agent.cad_import.model import ASSEMBLY as CAPABILITY_ASSEMBLY
from cad_costing_agent.cad_import.model import FEATURES as CAPABILITY_FEATURES
from cad_costing_agent.cad_import.model import GEOMETRY as CAPABILITY_GEOMETRY
from cad_costing_agent.cad_import.model import VIEWER as CAPABILITY_VIEWER
from cad_costing_agent.cad_mass import GeometryMassResult, calculate_mass_from_geometry
from cad_costing_agent.cad_mesh import (
    MAX_UPLOAD_BYTES,
    AssemblyMesh,
    CadViewerModel,
    MeshTooLargeError,
    build_assembly_mesh,
    build_viewer_model,
)
from cad_costing_agent.exploded_view import ExplodedViewState, build_exploded_view
from cad_costing_agent.features import (
    recognize_component_features,
    recognize_features_from_bytes,
)
from cad_costing_agent.features.model import FeatureRecognitionResult
from cad_costing_agent.operation_suggestions import (
    OperationSuggestionResult,
    UserAction,
    suggest_operations,
)
from cad_costing_agent.raw_stock import BarStock, propose_bar_stock, record_stock_decision
from cad_costing_agent.stage_costing import (
    MATERIAL_SUPPLYING_STAGES,
    StageInput,
    material_supplying_stage,
    price_stages,
    timed_stages,
)
from cad_costing_agent.process_classification import (
    FAMILY_LABELS,
    NOT_ASSESSED,
    STAGES,
    ManufacturingFamily,
    ProcessClassification,
    ProcessRoute,
    classify_process,
    plan_route_differences,
    record_route_decision,
    route_catalog,
)
from cad_costing_agent.operation_time import (
    OperationTimeRequest,
    OperationTimeResponse,
    estimate_operations,
)
from cad_costing_agent.manufacturing_plan import ManufacturingPlan, PlanTimeSummary, TimeSource
from cad_costing_agent.time_profiles import (
    SEED_TIME_PROFILES,
    TimeProfile,
    TimeProfileSummary,
    TimeProfileValidation,
    TimeProfileVersionCreate,
    advisory_notes,
)
from cad_costing_agent.operation_feedback import feedback_from_plan
from cad_costing_agent.cad_part_cost import CadPartCostEstimateResult, calculate_cad_part_cost
from cad_costing_agent.costing import MaterialCostResult, calculate_material_cost
from cad_costing_agent.operation import ManufacturingOperation
from cad_costing_agent.operation_costing import OperationCostResult, calculate_operation_cost
from cad_costing_agent.part import Part
from cad_costing_agent.part_cost import (
    PartCostEstimateRequest,
    PartCostEstimateResult,
    calculate_part_cost,
)
from cad_costing_agent.money import DEFAULT_CURRENCY, round_money
from cad_costing_agent.rate_sets import (
    OperationRequirement,
    RateSet,
    RateSetSummary,
    RateSetValidation,
    RateSetVersionCreate,
    resolve_material,
    resolve_operation,
)

_OPERATIONS_LIST_ADAPTER = TypeAdapter(list[ManufacturingOperation])
_REQUIREMENTS_LIST_ADAPTER = TypeAdapter(list[OperationRequirement])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables and seed the illustrative rate sets. Idempotent."""
    init_db()
    yield


app = FastAPI(title="CAD Costing Agent", lifespan=lifespan)

# The browser UI is plain static files served by this same app, so there is no
# build step and no second server to run. It is mounted under /ui rather than /
# so it cannot shadow any API route.
STATIC_PATH = Path(__file__).resolve().parent / "static"

# Windows resolves MIME types from the registry, where a stray entry can map
# .js to text/plain. Browsers refuse to execute an ES module served with a
# non-JavaScript type, which would break the 3D viewer with nothing but a
# console message to show for it. Pin the two that matter.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

class RevalidatedStaticFiles(StaticFiles):
    """Static files a browser must re-check before reusing.

    There is no build step here, so no filename ever carries a content hash:
    every release of the UI is served as `app.js`, `estimateflow.js` and so on
    at the same URLs forever. Starlette sends `etag` and `last-modified` but no
    `cache-control`, which leaves the browser free to apply *heuristic*
    freshness — Chrome then reuses its stored copy without asking the server
    whether it is still current.

    That is not theoretical. A fix to `estimateflow.js` was live on the server
    and absent from the running page: 13,403 cached bytes against 16,178 on
    disk, so a page loaded from the corrected server was executing the previous
    version of the file and quoting the wrong estimate. Any mismatch of this
    kind reads as an application bug, because every visible symptom is one.

    `no-cache` does not forbid caching; it requires revalidation. The etag is
    still sent and unchanged files still come back as an empty 304, so the cost
    is one conditional request per asset per load and the browser can never run
    a version of the UI the server is no longer serving.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers.setdefault("cache-control", "no-cache")
        return response


app.mount("/ui", RevalidatedStaticFiles(directory=STATIC_PATH, html=True), name="ui")


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    """Open the costing UI."""
    return RedirectResponse(url="/ui/")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/rates", response_model=list[RateSetSummary])
def rates(session: Session = Depends(get_session)) -> list[RateSetSummary]:
    """List every stored rate set, every version included.

    All catalog values are ILLUSTRATIVE POC VALUES, not real pricing.
    """
    return RateSetRepository(session).list()


def _stage_rate_problems(rate_set: RateSet) -> list[str]:
    """Stage-rate keys that are not a non-machining route stage.

    Checked here because the route catalog lives in `process_classification`,
    which the rate set module may not import. A machining stage is priced by
    the hour under `operations`, so a stage rate for one would be a second,
    contradictory price.
    """
    problems = []
    for key in rate_set.stage_rates:
        if key not in STAGES:
            known = ", ".join(sorted(k for k, (_, process) in STAGES.items() if process is None))
            problems.append(f"Unknown route stage '{key}' in stage rates. Stages: {known}.")
        elif STAGES[key][1] is not None:
            problems.append(
                f"'{key}' is a machining stage, priced by the hour under processes; it cannot "
                "also have a stage rate."
            )
    return problems


def _create_rate_set(rate_set: RateSet, session: Session) -> RateSet:
    """Persist one new rate set version, then return it as stored.

    Shared by both creation endpoints so the validation, the duplicate rule and
    the read-back live in exactly one place.
    """
    problems = _stage_rate_problems(rate_set)
    if problems:
        raise HTTPException(status_code=422, detail=" ".join(problems))
    repository = RateSetRepository(session)
    try:
        repository.add(rate_set)
    except ValueError as exc:
        # A version that already exists is immutable, so this is a conflict
        # rather than a malformed request.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    # Read back so the response is exactly what was persisted, not what was sent.
    return repository.get(rate_set.rate_set_id, rate_set.version)


@app.post("/rates/validate", response_model=RateSetValidation)
def validate_rate_set(
    rate_set: RateSet, session: Session = Depends(get_session)
) -> RateSetValidation:
    """Check a candidate rate set without storing it.

    The setup UI calls this from its review step so the "ready to publish"
    verdict comes from the backend rather than the browser. Nothing is written
    and no id is reserved.

    Malformed values never reach this function: `RateSet` rejects them as a 422
    first. What is left is the part that needs the stored catalog — whether
    this version already exists, which would make publishing a conflict because
    a published version is immutable.
    """
    repository = RateSetRepository(session)
    already_exists = repository.exists(rate_set.rate_set_id, rate_set.version)

    try:
        repository.get(rate_set.rate_set_id)
        is_new_profile = False
    except ValueError:
        is_new_profile = True

    messages: list[str] = []
    if already_exists:
        messages.append(
            f"Version {rate_set.version} of '{rate_set.rate_set_id}' has already been "
            "published, and a published version can never be changed. Choose a higher "
            "version number."
        )
    stage_problems = _stage_rate_problems(rate_set)
    messages.extend(stage_problems)
    if not rate_set.materials:
        messages.append("This profile prices no materials, so no part can be costed with it.")
    if not rate_set.operations:
        messages.append(
            "This profile prices no processes, so an estimate using it can only include "
            "material cost."
        )
    if rate_set.is_illustrative:
        messages.append(
            "This profile is marked as illustrative, so estimates made with it will carry "
            "a warning and must not be quoted."
        )

    return RateSetValidation(
        valid=not already_exists and not stage_problems,
        rate_set_id=rate_set.rate_set_id,
        name=rate_set.name,
        version=rate_set.version,
        currency=rate_set.currency,
        is_illustrative=rate_set.is_illustrative,
        material_count=len(rate_set.materials),
        process_count=len(rate_set.operations),
        version_already_exists=already_exists,
        is_new_profile=is_new_profile,
        messages=messages,
    )


@app.post("/rates", response_model=RateSet, status_code=201)
def create_rate_set(rate_set: RateSet, session: Session = Depends(get_session)) -> RateSet:
    """Author a new rate set version.

    Currency defaults to INR and `is_illustrative` defaults to true — set it to
    false when entering real rates. An existing `(rate_set_id, version)` is
    immutable: re-posting one returns 409, so changing rates means publishing a
    new version.
    """
    return _create_rate_set(rate_set, session)


@app.post("/rates/{rate_set_id}/versions", response_model=RateSet, status_code=201)
def create_rate_set_version(
    rate_set_id: str,
    version: RateSetVersionCreate,
    session: Session = Depends(get_session),
) -> RateSet:
    """Add a version to an existing rate set family, taking the id from the path."""
    try:
        rate_set = RateSet(rate_set_id=rate_set_id, **version.model_dump())
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _create_rate_set(rate_set, session)


@app.get("/rates/{rate_set_id}", response_model=RateSet)
def rate_set(
    rate_set_id: str, version: str | None = None, session: Session = Depends(get_session)
) -> RateSet:
    """Fetch one rate set. Omit `version` to get the latest version of that id."""
    try:
        return RateSetRepository(session).get(rate_set_id, version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/estimates", response_model=EstimateListPage)
def estimates(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    part_id: str | None = Query(None),
    rate_set_id: str | None = Query(None),
    created_after: datetime | None = Query(None, description="Inclusive lower bound, UTC."),
    created_before: datetime | None = Query(None, description="Inclusive upper bound, UTC."),
    session: Session = Depends(get_session),
) -> EstimateListPage:
    """Browse estimate history, newest first.

    Returns concise rows without the audit snapshots; fetch a single estimate
    to see those.
    """
    return EstimateRepository(session).list(
        limit=limit,
        offset=offset,
        part_id=part_id,
        rate_set_id=rate_set_id,
        created_after=created_after,
        created_before=created_before,
    )


# Declared before /estimates/{estimate_id} so "compare" is not read as an id.
@app.get("/estimates/compare", response_model=EstimateComparison)
def compare(
    baseline: str = Query(..., description="Estimate id to compare against."),
    comparison: str = Query(..., description="Estimate id being compared."),
    detail: bool = Query(
        True,
        description=(
            "Include the assumptions, input differences, cost decomposition and operation diff. "
            "Set false for totals only."
        ),
    ),
    session: Session = Depends(get_session),
) -> EstimateComparison:
    """Compare two persisted estimates, and say why they differ.

    Both sides come from what each estimate stored; nothing is recalculated, so
    publishing a newer rate set or time profile never changes a past comparison.

    Beyond the totals the answer carries the assumptions each side rested on,
    every input that differs, an exact decomposition of the money into the
    factors that moved it, and an operation-by-operation diff. Where several
    inputs changed at once it says so rather than inventing a causal split.

    Set `detail=false` for the totals-only comparison, which is cheaper when a
    caller only wants the headline numbers.
    """
    repository = EstimateRepository(session)
    try:
        baseline_summary = repository.get_summary(baseline)
        comparison_summary = repository.get_summary(comparison)
        if not detail:
            return compare_estimates(baseline_summary, comparison_summary)
        return compare_estimates(
            baseline_summary,
            comparison_summary,
            baseline_detail=_detail_of(repository, baseline),
            comparison_detail=_detail_of(repository, comparison),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _detail_of(repository: EstimateRepository, estimate_id: str) -> EstimateDetail:
    """The stored snapshots of one estimate, in the shape the comparison wants.

    Translating here rather than in `estimates.py` keeps that module free of any
    dependency on the repository layer, which already depends on it.
    """
    stored = repository.get(estimate_id)
    return EstimateDetail(
        estimate_id=stored.estimate_id,
        request_snapshot=stored.request_snapshot or {},
        result_snapshot=stored.result_snapshot or {},
        rate_set_snapshot=stored.rate_set_snapshot,
        plan_snapshot=stored.plan_snapshot,
        time_profile_snapshot=stored.time_profile_snapshot,
    )


@app.get("/estimates/{estimate_id}/assumptions", response_model=EstimateAssumptions)
def estimate_assumptions(
    estimate_id: str, session: Session = Depends(get_session)
) -> EstimateAssumptions:
    """Everything one estimate rested on: CAD, material, both profiles, the plan.

    Read back from the estimate's own snapshots rather than recomputed, so it
    reports what actually produced the number — including profile versions that
    may since have been superseded. A historical estimate keeps its own
    assumptions for good.
    """
    try:
        return extract_assumptions(_detail_of(EstimateRepository(session), estimate_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _quotation_revision(
    repository: EstimateRepository, estimate_id: str
) -> QuoteRevision | None:
    """The previous estimate of the same subject, as figures only.

    Read from both estimates' stored totals. No cause is attributed: the
    cost-impact analysis is a separate thing a reader can open, and printing a
    guess at *why* a price moved on a customer document is exactly the
    speculation this project refuses.
    """
    try:
        current = repository.get_summary(estimate_id)
    except ValueError:
        return None

    page = repository.list(limit=200, offset=0, part_id=current.part_id)
    earlier = [
        item
        for item in page.items
        if item.sequence < current.sequence and item.source == current.source
    ]
    if not earlier:
        return None
    previous = max(earlier, key=lambda item: item.sequence)

    comparable = previous.currency == current.currency
    return QuoteRevision(
        previous_estimate_id=previous.estimate_id,
        previous_total=previous.total_part_cost,
        current_total=current.total_part_cost,
        change=(current.total_part_cost - previous.total_part_cost) if comparable else None,
        currency=current.currency,
        comparable=comparable,
        note=(
            "Both totals are read from what each estimate recorded. No cause is attributed here."
            if comparable
            else (
                "The two estimates are denominated in different currencies and are never "
                "converted, so the difference is not shown."
            )
        ),
    )


def _build_quotation(
    session: Session,
    estimate_id: str,
    *,
    metadata: QuoteMetadata,
    view: str,
    issued_at: datetime | None = None,
) -> Quotation:
    """Render a quotation from one estimate's snapshots. Nothing is recalculated."""
    if view not in VIEWS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown view '{view}'. Known: {', '.join(VIEWS)}.",
        )
    repository = EstimateRepository(session)
    try:
        stored = repository.get(estimate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return build_quotation(
        _detail_of(repository, estimate_id),
        metadata=metadata,
        view=view,
        issued_at=issued_at,
        created_at=stored.created_at,
        revision=_quotation_revision(repository, estimate_id),
    )


@app.get("/estimates/{estimate_id}/quotation", response_model=Quotation)
def estimate_quotation(
    estimate_id: str,
    view: str = Query(
        CUSTOMER,
        description=(
            "'customer' for the document a buyer receives, 'engineering' to add the setups, "
            "operations, feature references, profile versions, component decisions and the "
            "system/user corrections. The customer view does not carry the engineering section "
            "at all, so internal correction records cannot travel in a forwarded document."
        ),
    ),
    customer_name: str = Query(""),
    project_name: str = Query(""),
    quote_reference: str = Query(""),
    prepared_by: str = Query(""),
    notes: str = Query(""),
    validity: str = Query(""),
    session: Session = Depends(get_session),
) -> Quotation:
    """Preview a quotation for one estimate, without issuing it.

    Every figure comes from the estimate's own immutable snapshots. Nothing is
    recalculated here, so this document reports what the estimate actually was
    even after newer rate sets or time profiles have been published.

    An estimate that must not become a customer-facing document — an assembly
    with components still needing costing input, one priced in more than one
    currency, or geometry that was never resolved — comes back with
    `available: false` and a blocker naming the reason. The engineering view is
    still rendered in that case, because the person fixing it needs to see it.
    """
    return _build_quotation(
        session,
        estimate_id,
        metadata=QuoteMetadata(
            customer_name=customer_name,
            project_name=project_name,
            quote_reference=quote_reference,
            prepared_by=prepared_by,
            notes=notes,
            validity=validity,
        ),
        view=view,
    )


class QuotationIssueRequest(BaseModel):
    """What a person types onto the document. Nothing is inferred from it."""

    customer_name: str = ""
    project_name: str = ""
    quote_number: str | None = Field(
        default=None,
        description=(
            "The reference to issue under. Omit it and a sequential one is generated. A number "
            "identifies one document and is never reused."
        ),
    )
    prepared_by: str = ""
    notes: str = ""
    validity: str = ""


class IssuedQuotationResult(BaseModel):
    """The issued record and the document it stands for."""

    issued: IssuedQuotation
    quotation: Quotation


@app.post("/estimates/{estimate_id}/quotation", response_model=IssuedQuotationResult)
def issue_quotation(
    estimate_id: str,
    request: QuotationIssueRequest,
    view: str = Query(CUSTOMER),
    session: Session = Depends(get_session),
) -> IssuedQuotationResult:
    """Issue a quotation for one estimate, and record that it was issued.

    Only the metadata and the totals as issued are stored. The figures are not
    copied: they are read back from the estimate's own snapshots whenever the
    document is rendered, so there is one source for them rather than two that
    could drift. `total_at_issue` is kept so a later re-render can be *checked*
    against what was sent.

    An estimate whose quotation is unavailable is refused rather than issued
    with a misleading total.
    """
    repository = QuotationRepository(session)
    number = (request.quote_number or "").strip() or repository.next_quote_number()
    metadata = QuoteMetadata(
        customer_name=request.customer_name,
        project_name=request.project_name,
        quote_reference=number,
        prepared_by=request.prepared_by,
        notes=request.notes,
        validity=request.validity,
    )
    issued_at = datetime.now(timezone.utc).replace(tzinfo=None)
    document = _build_quotation(
        session, estimate_id, metadata=metadata, view=view, issued_at=issued_at
    )

    if not document.available:
        raise HTTPException(
            status_code=409,
            detail=(
                "This estimate cannot be quoted: "
                + " ".join(blocker.detail for blocker in document.blockers)
            ),
        )

    try:
        issued = repository.issue(
            estimate_id=estimate_id,
            quote_number=number,
            customer_name=metadata.customer_name,
            project_name=metadata.project_name,
            quote_reference=number,
            prepared_by=metadata.prepared_by,
            notes=metadata.notes,
            validity=metadata.validity,
            currency=document.totals.currency if document.totals else "",
            total_at_issue=document.totals.total if document.totals else None,
            per_unit_at_issue=document.totals.per_unit if document.totals else None,
            issued_at=issued_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return IssuedQuotationResult(issued=issued, quotation=document)


@app.get("/quotations/{quote_number}", response_model=IssuedQuotationResult)
def quotation(
    quote_number: str,
    view: str = Query(CUSTOMER),
    session: Session = Depends(get_session),
) -> IssuedQuotationResult:
    """Retrieve an issued quotation, re-rendered from the estimate it presents.

    This is where historical accuracy is demonstrable rather than promised: the
    document comes back from the estimate's snapshots, which no later rate set,
    time profile, classification or plan can change, and `total_at_issue` on the
    stored record is there to be compared against it.
    """
    repository = QuotationRepository(session)
    try:
        issued = repository.get(quote_number)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    document = _build_quotation(
        session,
        issued.estimate_id,
        metadata=QuoteMetadata(
            customer_name=issued.customer_name,
            project_name=issued.project_name,
            quote_reference=issued.quote_reference,
            prepared_by=issued.prepared_by,
            notes=issued.notes,
            validity=issued.validity,
        ),
        view=view,
        issued_at=issued.issued_at,
    )
    return IssuedQuotationResult(issued=issued, quotation=document)


# ------------------------------------------------------------------ actual costs
#
# A client's real cost, recorded against an estimate and compared with it. The
# comparison is `cost_actuals.compute_variance`, which imports no calculator and
# no repository; it is handed the stored estimate as a plain carrier built here.
# Recording an actual changes no estimate, and nothing in costing reads them.
#
# A record is an actual or a supplier quote (`figure_kind`, required). These two
# endpoints are the only readers of the table, and neither aggregates across
# records: each entry is compared on its own and carries its kind, and the
# side-by-side table is built from the quotes alone.


class ActualCostEntry(BaseModel):
    record: ActualCostRecord
    is_current: bool = Field(description="False once a later record has corrected this one.")
    superseded_by: str | None = None
    variance: CostVariance


class EstimateActuals(BaseModel):
    estimate_id: str
    estimate_kind: str
    currency: str
    estimate_quantity: int
    entries: list[ActualCostEntry]
    supplier_quotes: SupplierQuoteComparison = Field(
        description=(
            "The current supplier quotes side by side, in the order entered. Never sorted "
            "by price; none is marked as preferred."
        )
    )


def _figures_of(estimate: StoredEstimate) -> EstimateFigures:
    return figures_from_estimate(
        estimate_id=estimate.estimate_id,
        source=estimate.source,
        quantity=estimate.quantity,
        currency=estimate.currency,
        total_material_cost=estimate.total_material_cost,
        total_operation_cost=estimate.total_operation_cost,
        total_additional_cost=estimate.total_additional_cost,
        total_part_cost=estimate.total_part_cost,
        result_snapshot=estimate.result_snapshot,
    )


def _stored_estimate(session: Session, estimate_id: str) -> StoredEstimate:
    try:
        return EstimateRepository(session).get(estimate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _entries(figures: EstimateFigures, records: list[ActualCostRecord]) -> list[ActualCostEntry]:
    superseded_by = {
        record.supersedes_actual_id: record.actual_id
        for record in records
        if record.supersedes_actual_id is not None
    }
    return [
        ActualCostEntry(
            record=record,
            is_current=record.actual_id not in superseded_by,
            superseded_by=superseded_by.get(record.actual_id),
            variance=compute_variance(figures, record),
        )
        for record in records
    ]


@app.post("/estimates/{estimate_id}/actuals", response_model=ActualCostEntry, status_code=201)
def record_actual_cost(
    estimate_id: str,
    entry: ActualCostInput,
    session: Session = Depends(get_session),
) -> ActualCostEntry:
    """Record an actual cost or a supplier quote against an estimate, and compare the two.

    `figure_kind` says which, and has no default. An actual is reported as a
    variance (estimate minus actual); a supplier quote as a gap (quote minus
    should-cost), with no verdict on either figure.

    Only the total is required. A breakdown category left out is reported as not
    supplied, never as zero. A per-batch cost must state its batch quantity, and
    an actual in a different currency from the estimate is refused rather than
    converted.

    Nothing is edited: to correct a record, send a new one with
    `supersedes_actual_id` naming it. The estimate itself is never changed.
    """
    figures = _figures_of(_stored_estimate(session, estimate_id))
    if entry.currency != figures.currency:
        raise HTTPException(
            status_code=422,
            detail=(
                f"This estimate is in {figures.currency} and the figure entered is in "
                f"{entry.currency}. Currencies are never converted; record it in the "
                "estimate's currency."
            ),
        )
    repository = CostActualRepository(session)
    try:
        record = repository.record(estimate_id, entry)
    except LookupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    entries = _entries(figures, repository.for_estimate(estimate_id))
    return next(item for item in entries if item.record.actual_id == record.actual_id)


@app.get("/estimates/{estimate_id}/actuals", response_model=EstimateActuals)
def estimate_actual_costs(
    estimate_id: str, session: Session = Depends(get_session)
) -> EstimateActuals:
    """Every actual and supplier quote recorded against an estimate, oldest first.

    Each entry carries its `figure_kind` and its own comparison. The current
    supplier quotes are also set side by side in `supplier_quotes`.

    A corrected record is still listed, marked not current, with the id of the
    record that replaced it.
    """
    figures = _figures_of(_stored_estimate(session, estimate_id))
    records = CostActualRepository(session).for_estimate(estimate_id)
    return EstimateActuals(
        estimate_id=estimate_id,
        estimate_kind=figures.kind,
        currency=figures.currency,
        estimate_quantity=figures.quantity,
        entries=_entries(figures, records),
        supplier_quotes=compare_supplier_quotes(figures, records),
    )


# ------------------------------------------------------------------ feedback
#
# Read-only analytics over the corrections four earlier steps recorded. Nothing
# in this section writes: no rule is changed, no profile is adjusted, no
# confidence is recalibrated and no estimate is touched. `feedback_analytics`
# imports nothing from this package at all, which is what makes that a
# structural fact rather than a promise — it is handed plain snapshots and hands
# back plain models.


def _feedback_filters(
    component: str | None,
    operation_type: str | None,
    feature_type: str | None,
    material: str | None,
    source_type: str | None,
    confidence: str | None,
    time_profile_version: str | None,
    rate_set_version: str | None,
    rule_id: str | None,
    corrected_only: bool,
) -> FeedbackFilters:
    return FeedbackFilters(
        component=component,
        operation_type=operation_type,
        feature_type=feature_type,
        material=material,
        source_type=source_type,
        confidence=confidence,
        time_profile_version=time_profile_version,
        rate_set_version=rate_set_version,
        rule_id=rule_id,
        corrected_only=corrected_only,
    )


def _feedback_dataset(session: Session, filters: FeedbackFilters, window_size: int):
    """Every recorded correction in the window, normalized and filtered.

    Read once from the estimates' own snapshots. The window is bounded by
    `CAD_COSTING_MAX_FEEDBACK_ESTIMATES` and the response says when it
    truncated, because an answer over the most recent N estimates is useful
    while one that timed out is not.
    """
    repository = EstimateRepository(session)
    available = repository.count()
    size = min(window_size, MAX_FEEDBACK_ESTIMATES)
    stored = repository.recent_details(size)

    operations = []
    classifications = []
    for estimate in stored:
        rows, pairs = feedback_analytics.extract_records(estimate)
        operations.extend(rows)
        classifications.extend(pairs)

    kept = [record for record in operations if feedback_analytics.matches(record, filters)]
    if filters.material or filters.component:
        # A classification record has a component and a material but no
        # operation, so only those two filters can apply to it. Any other filter
        # is about an operation, and silently applying it would make the
        # classification counts disagree with what the filter says it selected.
        classifications = [
            entry
            for entry in classifications
            if (filters.component is None
                or str(entry.component_name).lower() == filters.component.lower()
                or str(entry.definition_id).lower() == filters.component.lower())
            and (filters.material is None
                 or (entry.material or "").lower() == filters.material.lower())
        ]

    truncated = len(stored) < available
    window = FeedbackWindow(
        estimates_read=len(stored),
        estimates_available=available,
        truncated=truncated,
        note=(
            f"The {len(stored)} most recent of {available} stored estimates. Raise "
            "CAD_COSTING_MAX_FEEDBACK_ESTIMATES to widen the window."
            if truncated
            else f"Every one of the {available} stored estimates."
        ),
    )
    return kept, classifications, window


@app.get("/feedback/summary", response_model=FeedbackSummary)
def feedback_summary(
    component: str | None = Query(None, description="Component name or definition id."),
    operation_type: str | None = Query(None, description="Process, as suggested or as chosen."),
    feature_type: str | None = Query(None),
    material: str | None = Query(None),
    source_type: str | None = Query(None, description="'brep' or 'faceted'."),
    confidence: str | None = Query(
        None, description="Matches operation, time or feature confidence."
    ),
    time_profile_version: str | None = Query(None),
    rate_set_version: str | None = Query(None),
    rule_id: str | None = Query(None),
    corrected_only: bool = Query(False),
    window: int = Query(
        MAX_FEEDBACK_ESTIMATES, description="How many recent estimates to read.", ge=1
    ),
    minimum_sample: int = Query(
        MIN_FEEDBACK_SAMPLE,
        ge=1,
        description=(
            "Observations before a pattern is reported as actionable. Not a significance "
            "test — a floor below which a median is a coincidence."
        ),
    ),
    session: Session = Depends(get_session),
) -> FeedbackSummary:
    """Where engineers disagree with the system, in aggregate.

    Every rate carries the numerator, the denominator and a sample guard, and a
    figure resting on too few observations says so instead of showing a
    percentage. **Nothing here learns**: no rule, profile, confidence value or
    estimate is changed by asking this question, and the response says so.

    Read entirely from the estimates' own persisted snapshots, so publishing a
    newer rate set or time profile cannot alter a single number.
    """
    filters = _feedback_filters(
        component,
        operation_type,
        feature_type,
        material,
        source_type,
        confidence,
        time_profile_version,
        rate_set_version,
        rule_id,
        corrected_only,
    )
    records, classifications, read = _feedback_dataset(session, filters, window)
    return feedback_analytics.summarise(
        records,
        classifications,
        minimum=minimum_sample,
        window=read,
        filters=filters,
    )


@app.get("/feedback/operations", response_model=OperationAnalytics)
def feedback_operations(
    component: str | None = Query(None),
    operation_type: str | None = Query(None),
    feature_type: str | None = Query(None),
    material: str | None = Query(None),
    source_type: str | None = Query(None),
    confidence: str | None = Query(None),
    time_profile_version: str | None = Query(None),
    rate_set_version: str | None = Query(None),
    rule_id: str | None = Query(None),
    corrected_only: bool = Query(False),
    window: int = Query(MAX_FEEDBACK_ESTIMATES, ge=1),
    minimum_sample: int = Query(MIN_FEEDBACK_SAMPLE, ge=1),
    session: Session = Depends(get_session),
) -> OperationAnalytics:
    """The system → engineer confusion matrix, and each rule's hit rate.

    This is where a systematically wrong suggestion rule becomes visible: a
    cell like `milling → turning 12` is the rule proposing a process engineers
    keep replacing. Nothing is changed as a result — measuring a rule and
    editing one are different steps, and this is the measuring one.
    """
    filters = _feedback_filters(
        component,
        operation_type,
        feature_type,
        material,
        source_type,
        confidence,
        time_profile_version,
        rate_set_version,
        rule_id,
        corrected_only,
    )
    records, _, read = _feedback_dataset(session, filters, window)
    return feedback_analytics.operation_report(
        records, minimum=minimum_sample, window=read, filters=filters
    )


@app.get("/feedback/times", response_model=TimeAnalytics)
def feedback_times(
    component: str | None = Query(None),
    operation_type: str | None = Query(None),
    feature_type: str | None = Query(None),
    material: str | None = Query(None),
    source_type: str | None = Query(None),
    confidence: str | None = Query(None),
    time_profile_version: str | None = Query(None),
    rate_set_version: str | None = Query(None),
    rule_id: str | None = Query(None),
    corrected_only: bool = Query(False),
    window: int = Query(MAX_FEEDBACK_ESTIMATES, ge=1),
    minimum_sample: int = Query(MIN_FEEDBACK_SAMPLE, ge=1),
    session: Session = Depends(get_session),
) -> TimeAnalytics:
    """System against engineer time, per process and per profile version.

    Medians carry the headline because one engineer typing 90 minutes where the
    system said 0.6 would otherwise define a shop's entire bias. Outliers are
    listed separately and still counted. A directional bias is claimed only
    above the sample threshold; below it the finding is "not enough evidence".
    """
    filters = _feedback_filters(
        component,
        operation_type,
        feature_type,
        material,
        source_type,
        confidence,
        time_profile_version,
        rate_set_version,
        rule_id,
        corrected_only,
    )
    records, _, read = _feedback_dataset(session, filters, window)
    return feedback_analytics.time_report(
        records, minimum=minimum_sample, window=read, filters=filters
    )


@app.get("/feedback/setups", response_model=SetupAnalytics)
def feedback_setups(
    component: str | None = Query(None),
    operation_type: str | None = Query(None),
    material: str | None = Query(None),
    source_type: str | None = Query(None),
    time_profile_version: str | None = Query(None),
    corrected_only: bool = Query(False),
    window: int = Query(MAX_FEEDBACK_ESTIMATES, ge=1),
    minimum_sample: int = Query(MIN_FEEDBACK_SAMPLE, ge=1),
    session: Session = Depends(get_session),
) -> SetupAnalytics:
    """How often the system's fixturing assumption was changed.

    Reported as a correction *frequency*, never as an error rate: the system
    assumes one setup at low confidence precisely because nothing in the CAD
    proves a second is needed, so an engineer splitting it is a person supplying
    information the geometry does not carry.
    """
    filters = _feedback_filters(
        component,
        operation_type,
        None,
        material,
        source_type,
        None,
        time_profile_version,
        None,
        None,
        corrected_only,
    )
    records, _, read = _feedback_dataset(session, filters, window)
    return feedback_analytics.setup_report(
        records, minimum=minimum_sample, window=read, filters=filters
    )


@app.get("/feedback/classifications", response_model=ClassificationAnalytics)
def feedback_classifications(
    component: str | None = Query(None),
    material: str | None = Query(None),
    window: int = Query(MAX_FEEDBACK_ESTIMATES, ge=1),
    minimum_sample: int = Query(MIN_FEEDBACK_SAMPLE, ge=1),
    session: Session = Depends(get_session),
) -> ClassificationAnalytics:
    """Which make/buy suggestions engineers changed, and to what.

    The Step 26 suggestion is deliberately low-confidence — no CAD format states
    make/buy — so this is the correction record that matters most. It reports
    what engineers chose and nothing about what a component truly is beyond that
    choice.
    """
    filters = _feedback_filters(
        component, None, None, material, None, None, None, None, None, False
    )
    _, classifications, _ = _feedback_dataset(session, filters, window)
    return feedback_analytics.classification_report(
        classifications, minimum=minimum_sample
    )


@app.get("/feedback/confidence", response_model=ConfidenceAnalytics)
def feedback_confidence(
    component: str | None = Query(None),
    operation_type: str | None = Query(None),
    material: str | None = Query(None),
    source_type: str | None = Query(None),
    time_profile_version: str | None = Query(None),
    window: int = Query(MAX_FEEDBACK_ESTIMATES, ge=1),
    minimum_sample: int = Query(MIN_FEEDBACK_SAMPLE, ge=1),
    session: Session = Depends(get_session),
) -> ConfidenceAnalytics:
    """Empirical acceptance by confidence band, and B-Rep against faceted.

    An **empirical** rate: confidence here is a three-way classification of
    which rule branches fired, never a probability, so a higher acceptance rate
    at `high` is a measured association and not evidence of calibration. B-Rep
    and faceted evidence are never combined silently.
    """
    filters = _feedback_filters(
        component, operation_type, None, material, source_type, None,
        time_profile_version, None, None, False,
    )
    records, _, read = _feedback_dataset(session, filters, window)
    return feedback_analytics.confidence_report(
        records, minimum=minimum_sample, window=read, filters=filters
    )


class EvidenceReport(BaseModel):
    """How complete the recorded evidence is, field by field."""

    window: FeedbackWindow = Field(default_factory=FeedbackWindow)
    filters: FeedbackFilters = Field(default_factory=FeedbackFilters)
    minimum_sample: int = 0
    readiness: DatasetReadiness = Field(default_factory=DatasetReadiness)
    disclaimer: str = feedback_analytics.LEARNING_DISCLAIMER


@app.get("/feedback/evidence", response_model=EvidenceReport)
def feedback_evidence(
    component: str | None = Query(None),
    operation_type: str | None = Query(None),
    feature_type: str | None = Query(None),
    material: str | None = Query(None),
    source_type: str | None = Query(None),
    confidence: str | None = Query(None),
    time_profile_version: str | None = Query(None),
    rate_set_version: str | None = Query(None),
    rule_id: str | None = Query(None),
    corrected_only: bool = Query(False),
    window: int = Query(MAX_FEEDBACK_ESTIMATES, ge=1),
    minimum_sample: int = Query(MIN_FEEDBACK_SAMPLE, ge=1),
    session: Session = Depends(get_session),
) -> EvidenceReport:
    """How complete the recorded evidence is, and what can never be completed.

    Each field is classified rather than counted: `known`, `not_applicable`
    where the value legitimately cannot exist for that record, `unrecorded`
    where something persisted could still recover it, and `unrecoverable` where
    it was never captured and nothing stored can bring it back.

    That distinction is the point. Measuring the real database found two of the
    three gaps this reports were not gaps: every single-part record was counted
    as missing a component identity, which a standalone part does not have, and
    every unfinished operation was counted as missing a paired time, which
    Step 29 requires it not to have.

    `percent_known` divides by the records where the value *could* exist. An
    unknown is never converted into a guess to improve it, and **nothing here
    learns** — this is an evidence-quality report, not a training system.
    """
    filters = _feedback_filters(
        component,
        operation_type,
        feature_type,
        material,
        source_type,
        confidence,
        time_profile_version,
        rate_set_version,
        rule_id,
        corrected_only,
    )
    records, _, read = _feedback_dataset(session, filters, window)
    return EvidenceReport(
        window=read,
        filters=filters,
        minimum_sample=minimum_sample,
        readiness=feedback_analytics.readiness(records, minimum_sample),
    )


@app.get("/feedback/records", response_model=RecordPage)
def feedback_records(
    component: str | None = Query(None),
    operation_type: str | None = Query(None),
    feature_type: str | None = Query(None),
    material: str | None = Query(None),
    source_type: str | None = Query(None),
    confidence: str | None = Query(None),
    time_profile_version: str | None = Query(None),
    rate_set_version: str | None = Query(None),
    rule_id: str | None = Query(None),
    corrected_only: bool = Query(False),
    system_operation: str | None = Query(
        None, description="Drill into one confusion cell: what the system suggested."
    ),
    user_operation: str | None = Query(
        None, description="Drill into one confusion cell: what the engineer chose."
    ),
    window: int = Query(MAX_FEEDBACK_ESTIMATES, ge=1),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> RecordPage:
    """The individual records behind a statistic, paginated.

    This is what makes a percentage checkable: a reader can ask which twelve
    corrections produced `milling → turning 12` and see the component, the
    feature, the prediction and the decision for each. The browser is given a
    page, never the whole history.
    """
    filters = _feedback_filters(
        component,
        operation_type,
        feature_type,
        material,
        source_type,
        confidence,
        time_profile_version,
        rate_set_version,
        rule_id,
        corrected_only,
    )
    records, _, read = _feedback_dataset(session, filters, window)

    if system_operation is not None:
        records = [r for r in records if (r.system_operation or "") == system_operation]
    if user_operation is not None:
        records = [r for r in records if (r.user_operation or "") == user_operation]

    return RecordPage(
        window=read,
        filters=filters,
        total=len(records),
        limit=limit,
        offset=offset,
        items=records[offset : offset + limit],
    )


@app.get("/estimates/{estimate_id}", response_model=StoredEstimate)
def estimate(estimate_id: str, session: Session = Depends(get_session)) -> StoredEstimate:
    """Retrieve a persisted estimate, including its audit snapshots."""
    try:
        return EstimateRepository(session).get(estimate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/cost/material", response_model=MaterialCostResult)
def cost_material(part: Part) -> MaterialCostResult:
    try:
        return calculate_material_cost(part)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/cost/operation", response_model=OperationCostResult)
def cost_operation(operation: ManufacturingOperation) -> OperationCostResult:
    return calculate_operation_cost(operation)


@app.post("/cost/part", response_model=PartCostEstimateResult)
def cost_part(
    request: PartCostEstimateRequest, session: Session = Depends(get_session)
) -> PartCostEstimateResult:
    try:
        # The rate set is resolved here, in the layer that owns the session,
        # and injected into the pure calculator.
        resolved_rate_set = (
            RateSetRepository(session).get(request.rate_set_id, request.rate_set_version)
            if request.rate_set_id is not None
            else None
        )
        result = calculate_part_cost(request, rate_set=resolved_rate_set)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result.estimate_id = EstimateRepository(session).save(
        source="cost/part",
        part_id=request.part.part_id,
        part_name=request.part.part_name,
        material=request.part.material,
        quantity=request.part.quantity,
        rate_set=resolved_rate_set,
        currency=result.currency,
        total_material_cost=result.total_material_cost,
        total_operation_cost=result.total_operation_cost,
        total_additional_cost=result.total_additional_cost,
        total_part_cost=result.total_part_cost,
        request_snapshot=request.model_dump(mode="json"),
        result_snapshot=result.model_dump(mode="json"),
    )
    session.commit()
    return result


def _import(
    data: bytes, filename: str, capability: str, *, with_geometry: bool = True
) -> CADDocument:
    """Import an upload and assert it can do what this endpoint needs.

    The one place an upload becomes a `CADDocument`. No endpoint parses a file
    extension, so none of them can disagree about what a format supports.

    Two refusals, deliberately given different statuses because they are
    different problems:

    - **415 Unsupported Media Type** when nothing at all can be read from the
      format, carrying the adapter's own explanation rather than a 400 that
      would read as though the file were malformed. A `.drw` upload is not a
      broken file — it is a drawing, and the response says exactly that.
    - **400 Bad Request** when the format reads perfectly but cannot do this
      particular thing. An STL asked for feature recognition is the standing
      example: the file is fine, and a triangle mesh simply carries no
      features. This is the status that path has always returned, and it keeps
      returning it.
    """
    try:
        document = import_cad_document(data, filename, with_geometry=with_geometry)
    except UnsupportedFormatError as exc:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "unsupported_cad_format",
                "source_format": exc.source_format,
                "format_label": exc.format_label,
                "message": str(exc),
            },
        ) from exc
    except UnknownFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        require(document, capability)
    except UnsupportedFormatError as exc:
        if document.any_support:
            # Readable format, unavailable capability: a plain 400 with the
            # adapter's explanation, which is what these endpoints have always
            # returned for an STL asked to have its features recognized.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise HTTPException(
            status_code=415,
            detail={
                "error": "unsupported_cad_format",
                "capability": capability,
                "source_format": document.source_format,
                "format_label": document.format_label,
                "message": str(exc),
            },
        ) from exc
    return document


def _geometry_of(document: CADDocument) -> GeometryProperties:
    """The document's geometry, or the file's own reason for not having any.

    Reached when a capability is `requires_user_input` rather than
    `unsupported`: the format can be measured in general, but *this* file is
    missing something the application will not invent. An IGES file with no
    unit declaration is the standing case — OpenCASCADE would read it as
    inches, so it is refused with that explanation instead of measured 25.4
    times too large.

    A 400 rather than a 415, because the format is fine and the file is not,
    and a plain string detail, because that is what every other unreadable-file
    refusal on these endpoints returns.
    """
    if document.geometry is None:
        raise HTTPException(
            status_code=400, detail=document.capability(CAPABILITY_GEOMETRY).detail
        )
    return document.geometry


@app.get("/cad/formats", response_model=list[FormatDescription])
async def cad_formats() -> list[FormatDescription]:
    """Every CAD format this application recognizes, and what each can do.

    Needs no file. The upload UI renders this rather than keeping its own list,
    so the interface cannot advertise a capability the backend does not have —
    these are the same declarations the endpoints gate on.
    """
    return format_catalog()


@app.post("/cad/import", response_model=CADDocument)
async def cad_import(
    file: UploadFile = File(...),
    with_geometry: bool = Query(
        True,
        description=(
            "Set false to identify the file and report its capabilities without parsing its "
            "geometry — milliseconds even on a 200 MB export."
        ),
    ),
) -> CADDocument:
    """Identify an uploaded CAD file and report honestly what can be done with it.

    The normalized representation everything downstream consumes: the format,
    what the file actually contains, where its units came from, its component
    structure, its header metadata, and a capability report per capability with
    a stated reason for anything less than full support.

    Calculates no mass and no cost. `detect_ms` and `parse_ms` are reported
    separately so the import layer's own cost is visible rather than hidden
    inside one total.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"CAD file is {len(data) / 1024 / 1024:.1f} MB, above the "
                f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB limit for this POC."
            ),
        )
    # Identification is reported for every recognized format, including those
    # that cannot be parsed: being told what a `.prt` file is, and why it is
    # refused, is the point of this endpoint.
    try:
        return import_cad_document(data, file.filename or "", with_geometry=with_geometry)
    except UnknownFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class AssemblyImportResult(BaseModel):
    """A decomposed assembly, and optionally a mesh the viewer can select in."""

    document: CADDocument
    mesh: AssemblyMesh | None = None
    mesh_error: str | None = Field(
        default=None,
        description=(
            "Why there is no preview. An assembly whose components carry no geometry still "
            "has a structure, so this is never a failure of the whole request."
        ),
    )
    exploded_view: ExplodedViewState | None = Field(
        default=None,
        description=(
            "A display-only exploded arrangement: each occurrence's CAD placement beside the "
            "offset it would take at full explosion. The viewer interpolates between them, so "
            "the slider needs no request and no recalculation. It is computed from placements "
            "alone, so an assembly whose components cannot be drawn still has one — and it is "
            "a visual arrangement, never an assembly sequence."
        ),
    )
    component_costing: list[ComponentCostingProfile] = Field(
        default_factory=list,
        description=(
            "A starting costing profile per component definition: the system's classification "
            "suggestion with the evidence for it, the material the CAD file states if it "
            "states one, and empty user fields. The UI edits these and sends them back to "
            "`/cost/assembly`. They live here rather than on `CADDocument` because a "
            "classification is a costing decision about a component, not a fact about the "
            "file — the import layer must stay free of it."
        ),
    )


def _assembly_document(
    data: bytes, filename: str, *, measure: bool, with_mesh: bool
) -> AssemblyImportResult:
    """Decompose one upload, reading its structure exactly once.

    The structure, the per-component measurements and the viewer mesh all need
    the same shapes, and reading them again per output would triple a cost that
    is already the most expensive thing this application does.
    """
    from cad_costing_agent.cad_assembly import read_step_assembly
    from cad_costing_agent.cad_import import decompose_assembly, detect_format

    adapter, _ = detect_format(filename, data)

    # Whether a format carries a component hierarchy is the adapter's answer,
    # not a comparison against a format name here.
    structure = None
    read_ms = 0.0
    if adapter.reads_assembly_structure:
        started = time.perf_counter()
        structure = read_step_assembly(data)
        read_ms = (time.perf_counter() - started) * 1000.0

    document = import_cad_document(
        data,
        filename,
        with_geometry=False,
        with_assembly=True,
        assembly_structure=structure,
    )
    if structure is not None and not measure:
        document = document.model_copy(
            update={
                "assembly": decompose_assembly(
                    data, filename, adapter, measure_components=False, structure=structure
                )
            }
        )

    mesh = None
    mesh_error = None
    if with_mesh:
        if structure is None:
            mesh_error = (
                f"{adapter.label} carries no component structure, so there is nothing to draw "
                "component by component. The ordinary preview is unaffected."
            )
        else:
            try:
                mesh = build_assembly_mesh(structure)
            except MeshTooLargeError as exc:
                mesh_error = str(exc)
            except ValueError as exc:
                mesh_error = str(exc)

    # `decompose_assembly` was handed an already-read structure, so its own
    # timer saw only the translation. Reporting that as the cost of decomposing
    # a 198 MB assembly would be off by three orders of magnitude.
    if document.assembly is not None and read_ms:
        document = document.model_copy(
            update={
                "assembly": document.assembly.model_copy(
                    update={"read_ms": document.assembly.read_ms + read_ms}
                )
            }
        )

    # Derived from the placements the document already carries: no geometry is
    # read, nothing is re-parsed, and the result feeds only the viewer.
    exploded = (
        build_exploded_view(document.assembly)
        if document.assembly is not None and document.assembly.occurrences
        else None
    )

    # Suggestions only. Nothing is decided here: every profile says the
    # classification came from the system and carries the caveat that no CAD
    # format states make/buy.
    profiles = (
        build_profiles(document.assembly) if document.assembly is not None else []
    )

    return AssemblyImportResult(
        document=document,
        mesh=mesh,
        mesh_error=mesh_error,
        exploded_view=exploded,
        component_costing=profiles,
    )


@app.post("/cad/assembly", response_model=AssemblyImportResult)
async def cad_assembly(
    file: UploadFile = File(...),
    measure: bool = Query(
        True,
        description=(
            "Measure each component's own geometry. Set false for the structure alone, which "
            "is much cheaper on a large assembly."
        ),
    ),
    with_mesh: bool = Query(
        False,
        description=(
            "Also build a viewer mesh carrying a range per component, so the browser can "
            "highlight and isolate one."
        ),
    ),
) -> AssemblyImportResult:
    """Decompose an uploaded CAD file into its components.

    Returns the normalized document with its `assembly`: every definition with
    its own geometry where the file carries it, every occurrence with its
    placement, the hierarchy, and the quantities.

    This is deliberately a separate endpoint from `/cad/import`. Measured on the
    198 MB Creo export, identifying the file takes about 1.4 seconds and reading
    its structure about 20, so the expensive answer is given when it is asked
    for and never as a side effect of an upload.

    A component the file carries no geometry for is named, counted and placed
    with its geometry reported as absent. The assembly's aggregate figures are
    never used in its place.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"CAD file is {len(data) / 1024 / 1024:.1f} MB, above the "
                f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB limit for this POC."
            ),
        )
    try:
        return _assembly_document(
            data, file.filename or "", measure=measure, with_mesh=with_mesh
        )
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except UnknownFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/cad/assembly/features", response_model=FeatureRecognitionResult)
async def cad_assembly_features(
    file: UploadFile = File(...),
    component_id: str = Form(..., description="The component definition to analyse."),
) -> FeatureRecognitionResult:
    """Recognize features on one component of an assembly.

    The recognizers are the same ones a standalone part goes through, run on
    that component's own shape. Feature ids come back prefixed with the
    component, so two components that each have a first hole do not both call
    it `H-001` and an operation can still be traced to the hole that caused it.
    """
    from cad_costing_agent.cad_assembly import read_step_assembly

    data = await file.read()
    _import(data, file.filename or "", CAPABILITY_FEATURES, with_geometry=False)

    try:
        structure = read_step_assembly(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    definition = structure.definitions.get(component_id)
    if definition is None:
        known = ", ".join(sorted(structure.definitions)[:20])
        raise HTTPException(
            status_code=404,
            detail=f"No component '{component_id}' in this file. Known components: {known}",
        )
    if not definition.has_geometry:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Component '{definition.name}' carries no geometry the reader could "
                "transfer, so there is nothing to analyse. It is still named, counted and "
                "placed in the assembly structure."
            ),
        )

    try:
        return recognize_component_features(
            definition.shape,
            component_id=definition.name or component_id,
            component_name=definition.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _component_decisions(components: str | None) -> dict[str, dict]:
    """Parse the classification decisions a request carries, strictly.

    A malformed body is refused rather than partially applied: half-applied
    classifications would silently cost some components the wrong way.
    """
    if not components:
        return {}
    try:
        parsed = json.loads(components)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"`components` is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, list):
        raise HTTPException(
            status_code=400,
            detail="`components` must be a JSON list of per-component costing decisions.",
        )

    decisions: dict[str, dict] = {}
    for entry in parsed:
        if not isinstance(entry, dict) or not entry.get("definition_id"):
            raise HTTPException(
                status_code=400,
                detail="Every entry in `components` needs a `definition_id`.",
            )
        classification = entry.get("user_classification")
        if classification is not None and classification not in CLASSIFICATIONS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown classification '{classification}'. "
                    f"Known: {', '.join(CLASSIFICATIONS)}."
                ),
            )
        decisions[entry["definition_id"]] = entry
    return decisions


_PLANS_ADAPTER = TypeAdapter(dict[str, ManufacturingPlan])


def _component_plan_payload(component_plans: str | None) -> dict[str, ManufacturingPlan]:
    """Parse the edited component plans a request carries, strictly.

    A malformed body is refused rather than partly applied: half-applied plans
    would cost some components from an edit and others from a stale plan, and
    nothing in the response would say which.
    """
    if not component_plans:
        return {}
    try:
        parsed = json.loads(component_plans)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"`component_plans` is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail=(
                "`component_plans` must be a JSON object mapping a component definition id to "
                "that component's manufacturing plan."
            ),
        )
    try:
        return _PLANS_ADAPTER.validate_python(parsed)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc


def _resolve_time_profile(
    session: Session, time_profile_id: str | None, time_profile_version: str | None
):
    """The time profile a plan's predictions are made under, or the built-in default."""
    if not time_profile_id:
        return None
    try:
        return TimeProfileRepository(session).get(time_profile_id, time_profile_version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _component_suggestions(data: bytes, structure, definitions, priced_processes=None):
    """Recognize features and suggest operations **per component**.

    Each component's own shape goes through the same recognizers a standalone
    part does, so a housing's holes are never attributed to a shaft. Feature ids
    come back prefixed with the component, which is what makes an operation
    traceable back to the geometry that caused it.

    A component whose shape the reader could not transfer is skipped rather than
    given the assembly's aggregate — the whole point of Step 24.
    """
    results: dict[str, object] = {}
    features: dict[str, FeatureRecognitionResult] = {}
    for definition in definitions:
        source = structure.definitions.get(definition.definition_id)
        if source is None or not source.has_geometry:
            continue
        try:
            recognition = recognize_component_features(
                source.shape,
                component_id=definition.name or definition.definition_id,
                component_name=definition.name,
            )
        except ValueError:
            # A component whose geometry defeats recognition leaves the
            # component unplanned with its reason, rather than taking the
            # whole request down with it.
            continue
        features[definition.definition_id] = recognition
        results[definition.definition_id] = suggest_operations(
            recognition, priced_processes=priced_processes
        )
    return results, features


class AssemblyPlanResult(BaseModel):
    """An assembly's component plans, with the per-component recognition behind them."""

    plan: AssemblyManufacturingPlan
    features: dict[str, FeatureRecognitionResult] = Field(
        default_factory=dict,
        description=(
            "Recognition per component definition, keyed by definition id. Present so the "
            "viewer can link an operation back to the faces that caused it without a second "
            "parse of the file."
        ),
    )
    suggestions: dict[str, OperationSuggestionResult] = Field(
        default_factory=dict,
        description="The suggestions each component's plan was built from, keyed the same way.",
    )
    read_ms: float = 0.0


@app.post("/cad/assembly/plans", response_model=AssemblyPlanResult)
async def cad_assembly_plans(
    file: UploadFile = File(...),
    components: str | None = Form(
        None,
        description=(
            "The same JSON list of per-component costing decisions `/cost/assembly` takes. "
            "Only components whose effective classification is 'manufactured' are planned; "
            "every other one comes back with its status and the reason."
        ),
    ),
    time_profile_id: str | None = Form(
        None, description="Time profile the predicted times are made under. Omit for the built-in."
    ),
    time_profile_version: str | None = Form(None),
    rate_set_id: str | None = Form(
        None,
        description=(
            "Optional, and only used to report which suggested processes your rate set already "
            "prices. It never changes what is suggested and no rate is read from it."
        ),
    ),
    session: Session = Depends(get_session),
) -> AssemblyPlanResult:
    """Build a manufacturing plan for every manufactured component of an assembly.

    One upload, read once. For each **manufactured** component definition this
    runs the same pipeline a standalone part goes through — its own feature
    recognition, the same suggestion rules, the same time estimator, the same
    setup model — and returns one plan per definition.

    There is deliberately **no assembly-level plan**. A setup is one physical
    fixturing of one part, so two components never share one however alike their
    processes look, and nothing here knows what order components are made in.

    A purchased, standard or excluded component comes back `not_applicable` with
    the reason; an unresolved or geometry-less one comes back `blocked` with what
    is missing. Neither is reported as a plan with zero operations.

    One plan per **definition**, not per occurrence: twelve identical bolts are
    made the same way once and placed twelve times, and multiplying the plan
    would multiply its setup time too.
    """
    from cad_costing_agent.cad_assembly import read_step_assembly

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"CAD file is {len(data) / 1024 / 1024:.1f} MB, above the "
                f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB limit for this POC."
            ),
        )

    decisions = _component_decisions(components)
    started = time.perf_counter()
    imported = _assembly_document(data, file.filename or "", measure=True, with_mesh=False)
    assembly = imported.document.assembly
    if assembly is None or assembly.structure_state != "structure_available":
        raise HTTPException(
            status_code=400,
            detail=(
                "This file carries no component structure, so there are no components to plan. "
                "A single part is planned through /cad/operations/suggest and the operations "
                "table, exactly as before."
            ),
        )

    try:
        profiles = build_profiles(assembly, decisions=decisions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    unknown = set(decisions) - {profile.definition_id for profile in profiles}
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=f"No component definition(s) {', '.join(sorted(unknown))} in this assembly.",
        )

    classifications = {profile.definition_id: profile.classification for profile in profiles}
    manufactured = [
        definition
        for definition in assembly.definitions
        if classifications.get(definition.definition_id) == MANUFACTURED
        and not definition.is_assembly
    ]

    priced_processes = None
    if rate_set_id:
        try:
            priced_processes = list(RateSetRepository(session).get(rate_set_id).operations.keys())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # The structure is read once more here because recognition needs the OCCT
    # shapes, which never cross the normalized boundary. The document above is
    # what says *which* components to look at.
    structure = read_step_assembly(data)
    suggestions, features = _component_suggestions(
        data, structure, manufactured, priced_processes
    )

    profile = _resolve_time_profile(session, time_profile_id, time_profile_version)
    plan = build_assembly_plan(
        assembly,
        classifications,
        suggestions,
        units="mm",
        profile=profile,
        source_filename=file.filename or "",
    )

    return AssemblyPlanResult(
        plan=plan,
        features=features,
        suggestions=suggestions,
        read_ms=(time.perf_counter() - started) * 1000.0,
    )


@app.post("/cost/assembly", response_model=AssemblyCostSummary)
async def cost_assembly(
    file: UploadFile = File(...),
    material: str = Form(..., description="Default material for manufactured components."),
    source_unit: str = Form("mm"),
    rate_set_id: str | None = Form(None),
    rate_set_version: str | None = Form(None),
    components: str | None = Form(
        None,
        description=(
            "A JSON list of per-component costing decisions, one object per component "
            "definition: `definition_id` plus any of `user_classification`, `material`, "
            "`unit_cost`, `currency`, `supplier`, `supplier_part_number`, "
            "`exclusion_reason`, `classification_reason`, `notes`. Anything else in an "
            "object is ignored, so a round trip cannot rewrite what the system suggested or "
            "how many of a component the file contains."
        ),
    ),
    assembly_quantity: int = Form(
        1,
        description=(
            "How many of this assembly are being built. Not a property of the CAD file and "
            "never confused with a component's occurrence count: the two are multiplied "
            "exactly once, in the roll-up."
        ),
    ),
    component_plans: str | None = Form(
        None,
        description=(
            "A JSON object mapping a component definition id to that component's "
            "`ManufacturingPlan`. A manufactured component with a plan is costed from it — "
            "material plus the operations in the plan that carry a committed time, with setup "
            "charged once per setup — exactly as a standalone part is. One plan per "
            "definition: the plan is not repeated per occurrence, its per-part cost is "
            "multiplied by the occurrence count in the roll-up. A component with no plan is "
            "costed for material only, and the roll-up says so."
        ),
    ),
    time_profile_id: str | None = Form(
        None,
        description=(
            "Recorded on the estimate as the profile the component plans' predicted times "
            "were made under. Snapshotted whole, so publishing a later version cannot change "
            "what this estimate says it assumed."
        ),
    ),
    time_profile_version: str | None = Form(None),
    session: Session = Depends(get_session),
) -> AssemblyCostSummary:
    """Roll component costs up into an assembly total.

    Each component contributes according to its classification, and nothing
    else. A **manufactured** component is costed as a part by the unchanged
    costing engine; a **purchased** or **standard** one contributes the unit
    cost the caller stated; an **excluded** one contributes nothing on purpose;
    an **unresolved** one contributes nothing and leaves the total incomplete.

    Changing a component to purchased genuinely removes it from the
    manufacturing path — no geometry is costed for it, no material is applied
    and no operations are considered — rather than costing it and then hiding
    the number.

    There is still no assembly labour and no fastening time, and the summary
    says so on every response.

    A component that cannot be costed is listed with the reason and leaves the
    total marked incomplete. It is never counted as zero.
    """
    data = await file.read()
    decisions = _component_decisions(components)
    plans = _component_plan_payload(component_plans)
    imported = _assembly_document(data, file.filename or "", measure=True, with_mesh=False)
    assembly = imported.document.assembly
    if assembly is None:  # pragma: no cover - decompose always returns one
        raise HTTPException(status_code=400, detail="This file could not be decomposed.")

    try:
        profiles = build_profiles(assembly, decisions=decisions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    unknown = set(decisions) - {profile.definition_id for profile in profiles}
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No component definition(s) {', '.join(sorted(unknown))} in this assembly. "
                "A decision about a component the file does not contain is more likely a "
                "mismatched file than a typo, so nothing was costed."
            ),
        )

    resolved_rate_set = (
        RateSetRepository(session).get(rate_set_id, rate_set_version)
        if rate_set_id is not None
        else None
    )
    resolved_time_profile = _resolve_time_profile(session, time_profile_id, time_profile_version)

    # Only manufactured components go through the costing engine. This is the
    # whole point of the classification: a bought bearing is not costed by its
    # volume of steel and then quietly ignored — it never enters the pipeline.
    classifications = {profile.definition_id: profile.classification for profile in profiles}
    assembly_plan = build_assembly_plan(
        assembly, classifications, {}, units=source_unit, source_filename=file.filename or ""
    )
    try:
        assembly_plan = apply_edited_plans(assembly_plan, plans)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    estimates: dict[str, ComponentEstimate] = {}
    materials_used: set[str] = set()
    plan_facts: dict[str, dict] = {}
    for profile in profiles:
        if profile.classification != MANUFACTURED:
            continue
        definition = assembly.definition(profile.definition_id)
        if definition.is_assembly or definition.geometry is None:
            continue
        quantity = max(definition.quantity, 1)
        component_material = profile.material or material
        materials_used.add(component_material)

        # The component's own plan supplies its operations, through the same
        # bridge a standalone part uses: `to_operation_requirements` charges each
        # setup's time once, on the first costable operation in it. No plan means
        # material only — and the roll-up reports the plan status beside the cost
        # so "not planned yet" is never read as "needs no machining".
        entry = assembly_plan.component(definition.definition_id)
        requirements = (
            entry.plan.to_operation_requirements(quantity) if entry.plan is not None else []
        )
        if entry.plan is not None:
            summary_ = entry.time_summary or entry.plan.time_summary(quantity)
            plan_facts[definition.definition_id] = {
                "status": entry.status,
                "operation_count": len(entry.plan.active),
                "setup_count": len(entry.plan.setups),
                "setup_time_minutes": summary_.total_setup_time_minutes,
                "cycle_time_minutes": summary_.total_cycle_time_minutes,
            }
        else:
            plan_facts[definition.definition_id] = {
                "status": entry.status,
                "operation_count": 0,
                "setup_count": 0,
                "setup_time_minutes": None,
                "cycle_time_minutes": None,
            }

        try:
            component = calculate_cad_part_cost(
                geometry=definition.geometry,
                material_name=component_material,
                source_unit=source_unit,
                part_id=definition.definition_id,
                part_name=definition.name,
                quantity=quantity,
                operations=[],
                operation_requirements=requirements,
                additional_costs=None,
                rate_set_id=rate_set_id,
                rate_set_version=rate_set_version,
                rate_set=resolved_rate_set,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        cost = component.part_cost
        # Setup and run are separated from figures the engine already produced:
        # `setup_cost` is its own field on every costed operation.
        setup_total = sum(operation.setup_cost for operation in cost.operations)
        run_total = sum(
            operation.machine_cost + operation.labor_cost for operation in cost.operations
        )
        estimates[definition.definition_id] = ComponentEstimate(
            definition_id=definition.definition_id,
            estimate_id=cost.estimate_id or definition.definition_id,
            currency=cost.currency,
            per_unit_cost=cost.per_unit_cost,
            material_per_unit=cost.total_material_cost / quantity,
            operation_per_unit=cost.total_operation_cost / quantity,
            additional_per_unit=cost.total_additional_cost / quantity,
            setup_per_unit=setup_total / quantity,
            run_per_unit=run_total / quantity,
        )

    summary = roll_up(
        assembly,
        estimates,
        profiles=profiles,
        assembly_quantity=assembly_quantity,
        plans=plan_facts,
    )
    planned = [entry for entry in assembly_plan.component_plans if entry.has_plan]
    if planned:
        needing = [entry for entry in planned if entry.missing]
        summary.notes.append(
            f"{len(planned)} component(s) were costed from their own manufacturing plan: each "
            "component's operations and setups are its own, and setup time is charged once per "
            "setup rather than once per operation."
            + (
                f" {len(needing)} of them still has an input missing, and those operations were "
                "left out of the cost rather than defaulted."
                if needing
                else ""
            )
        )
    if materials_used:
        summary.notes.append(
            f"Manufactured components were costed as {', '.join(sorted(materials_used))} "
            f"against {('rate set ' + rate_set_id) if rate_set_id else 'the default rates'}. "
            "Where the CAD file states no material, the material given for the assembly was "
            "applied; that is an assumption, not a reading."
        )
    feedback = classification_feedback(profiles)
    changed = [record for record in feedback if record.classification_changed]
    if changed:
        summary.notes.append(
            f"{len(changed)} component classification(s) were changed from the system's "
            "suggestion. Both the suggestion and the correction are recorded on this "
            "estimate; nothing is trained on them and a later suggestion is unaffected."
        )

    # Persisted through the existing snapshot architecture, with no new table:
    # the structure, the quantities and every component's cost travel in the
    # result snapshot, so a later CAD change cannot rewrite what this estimate
    # was made from.
    # Both the classification decisions and the resulting contributions travel
    # in the snapshots, so a historical estimate keeps what each component was
    # classified as, at what quantity, for what unit cost. A later change to
    # the same file cannot rewrite it.
    # The id is kept, not discarded: it is what lets a quotation be produced
    # from this roll-up instead of from the part estimate that happens to be in
    # the browser's hand.
    summary.estimate_id = EstimateRepository(session).save(
        source="cost/assembly",
        part_id=assembly.assembly_id,
        part_name=assembly.name,
        material=material,
        quantity=assembly_quantity,
        rate_set=resolved_rate_set,
        currency=summary.currency or "INR",
        total_material_cost=(summary.material_cost or 0.0) * assembly_quantity,
        total_operation_cost=(summary.operation_cost or 0.0) * assembly_quantity,
        total_additional_cost=(summary.additional_cost or 0.0) * assembly_quantity,
        total_part_cost=summary.production_total_cost or 0.0,
        request_snapshot={
            "source_file": file.filename,
            "material": material,
            "source_unit": source_unit,
            "rate_set_id": rate_set_id,
            "rate_set_version": rate_set_version,
            "assembly_quantity": assembly_quantity,
            "time_profile_id": time_profile_id,
            "time_profile_version": time_profile_version,
            "component_costing": [
                profile.model_dump(mode="json") for profile in profiles
            ],
            "assembly": {
                "name": assembly.name,
                "structure_state": assembly.structure_state,
                "definition_count": assembly.definition_count,
                "occurrence_count": assembly.occurrence_count,
                "max_depth": assembly.max_depth,
                "definitions": [
                    definition.model_dump(mode="json") for definition in assembly.definitions
                ],
                "occurrences": [
                    occurrence.model_dump(mode="json") for occurrence in assembly.occurrences
                ],
            },
        },
        result_snapshot={
            **summary.model_dump(mode="json"),
            "classification_feedback": [
                record.model_dump(mode="json") for record in feedback
            ],
        },
        # The component plans travel in the existing plan snapshot — no second
        # persistence system — each with one feedback row per operation carrying
        # the component that produced it. A later edit makes a new estimate with
        # its own snapshot rather than rewriting this one.
        plan_snapshot=(
            {
                "assembly_plan": assembly_plan.model_dump(mode="json"),
                "component_feedback": {
                    entry.definition_id: [
                        record.model_dump(mode="json")
                        for record in feedback_from_plan(
                            entry.plan,
                            component_id=entry.definition_id,
                            component_name=entry.component_name,
                        )
                    ]
                    for entry in planned
                },
                "uncosted_operation_ids": {
                    entry.definition_id: [
                        operation.operation_id
                        for operation in entry.plan.uncosted_operations()
                    ]
                    for entry in planned
                },
                "note": (
                    "One plan per component definition, never per occurrence. Operations "
                    "without a committed cycle time were left out of the cost rather than "
                    "defaulted. Each component's setups are its own: a setup is one physical "
                    "fixturing of one part, so two components never share one."
                ),
            }
            if planned
            else None
        ),
        time_profile_snapshot=(
            {
                "profiles": {
                    resolved_time_profile.time_profile_id: resolved_time_profile.model_dump(
                        mode="json"
                    )
                }
            }
            if resolved_time_profile is not None
            else None
        ),
    )
    session.commit()
    return summary


@app.post("/cad/geometry", response_model=GeometryProperties)
async def cad_geometry(file: UploadFile = File(...)) -> GeometryProperties:
    """Extract geometry properties from an uploaded CAD file.

    This does not calculate mass or cost — it is not connected to costing.
    """
    data = await file.read()
    document = _import(data, file.filename or "", CAPABILITY_GEOMETRY)
    return _geometry_of(document)


@app.post("/cad/mesh", response_model=CadViewerModel)
async def cad_mesh(file: UploadFile = File(...)) -> CadViewerModel:
    """Geometry properties plus a browser-renderable triangle mesh.

    Everything the 3D viewer panel needs from one upload. `geometry` is the
    same value `/cad/geometry` returns for these bytes, because it comes from
    the same extractor.

    Visualization only — this endpoint calculates no mass and no cost. A model
    that parses but is too dense to preview still returns its geometry, with
    `mesh` null and `mesh_error` explaining why, so costing is never blocked
    by a rendering limit.
    """
    data = await file.read()
    document = _import(data, file.filename or "", CAPABILITY_VIEWER)
    try:
        # The geometry measured during import is handed on, so these bytes are
        # parsed for measurement once per request rather than twice.
        return build_viewer_model(data, file.filename or "", document.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_geometry(
    geometry: GeometryProperties,
    method: str | None,
    volume_mm3: float | None,
) -> GeometryProperties:
    """Apply a user's geometry assumption, or say precisely what is needed.

    A model whose polygons do not enclose a solid has no volume to measure. That
    is not an error to hide behind a 400: the caller is told what is known, what
    is missing and which resolutions are supported, so it can ask the user. Only
    when the user answers does a volume exist, and it is labelled as theirs.
    """
    supplied = method is not None or volume_mm3 is not None

    if geometry.geometry_status == COMPLETE:
        if supplied:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This model's geometry could be measured, so a geometry assumption was not "
                    "expected. Remove it to use the measured volume."
                ),
            )
        return geometry

    if not supplied:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "needs_geometry_input",
                "message": (
                    "This CAD file does not contain enough geometry to measure the part's "
                    "volume. Supply the missing information to continue."
                ),
                "geometry": (
                    geometry.incomplete.model_dump(mode="json")
                    if geometry.incomplete is not None
                    else None
                ),
            },
        )

    if method is None or volume_mm3 is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "A geometry assumption needs both a method "
                f"('{TOTAL_VOLUME}' or '{MISSING_VOLUME}') and a volume in mm³."
            ),
        )

    try:
        return complete_geometry(
            geometry, GeometryCompletion(method=method, volume_mm3=volume_mm3)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/cad/features", response_model=FeatureRecognitionResult)
async def cad_features(file: UploadFile = File(...)) -> FeatureRecognitionResult:
    """Recognize manufacturing-relevant features in an uploaded STEP file.

    STEP-first: recognition needs the exact faces and edges of a B-Rep, which
    a triangle mesh does not carry, so an STL upload is refused here with an
    explanation rather than guessed at. STL remains fully supported for
    geometry, the 3D preview and costing.

    This branches from geometry; it does not touch costing, changes no rates
    and persists nothing. It also does not infer manufacturing operations — a
    hole is reported as a hole.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"CAD file is {len(data) / 1024 / 1024:.1f} MB, above the "
                f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB limit for this POC."
            ),
        )
    # Identified without parsing geometry: recognition reads the file itself,
    # and measuring it here would be a second pass for a number this endpoint
    # does not report.
    _import(data, file.filename or "", CAPABILITY_FEATURES, with_geometry=False)
    try:
        return recognize_features_from_bytes(data, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a bad model must not take the app down
        raise HTTPException(
            status_code=500,
            detail=(
                "Feature recognition failed unexpectedly on this model "
                f"({type(exc).__name__}). Geometry, the 3D preview and costing are unaffected."
            ),
        ) from exc


class OperationSuggestionResponse(OperationSuggestionResult):
    """The suggestions, plus the manufacturing family and route they add up to.

    Classified from the same recognition in the same request, so the route can
    never be read from a different parse than the operations it summarises.
    """

    process_classification: ProcessClassification
    raw_stock: BarStock | None = Field(
        default=None,
        description=(
            "For a body of revolution: the bar it would be turned from, proposed for a person to "
            "confirm. Null for any other part. Changes no cost until confirmed."
        ),
    )


@app.post("/cad/operations/suggest", response_model=OperationSuggestionResponse)
async def cad_suggest_operations(
    file: UploadFile = File(...),
    rate_set_id: str | None = Form(
        None,
        description=(
            "Optional. Only used to report whether your rate set already prices a suggested "
            "process. It never changes what is suggested, and no rate is read from it."
        ),
    ),
    rate_set_version: str | None = Form(
        None, description="Exact rate set version to compare against. Omit for the latest."
    ),
    session: Session = Depends(get_session),
) -> OperationSuggestionResponse:
    """Suggest manufacturing operations for the features recognized in a STEP file.

    Runs the same recognition `/cad/features` does — one upload, one parse, no
    duplicated recognition logic — and then applies deterministic
    feature-to-operation rules to the normalized features.

    **Suggestions only.** Nothing here is costed, no estimate is touched, no
    rate is read or invented, and nothing is persisted. Every suggestion names
    the feature that caused it, the rule that fired and the evidence it stood
    on, and reports `costable: false` with what is missing — this step does not
    estimate machining time, so a suggestion becomes a priced operation only
    once you accept it and enter the times yourself.

    Features the geometry cannot support an operation for are returned in
    `declined` with the reason, rather than being guessed at or dropped.

    `process_classification` recommends a manufacturing family and route from
    the same features. It is a recommendation awaiting review, with its
    evidence; it changes no suggestion and prices nothing.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"CAD file is {len(data) / 1024 / 1024:.1f} MB, above the "
                f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB limit for this POC."
            ),
        )

    # Resolved only to list which processes the rate set prices. A missing or
    # unknown rate set is not fatal: suggestions do not depend on rates, so it
    # degrades to "no rate set compared" rather than refusing to suggest.
    priced_processes: list[str] | None = None
    resolved_version: str | None = None
    if rate_set_id:
        try:
            selected = RateSetRepository(session).get(rate_set_id, rate_set_version)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        priced_processes = list(selected.operations.keys())
        resolved_version = selected.version

    _import(data, file.filename or "", CAPABILITY_FEATURES, with_geometry=False)
    try:
        recognition = recognize_features_from_bytes(data, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a bad model must not take the app down
        raise HTTPException(
            status_code=500,
            detail=(
                "Feature recognition failed unexpectedly on this model "
                f"({type(exc).__name__}). Geometry, the 3D preview and costing are unaffected."
            ),
        ) from exc

    suggestions = suggest_operations(
        recognition,
        priced_processes=priced_processes,
        rate_set_id=rate_set_id,
        rate_set_version=resolved_version,
    )
    return OperationSuggestionResponse(
        **suggestions.model_dump(),
        process_classification=classify_process(recognition, suggestions),
        raw_stock=propose_bar_stock(list(recognition.features)),
    )


class StockDecisionRequest(BaseModel):
    stock: BarStock
    action: str = Field(description="'confirm', 'edit' (with a diameter and/or length) or 'withdraw'.")
    diameter_mm: float | None = None
    length_mm: float | None = None


@app.post("/cad/stock/decide", response_model=BarStock)
def decide_stock(request: StockDecisionRequest) -> BarStock:
    """Confirm the proposed bar, enter the bar actually used, or withdraw.

    Persists nothing on its own: the bar travels with the cost request and is
    stored on the estimate it produced. The proposal is never changed.
    """
    try:
        return record_stock_decision(
            request.stock,
            request.action,
            diameter_mm=request.diameter_mm,
            length_mm=request.length_mm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------- manufacturing process
#
# The family and route a part is made by. The system recommends one alongside
# the operation suggestions; these endpoints let an engineer review it. Neither
# prices anything: `/cost/cad-part` reads the decision only to refuse a route
# the existing costing cannot price.


class ProcessFamilyOption(BaseModel):
    family: ManufacturingFamily
    label: str


class ProcessRouteCatalog(BaseModel):
    families: list[ProcessFamilyOption]
    routes: list[ProcessRoute]
    not_assessed: list[str]
    yield_stages: list[str] = Field(
        default_factory=list,
        description=(
            "Stage ids whose rate includes the material. Priced per kg, such a stage may carry "
            "an engineer-entered yield; the page offers the input for these and no others."
        ),
    )


@app.get("/process-routes", response_model=ProcessRouteCatalog)
def process_routes() -> ProcessRouteCatalog:
    """Every manufacturing route an engineer can choose, and whether each stage is costable.

    Only milling, drilling and turning stages carry a cost model. The rest are
    here so a route can be recorded truthfully, not so it can be priced.
    """
    return ProcessRouteCatalog(
        families=[
            ProcessFamilyOption(family=family, label=label)
            for family, label in FAMILY_LABELS.items()
        ],
        routes=route_catalog(),
        not_assessed=list(NOT_ASSESSED),
        yield_stages=sorted(MATERIAL_SUPPLYING_STAGES),
    )


class ProcessDecisionRequest(BaseModel):
    classification: ProcessClassification
    action: UserAction = Field(
        description="'accepted', 'edited' (with route_id; 'unknown' marks it unknown) or 'pending'."
    )
    route_id: str | None = None
    note: str | None = None


@app.post("/cad/process/decide", response_model=ProcessClassification)
def decide_process(request: ProcessDecisionRequest) -> ProcessClassification:
    """Record the engineer's answer beside the recommendation, which stays unchanged.

    Persists nothing on its own: the decision travels with the cost request and
    is stored on the estimate it produced.
    """
    try:
        return record_route_decision(
            request.classification,
            request.action,
            route_id=request.route_id,
            note=request.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------- time profiles
#
# The exact counterpart of the rate-set endpoints, and separate from them on
# purpose: a rate set decides what an hour costs, a time profile decides how
# long the work takes. A shop revises its feeds and speeds on a different
# schedule from its labour rate, and an estimate has to be able to say which
# version of each produced it.


@app.get("/time-profiles", response_model=list[TimeProfileSummary])
def time_profiles(session: Session = Depends(get_session)) -> list[TimeProfileSummary]:
    """List every stored time-assumption profile, every version included.

    The built-in baseline holds ILLUSTRATIVE POC VALUES, not any real shop's
    feeds and speeds.
    """
    return TimeProfileRepository(session).list()


def _snapshot_time_profiles(plan: ManufacturingPlan, session: Session) -> dict | None:
    """Capture every time profile a plan's predictions were made under.

    Keyed by `id@version` so it stays uniform whether a plan used none, one, or
    (if a caller hand-assembled it) several. A profile no longer in the catalog
    is recorded as missing rather than silently dropped — an estimate should be
    able to say it cannot show its assumptions, not pretend it had none.
    """
    referenced = {
        (operation.system_time_profile_id, operation.system_time_profile_version)
        for operation in plan.operations
        if operation.system_time_profile_id
    }
    if not referenced:
        return None

    repository = TimeProfileRepository(session)
    profiles: dict[str, dict] = {}
    for profile_id, version in sorted(referenced):
        key = f"{profile_id}@{version}"
        try:
            profiles[key] = repository.get(profile_id, version).model_dump(mode="json")
        except ValueError as exc:
            profiles[key] = {"unavailable": str(exc)}
    return {
        "profiles": profiles,
        "note": (
            "The time-assumption profile(s) the predicted times were made under, captured whole. "
            "Publishing a later version of a profile does not change this estimate."
        ),
    }


def _create_time_profile(profile: TimeProfile, session: Session) -> TimeProfile:
    """Persist one new profile version, then return it as stored."""
    repository = TimeProfileRepository(session)
    try:
        repository.add(profile)
    except ValueError as exc:
        # A version that already exists is immutable, so this is a conflict
        # rather than a malformed request.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    # Read back so the response is exactly what was persisted, not what was sent.
    return repository.get(profile.time_profile_id, profile.version)


@app.post("/time-profiles/validate", response_model=TimeProfileValidation)
def validate_time_profile(
    profile: TimeProfile, session: Session = Depends(get_session)
) -> TimeProfileValidation:
    """Check a candidate time profile without storing it.

    The setup UI asks this before publishing so the verdict comes from the
    backend rather than the browser. Nothing is written and no id is reserved.

    Out-of-range parameters never reach this function: `TimeAssumptions` rejects
    them as a 422 first. What is left is what needs the stored catalog — whether
    this version already exists — plus advisory notes about values that are
    legal but unusual enough to be worth a second look. An advisory never makes
    a profile invalid: the parameters are the user's to choose.
    """
    repository = TimeProfileRepository(session)
    already_exists = repository.exists(profile.time_profile_id, profile.version)

    try:
        repository.get(profile.time_profile_id)
        is_new_profile = False
    except ValueError:
        is_new_profile = True

    messages: list[str] = []
    if already_exists:
        messages.append(
            f"Version {profile.version} of '{profile.time_profile_id}' has already been "
            "published, and a published version can never be changed. Choose a higher "
            "version number."
        )
    messages.extend(advisory_notes(profile))

    return TimeProfileValidation(
        valid=not already_exists,
        time_profile_id=profile.time_profile_id,
        name=profile.name,
        version=profile.version,
        is_illustrative=profile.is_illustrative,
        version_already_exists=already_exists,
        is_new_profile=is_new_profile,
        messages=messages,
    )


@app.post("/time-profiles", response_model=TimeProfile, status_code=201)
def create_time_profile(
    profile: TimeProfile, session: Session = Depends(get_session)
) -> TimeProfile:
    """Publish a new time-assumption profile version.

    `is_illustrative` defaults to true — set it to false once the parameters are
    your shop's real feeds and speeds. An existing `(time_profile_id, version)`
    is immutable: re-posting one returns 409, so changing an assumption means
    publishing a new version and leaves every earlier estimate untouched.
    """
    return _create_time_profile(profile, session)


@app.post("/time-profiles/{time_profile_id}/versions", response_model=TimeProfile, status_code=201)
def create_time_profile_version(
    time_profile_id: str,
    version: TimeProfileVersionCreate,
    session: Session = Depends(get_session),
) -> TimeProfile:
    """Add a version to an existing profile family, taking the id from the path."""
    try:
        profile = TimeProfile(time_profile_id=time_profile_id, **version.model_dump())
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _create_time_profile(profile, session)


@app.get("/time-profiles/{time_profile_id}", response_model=TimeProfile)
def time_profile(
    time_profile_id: str, version: str | None = None, session: Session = Depends(get_session)
) -> TimeProfile:
    """Fetch one time profile. Omit `version` to get the latest of that id."""
    try:
        return TimeProfileRepository(session).get(time_profile_id, version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class PlanCostPreview(BaseModel):
    """What a plan would cost as it stands, with setup and run time kept apart.

    Exists so the browser never has to do costing arithmetic to show the effect
    of a regrouping. Every figure here comes from the same unmodified
    calculators an estimate uses, so the preview and the estimate cannot
    disagree; nothing is persisted and no estimate is created.
    """

    quantity: int
    currency: str = DEFAULT_CURRENCY
    time: PlanTimeSummary
    setup_cost: float = Field(default=0.0, description="Charged once per setup, not per operation.")
    run_cost: float = Field(default=0.0, description="Machine and labour over the cycle times.")
    total_operation_cost: float = 0.0
    costed_operation_count: int = 0
    rate_set_id: str | None = None
    rate_set_version: str | None = None
    unpriced_processes: list[str] = Field(default_factory=list)
    note: str = (
        "Operation cost only — material and additional costs are not included. Setup time is "
        "charged once per setup; cycle time is charged for every operation."
    )


@app.post("/operations/plan-cost-preview", response_model=PlanCostPreview)
def plan_cost_preview(
    plan: ManufacturingPlan,
    quantity: int = Query(1, ge=1),
    rate_set_id: str | None = Query(None),
    rate_set_version: str | None = Query(None),
    session: Session = Depends(get_session),
) -> PlanCostPreview:
    """Price a manufacturing plan's operations without creating an estimate.

    This is what lets the UI say "moving that operation into its own setup adds
    10 minutes and ₹200" *before* anyone commits to it. It reuses
    `resolve_operation` and `calculate_operation_cost` unchanged — the same path
    a real estimate takes — so a preview can never quietly differ from the
    estimate that follows it.

    Splitting the total into setup and run cost is the point: the two behave
    completely differently when a plan is regrouped, and a single number would
    hide exactly the effect the engineer is trying to see.
    """
    summary = plan.time_summary(quantity)
    requirements = plan.to_operation_requirements(quantity)

    resolved = None
    if rate_set_id:
        try:
            resolved = RateSetRepository(session).get(rate_set_id, rate_set_version)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    setup_cost = 0.0
    run_cost = 0.0
    unpriced: list[str] = []

    for requirement in requirements:
        if resolved is None:
            continue
        try:
            operation = resolve_operation(resolved, requirement)
        except ValueError:
            if requirement.process_type not in unpriced:
                unpriced.append(requirement.process_type)
            continue
        costed = calculate_operation_cost(operation)
        # `setup_cost` is the machine rate over the setup minutes, charged once;
        # everything else scales with the cycle time and the quantity.
        setup_cost += float(costed.setup_cost)
        run_cost += float(costed.machine_cost) + float(costed.labor_cost)

    return PlanCostPreview(
        quantity=quantity,
        currency=resolved.currency if resolved is not None else DEFAULT_CURRENCY,
        time=summary,
        setup_cost=round_money(setup_cost),
        run_cost=round_money(run_cost),
        total_operation_cost=round_money(setup_cost + run_cost),
        costed_operation_count=len(requirements),
        rate_set_id=rate_set_id,
        rate_set_version=resolved.version if resolved is not None else None,
        unpriced_processes=unpriced,
    )


@app.post("/operations/estimate-time", response_model=OperationTimeResponse)
def operations_estimate_time(
    request: OperationTimeRequest,
    time_profile_id: str | None = Query(
        None, description="Published profile to estimate under. Omit for the built-in baseline."
    ),
    time_profile_version: str | None = Query(
        None, description="Exact profile version to pin. Omit to use the latest."
    ),
    session: Session = Depends(get_session),
) -> OperationTimeResponse:
    """Estimate setup and cycle times for a batch of normalized operations.

    **Deliberately takes no CAD file.** Recognition has already run by the time
    anyone wants a time, so this accepts the normalized operations that
    `/cad/operations/suggest` returns. Re-uploading the model would mean parsing
    a 200 MB export again for an answer that costs microseconds — hence the path
    outside `/cad`, which is for endpoints that actually read geometry.

    Deterministic and parameter-based. No ML, no learning, no randomness: the
    same operations under the same assumptions always give the same times.

    Every estimate reports the formula that produced it, the geometry it
    consumed and every parameter value it used. Where a formula's inputs are
    missing — or the units were never established — the result is
    `needs_user_input` rather than a substituted default.

    **Nothing here is costed, and nothing is committed.** Generating an estimate
    does not adopt it: a time reaches a cost only once someone accepts or enters
    it in the manufacturing plan.

    The parameters come from the published time profile named in the query, and
    each estimate records which profile and version produced it. Explicit
    `assumptions` in the body still win — that lets a caller try parameters they
    have not published — and the result then records no profile, which is the
    honest account of where the number came from.
    """
    profile = None
    if time_profile_id:
        try:
            profile = TimeProfileRepository(session).get(time_profile_id, time_profile_version)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return estimate_operations(request, profile=profile)


@app.post("/cad/mass", response_model=GeometryMassResult)
async def cad_mass(
    file: UploadFile = File(...),
    material: str = Form(..., description="Material name, e.g. 'Steel' or 'Aluminium'."),
    source_unit: str = Form(
        ...,
        description=(
            "Length unit the geometry is in: mm, cm, m, or inch. STEP files are "
            "normalized to mm by the reader, so STEP uploads must pass 'mm'."
        ),
    ),
    geometry_completion_method: str | None = Form(
        None,
        description=(
            f"Only for a file whose geometry cannot be measured: '{TOTAL_VOLUME}' or "
            f"'{MISSING_VOLUME}'."
        ),
    ),
    geometry_completion_volume_mm3: float | None = Form(
        None, description="The volume the user is supplying, in mm³."
    ),
) -> GeometryMassResult:
    """Extract CAD geometry and convert its volume into a mass.

    Not a cost calculation.
    """
    data = await file.read()
    geometry = _geometry_of(_import(data, file.filename or "", CAPABILITY_GEOMETRY))

    geometry = _resolve_geometry(
        geometry, geometry_completion_method, geometry_completion_volume_mm3
    )
    try:
        return calculate_mass_from_geometry(geometry, material, source_unit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _entered_yield_percent(stage_id: str, entry) -> float | None:
    """The yield a person entered for one stage in `stage_inputs`, or None when absent.

    Absent (or null) is "no yield", and nothing stands in for it. Anything else
    must be a number above 0 and at most 100, or the request is refused.
    """
    if not isinstance(entry, dict) or entry.get("yield_percent") is None:
        return None
    value = entry["yield_percent"]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= 100
    ):
        raise ValueError(
            f"'yield_percent' for stage '{stage_id}' must be a number above 0 and at most 100 — "
            f"the blank's mass as a percentage of the metal poured or billet bought; got {value!r}. "
            "Leave it out for no yield."
        )
    return float(value)


# Route stages whose work a weld's system time can carry as a named component,
# in route order: stage -> (the component's key in `system_time_parameters`,
# the error when it is charged twice, and the words the refusals use: the time
# a weld carries, the work, the stage as a choice, the profile figure).
_WELD_COMPONENT_STAGES = {
    "fit_up": (
        "welding_tack_minutes_per_weld",
        "fit_up_charged_twice",
        ("a tack time", "tacking", "fit-up", "tack time"),
    ),
    "weld_finishing": (
        "weld_finishing_minutes",
        "weld_finishing_charged_twice",
        ("a finishing time", "grinding", "weld finishing", "finishing rate"),
    ),
}


def _refuse_weld_component_charged_twice(
    plan: "ManufacturingPlan | None", classification, timed: dict[str, str]
) -> None:
    """Tacking and weld grinding are each charged once: by the route stage's
    rate, or inside the welds' time.

    A costed weld whose committed time is the system's and carries a stage's
    component, on a route that prices that stage by its stage rate, would charge
    the same work twice — refused. The stage priced by time with a system-timed
    weld that carries no such component would charge that weld's share nowhere —
    refused too. A time the engineer typed is their whole figure for the weld
    and is taken as they entered it.
    """
    route = classification.route if classification is not None else None
    route_ids = [stage.stage for stage in route.stages] if route is not None else []
    stages = [stage_id for stage_id in _WELD_COMPONENT_STAGES if stage_id in route_ids]
    if not stages:
        return
    if plan is None:
        unpriced = [stage_id for stage_id in stages if stage_id in timed]
        if unpriced:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "timed_stage_not_costed",
                    "message": " ".join(
                        f"No estimate was produced: {STAGES[stage_id][0]} is priced by weld "
                        "time, which needs a manufacturing plan whose welds carry "
                        f"{_WELD_COMPONENT_STAGES[stage_id][2][0]}."
                        for stage_id in unpriced
                    ),
                    "stages": unpriced,
                },
            )
        return
    welds = [
        operation
        for operation in plan.operations
        if plan.counts_toward_cost(operation)
        and operation.operation_type == "welding"
        and operation.time_source is TimeSource.SYSTEM
    ]
    for stage_id in stages:
        key, twice_error, (time, work, name, figure) = _WELD_COMPONENT_STAGES[stage_id]
        label = STAGES[stage_id][0]
        carrying = [o.operation_id for o in welds if key in o.system_time_parameters]
        lacking = [o.operation_id for o in welds if key not in o.system_time_parameters]
        if stage_id not in timed and carrying:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": twice_error,
                    "message": (
                        f"No estimate was produced: {len(carrying)} costed weld(s) carry {time} "
                        f"from their time profile, and {label} is also priced by its stage rate, "
                        f"so the same {work} would be charged twice. Choose one: price {name} by "
                        f"weld time, or time the welds with a profile that has no {figure}."
                    ),
                    "stage": stage_id,
                    "operations": carrying,
                },
            )
        if stage_id in timed and lacking:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "timed_stage_not_costed",
                    "message": (
                        f"No estimate was produced: {label} is priced by weld time, but "
                        f"{len(lacking)} costed weld(s) were timed by a profile with no {figure}, "
                        f"so their {work} would be charged nowhere. Time them with a profile "
                        f"that states one, enter their times, or price {name} from its stage "
                        "rate."
                    ),
                    "stages": [stage_id],
                    "operations": lacking,
                },
            )


def _entered_amortisation_quantity(stage_id: str, entry) -> int | None:
    """The tool life a person entered for one stage in `stage_inputs`, or None when absent.

    Absent (or null) means the stage's one-off charge falls whole on this batch,
    as it always has; nothing stands in for it. Anything else must be a whole
    number of pieces, at least 1, or the request is refused.
    """
    if not isinstance(entry, dict) or entry.get("amortisation_quantity") is None:
        return None
    value = entry["amortisation_quantity"]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value != int(value)
        or value < 1
    ):
        raise ValueError(
            f"'amortisation_quantity' for stage '{stage_id}' must be a whole number of pieces, at "
            f"least 1 — how many pieces the pattern or die is expected to make; got {value!r}. "
            "Leave it out to charge the whole one-off charge to this batch."
        )
    return int(value)


@dataclass
class PricedCadPart:
    """Everything `/cost/cad-part` works out before it stores an estimate.

    `price_cad_part` is the one pricing path for a CAD part. The endpoint stores
    what it returns; a preview (comparing routes, a bulk run) shows it and
    stores nothing — so a preview can never price a part differently from the
    estimate the same inputs would produce.
    """

    geometry: GeometryProperties
    operations_data: list
    requirement_models: list
    classification: "ProcessClassification | None"
    route_differences: list
    plan: "ManufacturingPlan | None"
    time_profile_snapshot: dict | None
    stock: "BarStock | None"
    stage_input_values: dict
    additional_costs: "AdditionalCostInputs | None"
    resolved_rate_set: "RateSet | None"
    result: CadPartCostEstimateResult


def price_cad_part(
    session: Session,
    *,
    data: bytes,
    filename: str,
    material: str,
    source_unit: str,
    part_id: str,
    part_name: str,
    quantity: int = 1,
    operations: str = "[]",
    operation_requirements: str = "[]",
    rate_set_id: str | None = None,
    rate_set_version: str | None = None,
    overhead_rate_percent: float | None = None,
    scrap_rate_percent: float | None = None,
    tooling_cost_per_batch: float | None = None,
    geometry_completion_method: str | None = None,
    geometry_completion_volume_mm3: float | None = None,
    manufacturing_plan: str | None = None,
    raw_stock: str | None = None,
    process_classification: str | None = None,
    stage_inputs: str | None = None,
) -> PricedCadPart:
    """Price one CAD part exactly as `/cost/cad-part` does, without storing it.

    Takes the endpoint's own form values. Raises the same `HTTPException`s the
    endpoint answers with (400, 415, 422), so a caller reports a refusal in the
    endpoint's words.
    """
    geometry = _geometry_of(_import(data, filename, CAPABILITY_GEOMETRY))

    # A model whose geometry cannot be measured stops here with a structured
    # request for the missing figure, rather than an opaque failure.
    geometry = _resolve_geometry(
        geometry, geometry_completion_method, geometry_completion_volume_mm3
    )

    try:
        try:
            operations_data = json.loads(operations)
        except json.JSONDecodeError as exc:
            raise ValueError(f"'operations' must be valid JSON: {exc}") from exc
        operation_models = _OPERATIONS_LIST_ADAPTER.validate_python(operations_data)

        try:
            requirements_data = json.loads(operation_requirements)
        except json.JSONDecodeError as exc:
            raise ValueError(f"'operation_requirements' must be valid JSON: {exc}") from exc
        requirement_models = _REQUIREMENTS_LIST_ADAPTER.validate_python(requirements_data)

        # The route is checked before anything is priced. Every costing path
        # here is machining arithmetic, so a route with a stage it cannot price
        # is refused whole: pricing only its machined stages would present part
        # of the cost as all of it.
        classification: ProcessClassification | None = None
        route_differences: list[str] = []
        if process_classification is not None:
            try:
                classification = ProcessClassification.model_validate_json(process_classification)
            except ValidationError as exc:
                raise ValueError(f"'process_classification' is not valid: {exc}") from exc
            if classification.blocks_costing:
                route = classification.route
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "process_route_not_costable",
                        "message": classification.costing_note,
                        "costing_status": classification.costing_status,
                        "decision_status": classification.decision_status,
                        "family": classification.family.value,
                        "route": route.label if route is not None else None,
                        "stage_rate_stages": classification.stage_rate_stages,
                    },
                )

        # What a person entered per route stage: a mass, and for a timeable stage
        # (welding) whether it is priced by time instead of by its stage rate.
        stage_input_values: dict = {}
        if stage_inputs is not None:
            try:
                stage_input_values = json.loads(stage_inputs)
            except json.JSONDecodeError as exc:
                raise ValueError(f"'stage_inputs' must be valid JSON: {exc}") from exc
            if not isinstance(stage_input_values, dict):
                raise ValueError("'stage_inputs' must be an object keyed by route stage id.")
        # A yield is the engineer's number or it is absent. Checked for every
        # stage sent, priced by this route or not, because it is stored as sent.
        stage_yields = {
            stage_id: _entered_yield_percent(stage_id, entry)
            for stage_id, entry in stage_input_values.items()
        }
        # So is a tool life, for the same reason.
        stage_amortisation = {
            stage_id: _entered_amortisation_quantity(stage_id, entry)
            for stage_id, entry in stage_input_values.items()
        }
        timed: dict[str, str] = {}
        if classification is not None and classification.route is not None:
            timed = timed_stages(
                stage_input_values, [stage.stage for stage in classification.route.stages]
            )

        # A manufacturing plan is the richer way to say the same thing, so the
        # two are mutually exclusive: accepting both would leave it ambiguous
        # which one priced the part.
        plan: ManufacturingPlan | None = None
        time_profile_snapshot: dict | None = None
        if manufacturing_plan is not None:
            if requirement_models:
                raise ValueError(
                    "Send either 'manufacturing_plan' or 'operation_requirements', not both — "
                    "otherwise which one produced the cost is ambiguous."
                )
            try:
                plan_data = json.loads(manufacturing_plan)
            except json.JSONDecodeError as exc:
                raise ValueError(f"'manufacturing_plan' must be valid JSON: {exc}") from exc
            plan = ManufacturingPlan.model_validate(plan_data)
            # The route the plan is costed under comes from the classification,
            # recomputed here, never from what the browser says it applied —
            # so the operations priced and the route recorded cannot disagree.
            if classification is not None:
                route_processes = classification.plan_processes
                if route_processes is not None:
                    # A stage priced by time is costed through its operations,
                    # so its process belongs to the route like a machining stage.
                    route_processes = route_processes + [
                        process for process in timed.values() if process not in route_processes
                    ]
                plan = plan.model_copy(
                    update={
                        "route_processes": route_processes,
                        "as_formed_feature_types": classification.as_formed_feature_types,
                    }
                )
            # Snapshot whichever profiles the plan's predictions were made under,
            # taken from the plan itself rather than from a separate field that
            # could disagree with it. Captured whole, so publishing a later
            # version can never change what this estimate says it assumed.
            time_profile_snapshot = _snapshot_time_profiles(plan, session)
            # Only operations carrying a time the engineer committed to, and in
            # the chosen route. The rest are left out rather than defaulted, and
            # are recorded below so the omission is visible in the stored estimate.
            requirement_models = plan.to_operation_requirements(quantity)

        if classification is not None:
            route_differences = plan_route_differences(
                classification,
                [requirement.process_type for requirement in requirement_models],
                timed_processes=timed.values(),
            )

        # A stage priced by time has no stage-rate line, so with no costed
        # operation of its process it would cost nothing at all. Refused instead.
        costed_processes = {requirement.process_type for requirement in requirement_models}
        untimed = [
            stage_id for stage_id, process in timed.items() if process not in costed_processes
        ]
        if untimed:
            labels = ", ".join(STAGES[stage_id][0] for stage_id in untimed)
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "timed_stage_not_costed",
                    "message": (
                        f"No estimate was produced: {labels} is priced by time, and the plan has "
                        "no operation of it with a committed time. Add the welds and accept or "
                        "enter their times, or price it from its stage rate."
                    ),
                    "stages": untimed,
                },
            )
        _refuse_weld_component_charged_twice(plan, classification, timed)

        # A bar counts only once a person confirmed it; an unconfirmed proposal
        # is recorded but material is costed from the finished part as before.
        stock: BarStock | None = None
        if raw_stock is not None:
            try:
                stock = BarStock.model_validate_json(raw_stock)
            except ValidationError as exc:
                raise ValueError(f"'raw_stock' is not valid: {exc}") from exc
            if stock.confirmed and not stock.fits_part:
                raise ValueError(
                    f"The confirmed bar (Ø{stock.diameter_mm:g} × {stock.length_mm:g} mm) is "
                    "smaller than the part, so material cannot be costed from it."
                )

        # Supplying any additional-cost field replaces the rate set's additional
        # costs wholesale; supplying none leaves them to the rate set.
        explicit_additional = [overhead_rate_percent, scrap_rate_percent, tooling_cost_per_batch]
        additional_costs = None
        if any(value is not None for value in explicit_additional):
            additional_costs = AdditionalCostInputs(
                overhead_rate_percent=overhead_rate_percent or 0.0,
                scrap_rate_percent=scrap_rate_percent or 0.0,
                tooling_cost_per_batch=tooling_cost_per_batch or 0.0,
            )

        resolved_rate_set = (
            RateSetRepository(session).get(rate_set_id, rate_set_version)
            if rate_set_id is not None
            else None
        )

        # A decided route's non-machining stages are priced from the rate set's
        # stage rates — every one of them, or the estimate is refused with each
        # stage's reason. Which stages those are comes from the route catalog by
        # stage id, not from what the payload says about itself.
        # A stage priced by time is left out of stage pricing, so a yield on it
        # would be dropped without a word. It is refused instead: a yield divides
        # the mass a per-kg rate is charged on, and this stage has no such charge.
        for stage_id in timed:
            if stage_yields.get(stage_id) is not None:
                raise ValueError(
                    f"A yield of {stage_yields[stage_id]:g}% was entered for "
                    f"{STAGES[stage_id][0]}, but that stage is priced by time, so there is no "
                    "per-kg mass for a yield to apply to. Remove the yield."
                )
            # Its stage rate is not charged at all, so neither is its one-off
            # charge: there is no tool charge here to spread over a quantity.
            if stage_amortisation.get(stage_id) is not None:
                raise ValueError(
                    f"An amortisation quantity of {stage_amortisation[stage_id]} pieces was "
                    f"entered for {STAGES[stage_id][0]}, but that stage is priced by time, so its "
                    "stage rate's one-off charge is not charged and there is nothing to amortise. "
                    "Remove the amortisation quantity."
                )
        stage_lines = []
        material_supplied_by: str | None = None
        if classification is not None and classification.costing_status == "stage_rates_required":
            route_ids = [stage.stage for stage in classification.route.stages]
            to_price = [
                StageInput(
                    stage=stage.stage,
                    label=STAGES[stage.stage][0] if stage.stage in STAGES else stage.label,
                    mass_kg=(stage_input_values.get(stage.stage) or {}).get("mass_kg"),
                    yield_percent=stage_yields.get(stage.stage),
                    amortisation_quantity=stage_amortisation.get(stage.stage),
                )
                for stage in classification.route.stages
                if (stage.stage not in STAGES or STAGES[stage.stage][1] is None)
                and stage.stage not in timed
            ]
            finished = calculate_mass_from_geometry(
                geometry,
                material,
                source_unit,
                material=(
                    resolve_material(resolved_rate_set, material)
                    if resolved_rate_set is not None
                    else None
                ),
            )
            pricing = price_stages(
                to_price,
                resolved_rate_set,
                quantity,
                material_stage=material_supplying_stage(route_ids),
                finished_mass_kg=finished.mass_kg,
                route_machines=any(
                    stage_id in STAGES and STAGES[stage_id][1] is not None
                    for stage_id in route_ids
                ),
            )
            if not pricing.priceable:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "route_stages_not_priceable",
                        "message": (
                            "No estimate was produced: "
                            + " ".join(problem.detail for problem in pricing.problems)
                        ),
                        "route": classification.route.label,
                        "stages": [problem.model_dump() for problem in pricing.problems],
                    },
                )
            stage_lines = pricing.lines
            material_supplied_by = pricing.material_supplied_by

        result = calculate_cad_part_cost(
            geometry=geometry,
            material_name=material,
            source_unit=source_unit,
            part_id=part_id,
            part_name=part_name,
            quantity=quantity,
            operations=operation_models,
            operation_requirements=requirement_models,
            additional_costs=additional_costs,
            rate_set_id=rate_set_id,
            rate_set_version=rate_set_version,
            rate_set=resolved_rate_set,
            material_stock_volume_mm3=(
                stock.volume_mm3 if stock is not None and stock.confirmed else None
            ),
            stage_costs=stage_lines,
            material_supplied_by=material_supplied_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return PricedCadPart(
        geometry=geometry,
        operations_data=operations_data,
        requirement_models=requirement_models,
        classification=classification,
        route_differences=route_differences,
        plan=plan,
        time_profile_snapshot=time_profile_snapshot,
        stock=stock,
        stage_input_values=stage_input_values,
        additional_costs=additional_costs,
        resolved_rate_set=resolved_rate_set,
        result=result,
    )


@app.post("/cost/cad-part", response_model=CadPartCostEstimateResult)
async def cost_cad_part(
    file: UploadFile = File(...),
    material: str = Form(..., description="Material name, e.g. 'Steel' or 'Aluminium'."),
    source_unit: str = Form(
        ...,
        description=(
            "Length unit the geometry is in: mm, cm, m, or inch. STEP files are "
            "normalized to mm by the reader, so STEP uploads must pass 'mm'."
        ),
    ),
    part_id: str = Form(...),
    part_name: str = Form(...),
    quantity: int = Form(1),
    operations: str = Form("[]", description="JSON-encoded list of ManufacturingOperation objects."),
    operation_requirements: str = Form(
        "[]",
        description=(
            "JSON-encoded list of OperationRequirement objects, whose rates are resolved "
            "from the selected rate set. Requires rate_set_id."
        ),
    ),
    rate_set_id: str | None = Form(None, description="Rate set to resolve rates from."),
    rate_set_version: str | None = Form(
        None, description="Exact rate set version to pin. Omit to use the latest."
    ),
    overhead_rate_percent: float | None = Form(
        None, description="Overhead as a percent of direct cost. Overrides the rate set."
    ),
    scrap_rate_percent: float | None = Form(
        None, description="Scrap allowance as a percent of direct cost. Overrides the rate set."
    ),
    tooling_cost_per_batch: float | None = Form(
        None, description="One-time tooling cost for the batch. Overrides the rate set."
    ),
    geometry_completion_method: str | None = Form(
        None,
        description=(
            f"Only for a file whose geometry cannot be measured: '{TOTAL_VOLUME}' supplies the "
            f"whole part's volume, '{MISSING_VOLUME}' adds to the watertight part that was "
            "measured. Recorded on the estimate as a stated assumption."
        ),
    ),
    geometry_completion_volume_mm3: float | None = Form(
        None, description="The volume the user is supplying, in mm³."
    ),
    manufacturing_plan: str | None = Form(
        None,
        description=(
            "JSON-encoded ManufacturingPlan. When given, the operations to cost are derived "
            "from it — only those with a committed time — and the whole plan plus its operation "
            "feedback is persisted on the estimate. Mutually exclusive with "
            "'operation_requirements'."
        ),
    ),
    raw_stock: str | None = Form(
        None,
        description=(
            "Optional JSON-encoded BarStock from /cad/operations/suggest, carrying any decision "
            "from /cad/stock/decide. When confirmed, material is costed from the bar's volume "
            "instead of the finished part's; unconfirmed, it is recorded and changes nothing."
        ),
    ),
    process_classification: str | None = Form(
        None,
        description=(
            "Optional JSON-encoded ProcessClassification from /cad/operations/suggest, carrying "
            "any decision from /cad/process/decide. Recorded on the estimate. A decided route's "
            "non-machining stages are priced from the rate set's stage rates; a route marked "
            "unknown, or an unreviewed recommendation needing stage rates, is refused with 422."
        ),
    ),
    stage_inputs: str | None = Form(
        None,
        description=(
            "Optional JSON object keyed by route stage id, e.g. {\"casting\": {\"mass_kg\": "
            "2.4}}: the mass, per part, that a per-kg stage rate is charged on, recorded on the "
            "estimate. A per-kg stage with none entered is charged on the finished part's CAD "
            "mass, labelled as such. The per-kg stage that supplies the material may also carry "
            "\"yield_percent\" (above 0, at most 100): the blank's mass as a percentage of the "
            "metal poured or billet bought, so the rate is charged on mass / (yield / 100). "
            "There is no default yield; absent, nothing is added. A stage whose rate has a "
            "one-off batch charge (pattern, die) may carry \"amortisation_quantity\" (a whole "
            "number of pieces, at least this estimate's quantity): how many pieces the tool is "
            "expected to make, so only batch_charge x quantity / amortisation_quantity is "
            "charged. There is no default tool life; absent, the whole charge falls on this batch."
        ),
    ),
    session: Session = Depends(get_session),
) -> CadPartCostEstimateResult:
    """CAD-driven part cost estimate: geometry -> mass -> existing part costing.

    This is the CAD-driven counterpart to `/cost/part`, which still accepts a
    manually supplied `mass_kg`. `/cost/part` is unchanged.
    """
    priced = price_cad_part(
        session,
        data=await file.read(),
        filename=file.filename or "",
        material=material,
        source_unit=source_unit,
        part_id=part_id,
        part_name=part_name,
        quantity=quantity,
        operations=operations,
        operation_requirements=operation_requirements,
        rate_set_id=rate_set_id,
        rate_set_version=rate_set_version,
        overhead_rate_percent=overhead_rate_percent,
        scrap_rate_percent=scrap_rate_percent,
        tooling_cost_per_batch=tooling_cost_per_batch,
        geometry_completion_method=geometry_completion_method,
        geometry_completion_volume_mm3=geometry_completion_volume_mm3,
        manufacturing_plan=manufacturing_plan,
        raw_stock=raw_stock,
        process_classification=process_classification,
        stage_inputs=stage_inputs,
    )
    geometry = priced.geometry
    operations_data = priced.operations_data
    requirement_models = priced.requirement_models
    classification = priced.classification
    route_differences = priced.route_differences
    plan = priced.plan
    time_profile_snapshot = priced.time_profile_snapshot
    stock = priced.stock
    stage_input_values = priced.stage_input_values
    additional_costs = priced.additional_costs
    resolved_rate_set = priced.resolved_rate_set
    result = priced.result

    part_cost = result.part_cost
    estimate_id = EstimateRepository(session).save(
        source="cost/cad-part",
        part_id=part_id,
        part_name=part_name,
        material=material,
        quantity=quantity,
        rate_set=resolved_rate_set,
        currency=part_cost.currency,
        total_material_cost=part_cost.total_material_cost,
        total_operation_cost=part_cost.total_operation_cost,
        total_additional_cost=part_cost.total_additional_cost,
        total_part_cost=part_cost.total_part_cost,
        request_snapshot={
            "source_file": file.filename,
            "material": material,
            "source_unit": source_unit,
            "part_id": part_id,
            "part_name": part_name,
            "quantity": quantity,
            "operations": operations_data,
            "operation_requirements": [
                requirement.model_dump(mode="json") for requirement in requirement_models
            ],
            "rate_set_id": rate_set_id,
            "rate_set_version": rate_set_version,
            "additional_costs": (
                additional_costs.model_dump(mode="json") if additional_costs is not None else None
            ),
            "geometry": geometry.model_dump(mode="json"),
            # Recorded explicitly as well as inside the geometry, so a reader of
            # this estimate years later cannot miss that a volume was assumed.
            "geometry_completion": (
                {
                    "method": geometry_completion_method,
                    "volume_mm3": geometry_completion_volume_mm3,
                    "resulting_volume_mm3": geometry.volume,
                }
                if geometry_completion_method is not None
                else None
            ),
            "mass": result.mass.model_dump(mode="json"),
            # The recommendation and the engineer's answer side by side, with
            # the costing status they implied when this estimate was made. Only
            # present when one was sent, so a snapshot without it is unchanged.
            **(
                {
                    "process_classification": classification.model_dump(mode="json"),
                    "process_route_differences": route_differences,
                }
                if classification is not None
                else {}
            ),
            # The proposed bar and the engineer's answer, only when one was sent.
            **({"raw_stock": stock.model_dump(mode="json")} if stock is not None else {}),
            # The masses a person entered for per-kg stages, as sent.
            **({"stage_inputs": stage_input_values} if stage_inputs is not None else {}),
        },
        result_snapshot=result.model_dump(mode="json"),
        # The manufacturing plan is recorded whole, alongside one feedback row
        # per operation. Together they make a historical estimate reconstructible:
        # the CAD feature, what the system suggested, what it estimated the time
        # to be, what the engineer changed, the final values, and the sequence.
        # A later edit produces a new estimate with its own snapshot rather than
        # rewriting this one, so what was believed at the time survives.
        plan_snapshot=(
            None
            if plan is None
            else {
                "plan": plan.model_dump(mode="json"),
                "feedback": [
                    entry.model_dump(mode="json") for entry in feedback_from_plan(plan)
                ],
                "uncosted_operation_ids": [
                    operation.operation_id for operation in plan.uncosted_operations()
                ],
                # Present only when a route was decided, so a snapshot made
                # without one keeps its old shape.
                **(
                    {
                        "out_of_route_operation_ids": [
                            operation.operation_id
                            for operation in plan.out_of_route_operations()
                        ]
                    }
                    if plan.route_processes is not None
                    else {}
                ),
                **(
                    {
                        "as_formed_operation_ids": [
                            operation.operation_id for operation in plan.as_formed_operations()
                        ]
                    }
                    if plan.as_formed_feature_types is not None
                    else {}
                ),
                "note": (
                    "Operations without a committed cycle time were left out of the cost "
                    "rather than defaulted. The system's own suggestions and time estimates "
                    "are recorded here unchanged, beside whatever the engineer chose."
                ),
            }
        ),
        time_profile_snapshot=time_profile_snapshot,
    )
    session.commit()

    result.estimate_id = estimate_id
    result.part_cost.estimate_id = estimate_id
    return result


# ---------------------------------------------------------- route comparison
#
# The recommended route and the routes of the other families the evidence is
# consistent with, each priced as if the engineer had chosen it. Every route goes
# through `price_cad_part`, the same path `/cost/cad-part` uses, so a previewed
# figure is the figure that route would be estimated at. Nothing is stored and
# nothing is chosen: a person picks one through `/cad/process/decide`.

from cad_costing_agent.route_comparison import (  # noqa: E402
    STATUS_PRICED,
    STATUS_REFUSED,
    RouteComparison,
    RouteComparisonRow,
    RouteRefusal,
    RouteStageSummary,
    StageMassView,
    refusal_from_detail,
    select_route_ids,
)


def _route_decided_as(
    classification: ProcessClassification, route_id: str, recommended: bool
) -> ProcessClassification:
    """The classification as if the engineer had just chosen this route.

    Any earlier answer is withdrawn first, so every route is previewed from the
    same untouched recommendation.
    """
    fresh = record_route_decision(classification, UserAction.PENDING)
    if recommended:
        return record_route_decision(fresh, UserAction.ACCEPTED)
    return record_route_decision(fresh, UserAction.EDITED, route_id=route_id)


@app.post("/cost/route-comparison", response_model=RouteComparison)
async def compare_routes(
    file: UploadFile = File(...),
    process_classification: str = Form(
        ...,
        description=(
            "JSON-encoded ProcessClassification from /cad/operations/suggest. Its recommended "
            "route and candidate families decide which routes are compared; any decision on it "
            "is ignored for the preview and only reported as `currently_decided`."
        ),
    ),
    material: str = Form(...),
    source_unit: str = Form(...),
    part_id: str = Form(...),
    part_name: str = Form(...),
    quantity: int = Form(1),
    operations: str = Form("[]"),
    operation_requirements: str = Form("[]"),
    rate_set_id: str | None = Form(None),
    rate_set_version: str | None = Form(None),
    overhead_rate_percent: float | None = Form(None),
    scrap_rate_percent: float | None = Form(None),
    tooling_cost_per_batch: float | None = Form(None),
    geometry_completion_method: str | None = Form(None),
    geometry_completion_volume_mm3: float | None = Form(None),
    manufacturing_plan: str | None = Form(None),
    raw_stock: str | None = Form(None),
    stage_inputs: str | None = Form(None),
    session: Session = Depends(get_session),
) -> RouteComparison:
    """Price each candidate route as a preview. Stores nothing and chooses nothing.

    Takes `/cost/cad-part`'s own inputs. A route that cannot be priced (a stage
    with no rate in the rate set, a per-kg stage with no entered mass, a route
    marked unknown) is listed with the reason rather than dropped or shown as zero.
    """
    try:
        classification = ProcessClassification.model_validate_json(process_classification)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400, detail=f"'process_classification' is not valid: {exc}"
        ) from exc

    data = await file.read()
    system = classification.system_route
    selected = select_route_ids(
        system.route_id if system is not None else None,
        [candidate.family.value for candidate in classification.candidates],
        [(route.route_id, route.family.value) for route in route_catalog()],
    )
    current = (
        classification.route.route_id
        if classification.user_action is not UserAction.PENDING and classification.route is not None
        else None
    )

    rows: list[RouteComparisonRow] = []
    rate_set_label: str | None = None
    for route_id, role in selected:
        decided = _route_decided_as(classification, route_id, role == "recommended")
        route = decided.route
        identity = dict(
            route_id=route_id,
            route_label=route.label if route is not None else route_id,
            family=route.family.value if route is not None else classification.family.value,
            role=role,
            currently_decided=current == route_id,
            stages=[
                RouteStageSummary(
                    stage=stage.stage,
                    label=stage.label,
                    pricing="machining" if stage.cost_model_available else "stage_rate",
                )
                for stage in (route.stages if route is not None else [])
            ],
        )
        try:
            priced = price_cad_part(
                session,
                data=data,
                filename=file.filename or "",
                material=material,
                source_unit=source_unit,
                part_id=part_id,
                part_name=part_name,
                quantity=quantity,
                operations=operations,
                operation_requirements=operation_requirements,
                rate_set_id=rate_set_id,
                rate_set_version=rate_set_version,
                overhead_rate_percent=overhead_rate_percent,
                scrap_rate_percent=scrap_rate_percent,
                tooling_cost_per_batch=tooling_cost_per_batch,
                geometry_completion_method=geometry_completion_method,
                geometry_completion_volume_mm3=geometry_completion_volume_mm3,
                manufacturing_plan=manufacturing_plan,
                raw_stock=raw_stock,
                process_classification=decided.model_dump_json(),
                stage_inputs=stage_inputs,
            )
        except HTTPException as exc:
            code, message, stages = refusal_from_detail(exc.status_code, exc.detail)
            rows.append(
                RouteComparisonRow(
                    **identity,
                    status=STATUS_REFUSED,
                    refusal=RouteRefusal(code=code, message=message, stages=stages),
                )
            )
            continue

        cost = priced.result.part_cost
        if cost.rate_set is not None and rate_set_label is None:
            rate_set_label = f"{cost.rate_set.rate_set_id} v{cost.rate_set.version}"
        plan = priced.plan
        rows.append(
            RouteComparisonRow(
                **identity,
                status=STATUS_PRICED,
                currency=cost.currency,
                total_part_cost=cost.total_part_cost,
                per_unit_cost=cost.per_unit_cost,
                total_material_cost=cost.total_material_cost,
                total_operation_cost=cost.total_operation_cost,
                total_stage_cost=cost.total_stage_cost,
                total_additional_cost=cost.total_additional_cost,
                material_charged=cost.material_charged,
                operations_costed=(
                    None if plan is None else sum(1 for o in plan.active if plan.counts_toward_cost(o))
                ),
                operations_not_timed=None if plan is None else len(plan.uncosted_operations()),
                operations_out_of_route=(
                    None if plan is None else len(plan.out_of_route_operations())
                ),
                operations_as_formed=None if plan is None else len(plan.as_formed_operations()),
                stage_masses=[
                    StageMassView(
                        stage=line.stage,
                        label=line.label,
                        mass_kg=line.mass_kg,
                        mass_source=line.mass_source,
                        mass_note=line.mass_note,
                        yield_percent=line.yield_percent,
                        base_mass_kg=line.base_mass_kg,
                    )
                    for line in cost.stage_costs
                    if line.mass_kg is not None
                ],
            )
        )

    return RouteComparison(
        rate_set=rate_set_label,
        quantity=quantity,
        routes=rows,
        not_compared=(
            None
            if rows
            else "The evidence recommends no route and lists no other family, so there is "
            "nothing to compare. Choose a route yourself."
        ),
    )


# ---------------------------------------------------------------- bulk preview
#
# Price many parts at once from a CSV and the CAD files it names, and store
# NOTHING. Every row runs the same pipeline a person would (import, recognize,
# suggest, plan, estimate times) and is then priced by `price_cad_part`, the one
# path `/cost/cad-part` uses, so a preview figure cannot disagree with the
# estimate the same inputs would produce. The times in a preview are the
# system's unreviewed predictions, marked accepted on an in-memory copy of the
# plan only so the part can be priced; that copy is never saved.

from fastapi import Response  # noqa: E402

from cad_costing_agent import bulk_preview  # noqa: E402
from cad_costing_agent.limits import (  # noqa: E402
    MAX_BULK_CSV_BYTES,
    MAX_BULK_FILES,
    MAX_BULK_ROWS,
    MAX_BULK_TOTAL_BYTES,
)
from cad_costing_agent.manufacturing_plan import build_plan  # noqa: E402


class BulkPreviewResponse(BaseModel):
    preview_label: str = bulk_preview.PREVIEW_LABEL
    notice: str = bulk_preview.PREVIEW_NOTICE
    stored: bool = Field(default=False, description="Always false: a preview stores nothing.")
    time_profile: str | None = None
    rows: list[bulk_preview.BulkRowResult]
    summary: bulk_preview.BulkSummary
    csv: str = Field(description="The same rows as CSV, produced by the server.")


def _bulk_reason(exc: Exception) -> tuple[str | None, str, bool]:
    """(code, sentence, refused) for a failure on one row."""
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("error") or str(detail)
            return detail.get("error"), str(message), exc.status_code == 422
        if isinstance(detail, list):
            return None, "; ".join(str(item.get("msg", item)) for item in detail), False
        return None, str(detail), False
    if isinstance(exc, ValueError):
        return None, str(exc), False
    return "unexpected", f"Unexpected failure ({type(exc).__name__}): {exc}", False


def _accept_for_preview(plan: ManufacturingPlan) -> ManufacturingPlan:
    """An in-memory copy with every system time marked accepted. Never persisted."""
    return plan.model_copy(
        update={
            "operations": [
                o.model_copy(update={"system_time_accepted": True})
                if o.system_suggested_cycle_time is not None
                else o
                for o in plan.operations
            ],
            "setups": [
                s.model_copy(update={"system_setup_time_accepted": True})
                if s.system_setup_time_minutes is not None
                else s
                for s in plan.setups
            ],
        }
    )


def _bulk_recognize(data: bytes, filename: str):
    """(suggestions, classification) exactly as `/cad/operations/suggest` builds them."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="CAD file is above the upload limit.")
    _import(data, filename, CAPABILITY_FEATURES, with_geometry=False)
    try:
        recognition = recognize_features_from_bytes(data, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - one bad model must not stop the batch
        raise HTTPException(
            status_code=500,
            detail=f"Feature recognition failed unexpectedly ({type(exc).__name__}).",
        ) from exc
    suggestions = suggest_operations(recognition)
    return suggestions, classify_process(recognition, suggestions)


def _bulk_price_row(
    session: Session,
    row: "bulk_preview.BulkRow",
    files: dict[str, bytes],
    recognized: dict,
    default_rate_set: tuple[str | None, str | None],
    profile,
) -> "bulk_preview.BulkRowResult":
    rate_set_id = row.rate_set_id or default_rate_set[0]
    rate_set_version = row.rate_set_version or (
        default_rate_set[1] if row.rate_set_id is None else None
    )
    base = dict(
        line=row.line,
        file=row.file,
        part_id=row.part_id,
        part_name=row.part_name,
        material=row.material,
        quantity=row.quantity,
        rate_set_id=rate_set_id,
        rate_set_version=rate_set_version,
        route_id=row.route_id,
    )

    def failed(status: str, code: str | None, reason: str):
        return bulk_preview.BulkRowResult(**base, status=status, reason_code=code, reason=reason)

    if row.errors:
        return failed("error", "invalid_row", " ".join(row.errors))
    data = files.get(row.file)
    if data is None:
        return failed(
            "error",
            "file_not_uploaded",
            f"File '{row.file}' was not uploaded. Uploaded: {', '.join(sorted(files)) or 'none'}.",
        )
    if not rate_set_id:
        return failed("error", "no_rate_set", "No rate_set_id on the row and no batch default.")

    try:
        rate_set = RateSetRepository(session).get(rate_set_id, rate_set_version)
        resolve_material(rate_set, row.material)
    except ValueError as exc:
        return failed("error", "unknown_rate_set_or_material", str(exc))
    base["rate_set_version"] = rate_set.version

    try:
        if row.file not in recognized:
            try:
                recognized[row.file] = ("ok", _bulk_recognize(data, row.file))
            except Exception as exc:  # noqa: BLE001 - cached so a repeat row fails the same way
                recognized[row.file] = ("err", exc)
        kind, held = recognized[row.file]
        if kind == "err":
            raise held
        suggestions, classification = held

        route_label = None
        if row.route_id:
            classification = record_route_decision(
                classification, UserAction.EDITED, route_id=row.route_id
            )
            route_label = classification.route.label if classification.route else row.route_id

        plan = build_plan(
            suggestions.suggestions,
            units=suggestions.units,
            profile=profile,
            source_filename=row.file,
        )
        plan = _accept_for_preview(plan)
        priced = price_cad_part(
            session,
            data=data,
            filename=row.file,
            material=row.material,
            source_unit=row.source_unit,
            part_id=row.part_id,
            part_name=row.part_name,
            quantity=row.quantity,
            rate_set_id=rate_set_id,
            rate_set_version=rate_set.version,
            manufacturing_plan=json.dumps(plan.model_dump(mode="json")),
            process_classification=(
                classification.model_dump_json() if row.route_id else None
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a row's failure never aborts the batch
        code, reason, refused = _bulk_reason(exc)
        return failed("refused" if refused else "error", code, reason)

    cost = priced.result.part_cost
    omitted = [
        bulk_preview.OmittedOperation(
            operation_id=o.operation_id,
            operation_name=o.operation_name or o.operation_id,
            reason=o.system_time_reason or o.not_costable_reason or "No time could be estimated.",
        )
        for o in priced.plan.uncosted_operations()
    ]
    costed = len(priced.requirement_models)
    notes = []
    if costed == 0:
        notes.append("No operation carries a time, so this is the material cost only.")
    return bulk_preview.BulkRowResult(
        **base,
        status="priced",
        currency=cost.currency,
        totals=bulk_preview.BulkTotals(
            total_cost=round_money(cost.total_part_cost),
            per_unit_cost=round_money(cost.per_unit_cost),
            material_cost=round_money(cost.total_material_cost),
            operations_cost=round_money(cost.total_operation_cost),
            stages_cost=round_money(cost.total_stage_cost),
            additional_cost=round_money(cost.total_additional_cost),
        ),
        operations_costed=costed,
        operations_omitted=len(omitted),
        omitted_operations=omitted,
        operations_excluded_by_route=len(priced.plan.out_of_route_operations())
        + len(priced.plan.as_formed_operations()),
        notes=notes,
        route=route_label,
    )


def _bulk_summary(rows, uploaded: list[str]) -> "bulk_preview.BulkSummary":
    totals: dict[str, list] = {}
    for row in rows:
        if row.status == "priced":
            entry = totals.setdefault(row.currency, [0, 0.0])
            entry[0] += 1
            entry[1] += row.totals.total_cost
    named = {row.file for row in rows}
    return bulk_preview.BulkSummary(
        rows=len(rows),
        priced=sum(r.status == "priced" for r in rows),
        refused=sum(r.status == "refused" for r in rows),
        error=sum(r.status == "error" for r in rows),
        operations_omitted=sum(r.operations_omitted or 0 for r in rows),
        by_currency=[
            bulk_preview.CurrencyTotal(
                currency=currency, priced_rows=count, total_cost=round_money(total)
            )
            for currency, (count, total) in sorted(totals.items())
        ],
        currency_note=bulk_preview.CURRENCY_NOTE,
        unreferenced_files=[name for name in uploaded if name not in named],
    )


@app.post("/cad/bulk/preview", response_model=BulkPreviewResponse)
def bulk_preview_run(
    batch_csv: UploadFile = File(..., description="The batch CSV. See bulk_preview.py."),
    files: list[UploadFile] = File(..., description="The CAD files the CSV's `file` column names."),
    rate_set_id: str | None = Form(None, description="Default for rows with no rate_set_id."),
    rate_set_version: str | None = Form(None),
    time_profile_id: str | None = Form(None, description="Time profile; omit for the built-in baseline."),
    time_profile_version: str | None = Form(None),
    format: str = Query("json", description="'json' or 'csv'."),
    session: Session = Depends(get_session),
):
    """Price a batch of parts as an unreviewed preview. Stores nothing.

    Not an estimate and not quotable: every time is the system's own unreviewed
    prediction. A row that cannot be priced is reported with its reason and no
    cost; the other rows still run.
    """
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format must be 'json' or 'csv'.")
    raw_csv = batch_csv.file.read(MAX_BULK_CSV_BYTES + 1)
    if len(raw_csv) > MAX_BULK_CSV_BYTES:
        raise HTTPException(status_code=413, detail="The batch CSV is above the size limit.")
    try:
        text = raw_csv.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="The batch CSV is not UTF-8 text.") from exc

    if len(files) > MAX_BULK_FILES:
        raise HTTPException(
            status_code=413, detail=f"{len(files)} files uploaded; the limit is {MAX_BULK_FILES}."
        )
    uploaded: dict[str, bytes] = {}
    total = 0
    for upload in files:
        name = upload.filename or ""
        if name in uploaded:
            raise HTTPException(status_code=422, detail=f"File '{name}' was uploaded twice.")
        data = upload.file.read()
        total += len(data)
        if len(data) > MAX_UPLOAD_BYTES or total > MAX_BULK_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="The CAD files are above the size limit.")
        uploaded[name] = data

    try:
        parsed = bulk_preview.parse_batch_csv(text, max_rows=MAX_BULK_ROWS)
    except bulk_preview.BatchRefused as exc:
        raise HTTPException(
            status_code=422, detail={"error": "bulk_batch_refused", "message": str(exc)}
        ) from exc

    profile = _resolve_time_profile(session, time_profile_id, time_profile_version)
    recognized: dict = {}
    results = [
        _bulk_price_row(
            session, row, uploaded, recognized, (rate_set_id, rate_set_version), profile
        )
        for row in parsed.rows
    ]
    csv_text = bulk_preview.render_csv(results)
    if format == "csv":
        return Response(
            content=("﻿" + csv_text).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={
                "content-disposition": 'attachment; filename="bulk-preview-UNREVIEWED.csv"'
            },
        )
    return BulkPreviewResponse(
        time_profile=(
            f"{profile.time_profile_id}@{profile.version}"
            if profile is not None
            else "built-in baseline"
        ),
        rows=results,
        summary=_bulk_summary(results, sorted(uploaded)),
        csv=csv_text,
    )


# -------------------------------------------------------------- matrix preview
#
# One part priced as a grid: rate-set versions down, quantities across. Every
# cell goes through `price_cad_part`, the same path `/cost/cad-part` uses, with
# the plan exactly as it was sent (no time is accepted here), so a cell is the
# figure an estimate with that rate set and quantity would have. Nothing is
# stored, nothing is ordered by cost and nothing is chosen.

from cad_costing_agent import matrix_preview  # noqa: E402
from cad_costing_agent.limits import MAX_MATRIX_CELLS  # noqa: E402


def _matrix_rate_set(
    session: Session, request: "matrix_preview.RateSetRequest"
) -> "matrix_preview.MatrixRateSet":
    """The row's rate set as stored, or why it is not there. Read only, to label the row."""
    asked = dict(requested_rate_set_id=request.rate_set_id, requested_version=request.version)
    try:
        rate_set = RateSetRepository(session).get(request.rate_set_id, request.version)
    except ValueError as exc:
        return matrix_preview.MatrixRateSet(**asked, resolved=False, reason=str(exc))
    return matrix_preview.MatrixRateSet(
        **asked,
        resolved=True,
        rate_set_id=rate_set.rate_set_id,
        name=rate_set.name,
        version=rate_set.version,
        currency=rate_set.currency,
        is_illustrative=rate_set.is_illustrative,
    )


@app.post("/cost/matrix-preview", response_model=matrix_preview.MatrixPreview)
def cost_matrix_preview(
    file: UploadFile = File(...),
    rate_sets: str = Form(
        ...,
        description=(
            "JSON list of the rate sets to price under, in the order to show them, e.g. "
            '[{"rate_set_id": "poc-default"}, {"rate_set_id": "poc-machine-shop", "version": '
            '"1.0.0"}]. Omit `version` for the latest.'
        ),
    ),
    quantities: str = Form(
        ..., description="JSON list of the quantities to price at, in order, e.g. [1, 10, 100]."
    ),
    material: str = Form(...),
    source_unit: str = Form(...),
    part_id: str = Form(...),
    part_name: str = Form(...),
    operations: str = Form("[]"),
    operation_requirements: str = Form("[]"),
    overhead_rate_percent: float | None = Form(None),
    scrap_rate_percent: float | None = Form(None),
    tooling_cost_per_batch: float | None = Form(None),
    geometry_completion_method: str | None = Form(None),
    geometry_completion_volume_mm3: float | None = Form(None),
    manufacturing_plan: str | None = Form(None),
    raw_stock: str | None = Form(None),
    process_classification: str | None = Form(None),
    stage_inputs: str | None = Form(None),
    session: Session = Depends(get_session),
) -> "matrix_preview.MatrixPreview":
    """Price one CAD part under each rate set at each quantity, as a preview.

    Takes `/cost/cad-part`'s own inputs, with a list of rate sets and a list of
    quantities in place of one of each. A cell that cannot be priced (a rate set
    with no rate for a process in the plan, an unknown rate set) is listed with
    the pricing path's reason and no figures. Currencies are never converted.
    """
    try:
        requests = matrix_preview.parse_rate_set_requests(rate_sets)
        columns = matrix_preview.parse_quantities(quantities)
        matrix_preview.check_grid_size(len(requests), len(columns), MAX_MATRIX_CELLS)
    except matrix_preview.MatrixRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    data = file.file.read()
    rows: list[matrix_preview.MatrixRow] = []
    for request in requests:
        rate_set = _matrix_rate_set(session, request)
        cells: list[matrix_preview.MatrixCell] = []
        for quantity in columns:
            try:
                priced = price_cad_part(
                    session,
                    data=data,
                    filename=file.filename or "",
                    material=material,
                    source_unit=source_unit,
                    part_id=part_id,
                    part_name=part_name,
                    quantity=quantity,
                    operations=operations,
                    operation_requirements=operation_requirements,
                    rate_set_id=request.rate_set_id,
                    # The version the row's label names, so "latest" is one
                    # version for the whole row.
                    rate_set_version=rate_set.version if rate_set.resolved else request.version,
                    overhead_rate_percent=overhead_rate_percent,
                    scrap_rate_percent=scrap_rate_percent,
                    tooling_cost_per_batch=tooling_cost_per_batch,
                    geometry_completion_method=geometry_completion_method,
                    geometry_completion_volume_mm3=geometry_completion_volume_mm3,
                    manufacturing_plan=manufacturing_plan,
                    raw_stock=raw_stock,
                    process_classification=process_classification,
                    stage_inputs=stage_inputs,
                )
            except HTTPException as exc:
                code, message, stages = refusal_from_detail(exc.status_code, exc.detail)
                cells.append(
                    matrix_preview.MatrixCell(
                        rate_set=rate_set,
                        quantity=quantity,
                        status=matrix_preview.STATUS_REFUSED,
                        refusal=matrix_preview.MatrixRefusal(
                            code=code, message=message, stages=stages
                        ),
                    )
                )
                continue

            cost = priced.result.part_cost
            plan = priced.plan
            cells.append(
                matrix_preview.MatrixCell(
                    rate_set=rate_set,
                    quantity=quantity,
                    status=matrix_preview.STATUS_PRICED,
                    currency=cost.currency,
                    total_part_cost=cost.total_part_cost,
                    per_unit_cost=cost.per_unit_cost,
                    total_material_cost=cost.total_material_cost,
                    total_operation_cost=cost.total_operation_cost,
                    total_stage_cost=cost.total_stage_cost,
                    direct_cost_base=cost.direct_cost_base,
                    overhead_cost=cost.additional_costs.overhead_cost,
                    scrap_cost=cost.additional_costs.scrap_cost,
                    tooling_cost=cost.additional_costs.tooling_cost,
                    total_additional_cost=cost.total_additional_cost,
                    material_charged=cost.material_charged,
                    operations_costed=(
                        None if plan is None else len(priced.requirement_models)
                    ),
                    operations_not_timed=(
                        None if plan is None else len(plan.uncosted_operations())
                    ),
                )
            )
        rows.append(matrix_preview.MatrixRow(rate_set=rate_set, cells=cells))

    return matrix_preview.MatrixPreview(
        currency_note=matrix_preview.currency_note(row.rate_set.currency for row in rows),
        quantities=columns,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Advisory design-for-manufacturing flags and their threshold profiles.
#
# Kept as a separate endpoint rather than folded into /cad/operations/suggest:
# the suggest response is the proposal the plan builder consumes, and DFM advice
# must not be able to reach it. Recognition is re-run (as /cad/features does),
# not duplicated. Nothing here is persisted against an estimate.
# ---------------------------------------------------------------------------


@app.get("/dfm-profiles", response_model=list[DfmProfileSummary])
def dfm_profiles(session: Session = Depends(get_session)) -> list[DfmProfileSummary]:
    """List every stored DFM threshold profile version (illustrative, cited defaults)."""
    return DfmProfileRepository(session).list()


@app.get("/dfm-profiles/{dfm_profile_id}", response_model=DfmProfile)
def dfm_profile(
    dfm_profile_id: str, version: str | None = None, session: Session = Depends(get_session)
) -> DfmProfile:
    """Fetch one DFM profile with each threshold's source. Omit `version` for the latest."""
    try:
        return DfmProfileRepository(session).get(dfm_profile_id, version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/dfm-profiles/{dfm_profile_id}/versions", response_model=DfmProfile, status_code=201)
def create_dfm_profile_version(
    dfm_profile_id: str,
    version: DfmProfileVersionCreate,
    session: Session = Depends(get_session),
) -> DfmProfile:
    """Publish a new version of a DFM profile. An existing version is immutable (409)."""
    try:
        profile = DfmProfile(dfm_profile_id=dfm_profile_id, **version.model_dump())
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    repository = DfmProfileRepository(session)
    try:
        repository.add(profile)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return repository.get(profile.dfm_profile_id, profile.version)


@app.post("/cad/dfm", response_model=DfmReport)
async def cad_dfm(
    file: UploadFile = File(...),
    dfm_profile_id: str = Form(DEFAULT_DFM_PROFILE_ID),
    dfm_profile_version: str | None = Form(None, description="Omit for the latest version."),
    session: Session = Depends(get_session),
) -> DfmReport:
    """Advisory DFM flags for the features recognized in an uploaded CAD file.

    Same recognition as `/cad/features`. Changes no cost, time, suggestion or
    classification and persists nothing.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"CAD file is {len(data) / 1024 / 1024:.1f} MB, above the "
                f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB limit for this POC."
            ),
        )
    try:
        profile = DfmProfileRepository(session).get(dfm_profile_id, dfm_profile_version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _import(data, file.filename or "", CAPABILITY_FEATURES, with_geometry=False)
    try:
        # The shape is kept so the minimum-wall rule measures the B-Rep recognition read.
        recognized = recognize_features_with_shape(data, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a bad model must not take the app down
        raise HTTPException(
            status_code=500,
            detail=(
                "Feature recognition failed unexpectedly on this model "
                f"({type(exc).__name__}). DFM flags are unavailable; costing is unaffected."
            ),
        ) from exc
    recognition = recognized.recognition
    # Walls are measured only when the profile has a rule that reads them.
    wall = _wall_evidence(recognized, file.filename or "") if _profile_reads_walls(profile) else None
    report = evaluate_dfm(recognition.features, profile, file.filename or "", wall=wall)
    report.warnings.extend(recognition.warnings)
    return report


# ---------------------------------------------------------------- cost drivers
#
# What makes up one stored estimate's cost, each figure with its share of the
# stored total. Read from the result snapshot by `cost_drivers`, which imports
# `money` only: nothing is recalculated, looked up, ranked or stored.

from cad_costing_agent import cost_drivers  # noqa: E402


@app.get(
    "/estimates/{estimate_id}/cost-drivers", response_model=cost_drivers.CostDriverBreakdown
)
def estimate_cost_drivers(
    estimate_id: str, session: Session = Depends(get_session)
) -> cost_drivers.CostDriverBreakdown:
    """The stored total broken into the figures that built it, in build-up order."""
    try:
        stored = EstimateRepository(session).get(estimate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return cost_drivers.cost_drivers(stored.estimate_id, stored.result_snapshot)


# ------------------------------------------------------------- wall thickness
#
# The measured wall thickness of an exact B-Rep (`features.wall_thickness`), and
# the one place it becomes the minimum-wall DFM rule's evidence. Analysis only:
# nothing here reaches a mass, a time, a route or a cost, and nothing is stored.
# The shape measured is the one recognition read (`recognize_features_with_shape`),
# so the file is parsed once, by the one reader, and the same representation
# decides what can be measured. A faceted export, an STL or an IGES surface
# model is refused with its reason; over the ray budget the measurement degrades
# and says so, and an estimate is never touched.

from cad_costing_agent.dfm import RuleId, WallEvidence  # noqa: E402
from cad_costing_agent.features import (  # noqa: E402
    RecognizedShape,
    recognize_features_with_shape,
)
from cad_costing_agent.features import wall_thickness  # noqa: E402


def _measure_walls(recognized: RecognizedShape, filename: str) -> wall_thickness.WallThicknessResult:
    """The one path from a recognized upload to its wall measurement."""
    reason = wall_thickness.refusal_reason(
        recognized.recognition.representation, recognized.shape is not None
    )
    if reason is not None:
        return wall_thickness.not_measured(reason, filename=filename)
    return wall_thickness.measure_wall_thickness(recognized.shape, filename=filename)


def _profile_reads_walls(profile: DfmProfile) -> bool:
    return profile.setting(RuleId.MIN_WALL) is not None


def _wall_evidence(recognized: RecognizedShape, filename: str) -> WallEvidence:
    """The measurement as the DFM rule's plain carrier, copied field by field."""
    try:
        result = _measure_walls(recognized, filename)
    except Exception as exc:  # noqa: BLE001 - a failed measurement skips one rule, no more
        return WallEvidence(
            measured=False,
            not_measured_reason=(
                f"Wall-thickness measurement failed on this model ({type(exc).__name__})."
            ),
        )
    minimum = result.minimum
    return WallEvidence(
        measured=result.measured,
        not_measured_reason=result.not_measured_reason,
        minimum_mm=minimum.thickness_mm if minimum else None,
        from_face=minimum.from_face if minimum else None,
        to_face=minimum.to_face if minimum else None,
        location=minimum.midpoint if minimum else None,
        direction=minimum.direction if minimum else None,
        face_minima=[
            (face.face_id, face.minimum_mm) for face in result.faces if face.minimum_mm is not None
        ],
        method=result.method,
        method_description=result.method_description,
        limitations=list(result.limitations),
        grid_side=result.grid_side,
        budget_limited=result.budget_limited,
        budget_note=result.budget_note,
    )


@app.post("/cad/wall-thickness", response_model=wall_thickness.WallThicknessResult)
async def cad_wall_thickness(file: UploadFile = File(...)) -> wall_thickness.WallThicknessResult:
    """Measure the walls of an uploaded exact B-Rep part: the thinnest found, per face, and a
    fixed-bin distribution, with the method and what it can miss.

    Read-only analysis. Changes no cost, time, route or suggestion and persists
    nothing. Refused (400) for geometry it cannot measure honestly — a faceted
    export, an STL, an IGES surface model — with the reason.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"CAD file is {len(data) / 1024 / 1024:.1f} MB, above the "
                f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB limit for this POC."
            ),
        )
    filename = file.filename or ""
    # The same capability as recognition: walls are measured on the shape it reads.
    _import(data, filename, CAPABILITY_FEATURES, with_geometry=False)
    try:
        recognized = recognize_features_with_shape(data, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a bad model must not take the app down
        raise HTTPException(
            status_code=500,
            detail=(
                f"This model could not be read for analysis ({type(exc).__name__}). "
                "Geometry, the 3D preview and costing are unaffected."
            ),
        ) from exc
    reason = wall_thickness.refusal_reason(
        recognized.recognition.representation, recognized.shape is not None
    )
    if reason is not None:
        raise HTTPException(status_code=400, detail=reason)
    try:
        # Over budget, or with no solid, this still answers 200 with `measured` false and
        # the reason: the file is fine, this one analysis is what degraded.
        return _measure_walls(recognized, filename)
    except Exception as exc:  # noqa: BLE001 - a bad model must not take the app down
        raise HTTPException(
            status_code=500,
            detail=(
                f"Wall-thickness measurement failed unexpectedly on this model "
                f"({type(exc).__name__}). Geometry, the 3D preview and costing are unaffected."
            ),
        ) from exc
