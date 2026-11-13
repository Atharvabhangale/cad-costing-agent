"""Repository layer: the only place ORM rows and domain models are translated.

Nothing in the deterministic costing modules imports this file. Endpoints read
rate sets through `RateSetRepository`, hand the resolved `RateSet` to the pure
calculators, and write the outcome through `EstimateRepository`.
"""

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cad_costing_agent.cost_actuals import ActualCostInput, ActualCostRecord
from cad_costing_agent.db_models import (
    CostActualRow,
    DfmProfileRow,
    EstimateRow,
    QuotationRow,
    RateSetRow,
    TimeProfileRow,
)
from cad_costing_agent.dfm import DfmProfile, DfmProfileSummary
from cad_costing_agent.estimates import EstimateListPage, EstimateSummary
from cad_costing_agent.money import Money, round_money
from cad_costing_agent.rate_sets import RateSet, RateSetSummary, version_key
from cad_costing_agent.time_profiles import TimeProfile, TimeProfileSummary


class StoredEstimate(BaseModel):
    """A persisted estimate as returned by `GET /estimates/{estimate_id}`."""

    estimate_id: str
    created_at: datetime
    source: str
    part_id: str
    part_name: str
    material: str
    quantity: int
    rate_set_id: str | None = None
    rate_set_version: str | None = None
    currency: str
    total_material_cost: Money
    total_operation_cost: Money
    total_additional_cost: Money
    total_part_cost: Money
    request_snapshot: dict
    result_snapshot: dict
    rate_set_snapshot: dict | None = None
    plan_snapshot: dict | None = None
    time_profile_snapshot: dict | None = None


def _to_naive_utc(moment: datetime) -> datetime:
    """Normalize a filter bound to match how timestamps are stored.

    `created_at` is written as naive UTC, so an aware datetime from a query
    string has to be converted before it can be compared meaningfully.
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _row_to_summary(row: EstimateRow) -> EstimateSummary:
    return EstimateSummary(
        estimate_id=row.estimate_id,
        sequence=row.id,
        created_at=row.created_at,
        source=row.source,
        part_id=row.part_id,
        part_name=row.part_name,
        quantity=row.quantity,
        rate_set_id=row.rate_set_id,
        rate_set_version=row.rate_set_version,
        currency=row.currency,
        total_material_cost=row.total_material_cost,
        total_operation_cost=row.total_operation_cost,
        total_additional_cost=row.total_additional_cost,
        total_part_cost=row.total_part_cost,
    )


def _row_to_rate_set(row: RateSetRow) -> RateSet:
    return RateSet(
        rate_set_id=row.rate_set_id,
        name=row.name,
        version=row.version,
        description=row.description,
        is_illustrative=row.is_illustrative,
        currency=row.currency,
        **row.rates,
    )


def _row_to_time_profile(row: TimeProfileRow) -> TimeProfile:
    return TimeProfile(
        time_profile_id=row.time_profile_id,
        name=row.name,
        version=row.version,
        description=row.description,
        is_illustrative=row.is_illustrative,
        assumptions=row.assumptions or {},
    )


class RateSetRepository:
    """Reads and inserts rate sets. There is deliberately no update method."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self) -> list[RateSetSummary]:
        rows = self.session.execute(select(RateSetRow)).scalars().all()
        ordered = sorted(rows, key=lambda row: (row.rate_set_id, version_key(row.version)))
        return [
            RateSetSummary(
                rate_set_id=row.rate_set_id,
                name=row.name,
                version=row.version,
                description=row.description,
                currency=row.currency,
                is_illustrative=row.is_illustrative,
                material_count=len((row.rates or {}).get("materials") or {}),
                process_count=len((row.rates or {}).get("operations") or {}),
                stage_rate_count=len((row.rates or {}).get("stage_rates") or {}),
                # --- factory label (Iteration 9): read from the version's own JSON ---
                region=((row.rates or {}).get("factory") or {}).get("region"),
                factory_name=((row.rates or {}).get("factory") or {}).get("factory_name"),
            )
            for row in ordered
        ]

    def get(self, rate_set_id: str, version: str | None = None) -> RateSet:
        """Resolve by id, and optionally by exact version.

        Omitting the version selects the highest version of that id. Version
        strings are compared numerically, not lexically, so 10.0.0 sorts above
        2.0.0.
        """
        rows = (
            self.session.execute(select(RateSetRow).where(RateSetRow.rate_set_id == rate_set_id))
            .scalars()
            .all()
        )
        if not rows:
            known = self.session.execute(select(RateSetRow.rate_set_id).distinct()).scalars().all()
            raise ValueError(
                f"Unknown rate set '{rate_set_id}'. Known rate sets: {', '.join(sorted(known)) or 'none'}"
            )

        if version is None:
            resolved = max(rows, key=lambda row: version_key(row.version))
        else:
            matches = [row for row in rows if row.version == version]
            if not matches:
                known_versions = ", ".join(sorted((row.version for row in rows), key=version_key))
                raise ValueError(
                    f"Rate set '{rate_set_id}' has no version '{version}'. "
                    f"Known versions: {known_versions}"
                )
            resolved = matches[0]

        return _row_to_rate_set(resolved)

    def exists(self, rate_set_id: str, version: str) -> bool:
        row = self.session.execute(
            select(RateSetRow).where(
                RateSetRow.rate_set_id == rate_set_id, RateSetRow.version == version
            )
        ).scalar_one_or_none()
        return row is not None

    def add(self, rate_set: RateSet) -> None:
        """Insert one rate set version.

        Rejects a duplicate `(rate_set_id, version)`: an existing version is
        immutable, so new rates require a new version.
        """
        if self.exists(rate_set.rate_set_id, rate_set.version):
            raise ValueError(
                f"Rate set '{rate_set.rate_set_id}' version '{rate_set.version}' already exists. "
                "Rate set versions are immutable — publish a new version instead."
            )

        payload = rate_set.model_dump(mode="json")
        row = RateSetRow(
            rate_set_id=rate_set.rate_set_id,
            version=rate_set.version,
            name=rate_set.name,
            description=rate_set.description,
            is_illustrative=rate_set.is_illustrative,
            currency=rate_set.currency,
            rates={
                "materials": payload["materials"],
                "operations": payload["operations"],
                "additional_costs": payload["additional_costs"],
                # Only when there are some, so a version without stage rates is
                # stored exactly as every version before them was.
                **({"stage_rates": payload["stage_rates"]} if payload["stage_rates"] else {}),
                # Likewise the factory label: absent from the payload when none
                # was stated (`exclude_if`), and then absent here too.
                **({"factory": payload["factory"]} if payload.get("factory") else {}),
            },
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:  # pragma: no cover - guarded by exists() above
            self.session.rollback()
            raise ValueError(
                f"Rate set '{rate_set.rate_set_id}' version '{rate_set.version}' already exists."
            ) from exc

    def seed(self, rate_sets: tuple[RateSet, ...]) -> int:
        """Insert any seed versions not already present. Returns how many were added."""
        inserted = 0
        for rate_set in rate_sets:
            if not self.exists(rate_set.rate_set_id, rate_set.version):
                self.add(rate_set)
                inserted += 1
        return inserted


class TimeProfileRepository:
    """Reads and inserts time-assumption profiles. There is no update method.

    Identical in shape to `RateSetRepository`, and for the same reason: a
    published version is immutable, so the only write is an insert of a new one.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self) -> list[TimeProfileSummary]:
        rows = self.session.execute(select(TimeProfileRow)).scalars().all()
        ordered = sorted(rows, key=lambda row: (row.time_profile_id, version_key(row.version)))
        return [
            TimeProfileSummary(
                time_profile_id=row.time_profile_id,
                name=row.name,
                version=row.version,
                description=row.description,
                is_illustrative=row.is_illustrative,
            )
            for row in ordered
        ]

    def get(self, time_profile_id: str, version: str | None = None) -> TimeProfile:
        """Resolve by id, and optionally by exact version.

        Omitting the version selects the highest, compared numerically rather
        than lexically, so 10.0.0 sorts above 2.0.0.
        """
        rows = (
            self.session.execute(
                select(TimeProfileRow).where(TimeProfileRow.time_profile_id == time_profile_id)
            )
            .scalars()
            .all()
        )
        if not rows:
            known = (
                self.session.execute(select(TimeProfileRow.time_profile_id).distinct())
                .scalars()
                .all()
            )
            raise ValueError(
                f"Unknown time profile '{time_profile_id}'. Known profiles: "
                f"{', '.join(sorted(known)) or 'none'}"
            )

        if version is None:
            resolved = max(rows, key=lambda row: version_key(row.version))
        else:
            matches = [row for row in rows if row.version == version]
            if not matches:
                known_versions = ", ".join(sorted((row.version for row in rows), key=version_key))
                raise ValueError(
                    f"Time profile '{time_profile_id}' has no version '{version}'. "
                    f"Known versions: {known_versions}"
                )
            resolved = matches[0]

        return _row_to_time_profile(resolved)

    def exists(self, time_profile_id: str, version: str) -> bool:
        row = self.session.execute(
            select(TimeProfileRow).where(
                TimeProfileRow.time_profile_id == time_profile_id,
                TimeProfileRow.version == version,
            )
        ).scalar_one_or_none()
        return row is not None

    def add(self, profile: TimeProfile) -> None:
        """Insert one profile version, rejecting a duplicate identity."""
        if self.exists(profile.time_profile_id, profile.version):
            raise ValueError(
                f"Time profile '{profile.time_profile_id}' version '{profile.version}' already "
                "exists. Time profile versions are immutable — publish a new version instead."
            )

        row = TimeProfileRow(
            time_profile_id=profile.time_profile_id,
            version=profile.version,
            name=profile.name,
            description=profile.description,
            is_illustrative=profile.is_illustrative,
            assumptions=profile.assumptions.model_dump(mode="json"),
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:  # pragma: no cover - guarded by exists() above
            self.session.rollback()
            raise ValueError(
                f"Time profile '{profile.time_profile_id}' version '{profile.version}' "
                "already exists."
            ) from exc

    def seed(self, profiles: tuple[TimeProfile, ...]) -> int:
        """Insert any seed versions not already present. Returns how many were added."""
        inserted = 0
        for profile in profiles:
            if not self.exists(profile.time_profile_id, profile.version):
                self.add(profile)
                inserted += 1
        return inserted


class DfmProfileRepository:
    """Reads and inserts DFM threshold profiles. There is no update method."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _to_profile(row: DfmProfileRow) -> DfmProfile:
        return DfmProfile(
            dfm_profile_id=row.dfm_profile_id,
            name=row.name,
            version=row.version,
            description=row.description,
            is_illustrative=row.is_illustrative,
            rules=row.rules or [],
        )

    def list(self) -> list[DfmProfileSummary]:
        rows = self.session.execute(select(DfmProfileRow)).scalars().all()
        ordered = sorted(rows, key=lambda row: (row.dfm_profile_id, version_key(row.version)))
        return [
            DfmProfileSummary(
                dfm_profile_id=row.dfm_profile_id,
                name=row.name,
                version=row.version,
                description=row.description,
                is_illustrative=row.is_illustrative,
            )
            for row in ordered
        ]

    def get(self, dfm_profile_id: str, version: str | None = None) -> DfmProfile:
        rows = (
            self.session.execute(
                select(DfmProfileRow).where(DfmProfileRow.dfm_profile_id == dfm_profile_id)
            )
            .scalars()
            .all()
        )
        if not rows:
            known = self.session.execute(select(DfmProfileRow.dfm_profile_id).distinct()).scalars().all()
            raise ValueError(
                f"Unknown DFM profile '{dfm_profile_id}'. Known profiles: "
                f"{', '.join(sorted(known)) or 'none'}"
            )
        if version is None:
            resolved = max(rows, key=lambda row: version_key(row.version))
        else:
            matches = [row for row in rows if row.version == version]
            if not matches:
                known_versions = ", ".join(sorted((row.version for row in rows), key=version_key))
                raise ValueError(
                    f"DFM profile '{dfm_profile_id}' has no version '{version}'. "
                    f"Known versions: {known_versions}"
                )
            resolved = matches[0]
        return self._to_profile(resolved)

    def exists(self, dfm_profile_id: str, version: str) -> bool:
        return (
            self.session.execute(
                select(DfmProfileRow).where(
                    DfmProfileRow.dfm_profile_id == dfm_profile_id,
                    DfmProfileRow.version == version,
                )
            ).scalar_one_or_none()
            is not None
        )

    def add(self, profile: DfmProfile) -> None:
        if self.exists(profile.dfm_profile_id, profile.version):
            raise ValueError(
                f"DFM profile '{profile.dfm_profile_id}' version '{profile.version}' already "
                "exists. DFM profile versions are immutable — publish a new version instead."
            )
        self.session.add(
            DfmProfileRow(
                dfm_profile_id=profile.dfm_profile_id,
                version=profile.version,
                name=profile.name,
                description=profile.description,
                is_illustrative=profile.is_illustrative,
                rules=[rule.model_dump(mode="json") for rule in profile.rules],
            )
        )
        self.session.flush()

    def seed(self, profiles: tuple[DfmProfile, ...]) -> int:
        inserted = 0
        for profile in profiles:
            if not self.exists(profile.dfm_profile_id, profile.version):
                self.add(profile)
                inserted += 1
        return inserted


class EstimateRepository:
    """Writes and reads persisted estimates. There is no update method."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def save(
        self,
        *,
        source: str,
        part_id: str,
        part_name: str,
        material: str,
        quantity: int,
        rate_set: RateSet | None,
        currency: str,
        total_material_cost: float,
        total_operation_cost: float,
        total_additional_cost: float,
        total_part_cost: float,
        request_snapshot: dict,
        result_snapshot: dict,
        plan_snapshot: dict | None = None,
        time_profile_snapshot: dict | None = None,
    ) -> str:
        """Persist one estimate and return its new id."""
        estimate_id = uuid.uuid4().hex
        row = EstimateRow(
            estimate_id=estimate_id,
            source=source,
            part_id=part_id,
            part_name=part_name,
            material=material,
            quantity=quantity,
            rate_set_id=rate_set.rate_set_id if rate_set is not None else None,
            rate_set_version=rate_set.version if rate_set is not None else None,
            currency=currency,
            # Stored at the presentation policy's 2 decimal places.
            total_material_cost=round_money(total_material_cost),
            total_operation_cost=round_money(total_operation_cost),
            total_additional_cost=round_money(total_additional_cost),
            total_part_cost=round_money(total_part_cost),
            request_snapshot=request_snapshot,
            result_snapshot=result_snapshot,
            rate_set_snapshot=rate_set.model_dump(mode="json") if rate_set is not None else None,
            plan_snapshot=plan_snapshot,
            time_profile_snapshot=time_profile_snapshot,
        )
        self.session.add(row)
        self.session.flush()
        return estimate_id

    def get(self, estimate_id: str) -> StoredEstimate:
        row = self.session.execute(
            select(EstimateRow).where(EstimateRow.estimate_id == estimate_id)
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"Unknown estimate '{estimate_id}'.")
        return StoredEstimate.model_validate(row, from_attributes=True)

    def count(self) -> int:
        return self.session.execute(select(func.count()).select_from(EstimateRow)).scalar_one()

    def recent_details(self, limit: int) -> list[StoredEstimate]:
        """The most recent estimates with their snapshots, newest first.

        Read-only, and bounded: the feedback analytics summarises history, and a
        window over the most recent N estimates is a useful answer while one
        that ran out of memory is not. `EstimateRow.id` orders it, for the same
        reason the history page uses it — `created_at`'s resolution is too
        coarse (~15 ms on Windows) to separate estimates made in quick
        succession.
        """
        rows = (
            self.session.execute(
                select(EstimateRow).order_by(EstimateRow.id.desc()).limit(limit)
            )
            .scalars()
            .all()
        )
        return [StoredEstimate.model_validate(row, from_attributes=True) for row in rows]

    def get_summary(self, estimate_id: str) -> EstimateSummary:
        row = self.session.execute(
            select(EstimateRow).where(EstimateRow.estimate_id == estimate_id)
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"Unknown estimate '{estimate_id}'.")
        return _row_to_summary(row)

    def list(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        part_id: str | None = None,
        rate_set_id: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> EstimateListPage:
        """One page of estimate history, newest first.

        `total` counts everything matching the filters, not just this page.
        Ordering uses the monotonic surrogate key rather than `created_at`, so
        paging stays in true insertion order even when several estimates share a
        timestamp because of clock granularity.
        """
        filters = []
        if part_id is not None:
            filters.append(EstimateRow.part_id == part_id)
        if rate_set_id is not None:
            filters.append(EstimateRow.rate_set_id == rate_set_id)
        if created_after is not None:
            filters.append(EstimateRow.created_at >= _to_naive_utc(created_after))
        if created_before is not None:
            filters.append(EstimateRow.created_at <= _to_naive_utc(created_before))

        total = self.session.execute(
            select(func.count()).select_from(EstimateRow).where(*filters)
        ).scalar_one()

        rows = (
            self.session.execute(
                select(EstimateRow)
                .where(*filters)
                .order_by(EstimateRow.id.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )

        return EstimateListPage(
            total=total,
            limit=limit,
            offset=offset,
            items=[_row_to_summary(row) for row in rows],
        )


class IssuedQuotation(BaseModel):
    """An issued quotation's own record: the metadata and the totals as issued.

    The document's figures are not here. They are read back from the estimate's
    immutable snapshots whenever it is rendered, so there is one source for them.
    `total_at_issue` exists so a re-render can be *checked* against what was
    issued rather than trusted.
    """

    quote_number: str
    estimate_id: str
    issued_at: datetime
    customer_name: str = ""
    project_name: str = ""
    quote_reference: str = ""
    prepared_by: str = ""
    notes: str = ""
    validity: str = ""
    currency: str
    total_at_issue: Money | None = None
    per_unit_at_issue: Money | None = None


class QuotationRepository:
    """Issues and reads quotations. There is deliberately no update method.

    An issued quotation is a document somebody already has, so it is immutable
    for the same reason a rate set version is: correcting one means issuing a new
    quotation with a new number, not rewriting what was sent.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def next_quote_number(self, prefix: str = "Q") -> str:
        """The next sequential number, from how many exist.

        Deliberately the simplest thing that produces a readable reference:
        `Q-0001`. There is no reservation, no per-customer series and no
        year-based reset, because none of that is needed to put an identifier on
        a document, and a caller who wants their own reference supplies one.
        """
        count = self.session.execute(select(func.count()).select_from(QuotationRow)).scalar_one()
        return f"{prefix}-{count + 1:04d}"

    def issue(
        self,
        *,
        estimate_id: str,
        quote_number: str,
        customer_name: str = "",
        project_name: str = "",
        quote_reference: str = "",
        prepared_by: str = "",
        notes: str = "",
        validity: str = "",
        currency: str,
        total_at_issue: float | None,
        per_unit_at_issue: float | None,
        issued_at: datetime | None = None,
    ) -> IssuedQuotation:
        row = QuotationRow(
            quote_number=quote_number,
            estimate_id=estimate_id,
            issued_at=issued_at or datetime.now(timezone.utc).replace(tzinfo=None),
            customer_name=customer_name,
            project_name=project_name,
            quote_reference=quote_reference,
            prepared_by=prepared_by,
            notes=notes,
            validity=validity,
            currency=currency,
            total_at_issue=None if total_at_issue is None else round_money(total_at_issue),
            per_unit_at_issue=(
                None if per_unit_at_issue is None else round_money(per_unit_at_issue)
            ),
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ValueError(
                f"Quotation number '{quote_number}' has already been issued. A quote number "
                "identifies one document and is never reused; issue a new one."
            ) from exc
        return IssuedQuotation.model_validate(row, from_attributes=True)

    def get(self, quote_number: str) -> IssuedQuotation:
        row = self.session.execute(
            select(QuotationRow).where(QuotationRow.quote_number == quote_number)
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"Unknown quotation '{quote_number}'.")
        return IssuedQuotation.model_validate(row, from_attributes=True)

    def for_estimate(self, estimate_id: str) -> list[IssuedQuotation]:
        rows = (
            self.session.execute(
                select(QuotationRow)
                .where(QuotationRow.estimate_id == estimate_id)
                .order_by(QuotationRow.id.desc())
            )
            .scalars()
            .all()
        )
        return [IssuedQuotation.model_validate(row, from_attributes=True) for row in rows]


class CostActualRepository:
    """Records and reads actual costs and supplier quotes. There is deliberately
    no update or delete.

    Both kinds are rows of one table, told apart by `figure_kind`, which every
    record carries. Nothing here filters or aggregates by it.

    An actual is a statement somebody made about a real cost; changing it in
    place would lose what was first entered. A correction is recorded as a new
    actual naming the one it supersedes, and the superseded one stays readable.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, estimate_id: str, entry: ActualCostInput) -> ActualCostRecord:
        if entry.supersedes_actual_id is not None:
            previous = self._row(entry.supersedes_actual_id)
            if previous is None or previous.estimate_id != estimate_id:
                raise LookupError(
                    f"Actual cost '{entry.supersedes_actual_id}' is not recorded against "
                    f"estimate '{estimate_id}', so it cannot be corrected here."
                )
        row = CostActualRow(
            actual_id=uuid.uuid4().hex,
            estimate_id=estimate_id,
            recorded_at=datetime.now(timezone.utc).replace(tzinfo=None),
            **entry.model_dump(),
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ValueError(
                f"Actual cost '{entry.supersedes_actual_id}' has already been corrected. "
                "Correct the latest record instead, so the history stays one line."
            ) from exc
        return _row_to_actual(row)

    def get(self, actual_id: str) -> ActualCostRecord:
        row = self._row(actual_id)
        if row is None:
            raise ValueError(f"Unknown actual cost '{actual_id}'.")
        return _row_to_actual(row)

    def for_estimate(self, estimate_id: str) -> list[ActualCostRecord]:
        """Every record against the estimate — actuals and quotes — oldest first."""
        rows = (
            self.session.execute(
                select(CostActualRow)
                .where(CostActualRow.estimate_id == estimate_id)
                .order_by(CostActualRow.id)
            )
            .scalars()
            .all()
        )
        return [_row_to_actual(row) for row in rows]

    def _row(self, actual_id: str) -> CostActualRow | None:
        return self.session.execute(
            select(CostActualRow).where(CostActualRow.actual_id == actual_id)
        ).scalar_one_or_none()


def _row_to_actual(row: CostActualRow) -> ActualCostRecord:
    return ActualCostRecord.model_validate(row, from_attributes=True)


# =============================================================================
# Access control: users, estimate approval events, audit log (revision 0011).
# Each repository below is append-only except `UserRepository.deactivate`,
# which switches a user off without deleting them.
# =============================================================================

from sqlalchemy import String as _SqlString  # noqa: E402

from cad_costing_agent import credentials  # noqa: E402
from cad_costing_agent.approvals import (  # noqa: E402
    ApprovalEvent,
    ApprovalRefused,
    check_event,
)
from cad_costing_agent.db_models import (  # noqa: E402
    ApprovalEventRow,
    AuditEventRow,
    EstimateAttributionRow,
    UserRow,
)


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class StoredUser(BaseModel):
    """A user as the API shows one. Carries no token, salt or hash."""

    user_id: str
    name: str
    role: str
    created_at: datetime
    created_by: str | None = None
    deactivated_at: datetime | None = None
    deactivated_by: str | None = None


class UserRepository:
    """Creates, reads and deactivates users. A user is never deleted, and their
    token is never stored — only a salted hash of its secret part."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, *, name: str, role: str, created_by: str | None) -> tuple[StoredUser, str]:
        """Create a user and return them with their token. The token is not
        recoverable afterwards: this return value is the only place it exists."""
        if role not in credentials.ROLES:
            raise ValueError(f"Unknown role '{role}'. Roles: {', '.join(credentials.ROLES)}.")
        if not name.strip():
            raise ValueError("A user needs a name, so the audit log can say who acted.")
        token, token_id, secret = credentials.new_token()
        salt = credentials.new_salt()
        row = UserRow(
            user_id=f"u-{uuid.uuid4().hex[:12]}",
            name=name.strip(),
            role=role,
            token_id=token_id,
            token_salt=salt,
            token_hash=credentials.hash_secret(secret, salt, credentials.KDF_ITERATIONS),
            kdf=credentials.KDF_NAME,
            kdf_iterations=credentials.KDF_ITERATIONS,
            created_at=_utc_naive_now(),
            created_by=created_by,
        )
        self.session.add(row)
        self.session.flush()
        return StoredUser.model_validate(row, from_attributes=True), token

    def by_token(self, token: str) -> StoredUser | None:
        """The active user a presented token identifies, or None."""
        parsed = credentials.parse_token(token)
        if parsed is None:
            return None
        token_id, secret = parsed
        row = self.session.execute(
            select(UserRow).where(UserRow.token_id == token_id)
        ).scalar_one_or_none()
        if row is None:
            credentials.burn_equivalent_time(secret)
            return None
        if row.kdf != credentials.KDF_NAME or not credentials.secret_matches(
            secret, row.token_salt, row.kdf_iterations, row.token_hash
        ):
            return None
        if row.deactivated_at is not None:
            return None
        return StoredUser.model_validate(row, from_attributes=True)

    def get(self, user_id: str) -> StoredUser:
        row = self._row(user_id)
        if row is None:
            raise LookupError(f"Unknown user '{user_id}'.")
        return StoredUser.model_validate(row, from_attributes=True)

    def list(self) -> list[StoredUser]:
        rows = self.session.execute(select(UserRow).order_by(UserRow.id)).scalars().all()
        return [StoredUser.model_validate(row, from_attributes=True) for row in rows]

    def active_admin_count(self) -> int:
        return self.session.execute(
            select(func.count())
            .select_from(UserRow)
            .where(UserRow.role == "admin", UserRow.deactivated_at.is_(None))
        ).scalar_one()

    def deactivate(self, user_id: str, *, by: str) -> StoredUser:
        """Switch a user off. Their token stops working; their history stays."""
        row = self._row(user_id)
        if row is None:
            raise LookupError(f"Unknown user '{user_id}'.")
        if row.deactivated_at is not None:
            raise ValueError(f"User '{user_id}' is already deactivated.")
        if row.role == "admin" and self.active_admin_count() <= 1:
            raise ValueError(
                "This is the last active admin. Create another admin before deactivating it, "
                "or nobody could manage users again without the bootstrap script."
            )
        row.deactivated_at = _utc_naive_now()
        row.deactivated_by = by
        self.session.flush()
        return StoredUser.model_validate(row, from_attributes=True)

    def _row(self, user_id: str) -> UserRow | None:
        return self.session.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one_or_none()


class ApprovalEventRepository:
    """Records and reads an estimate's approval events. There is deliberately no
    update or delete: the current state is derived from these rows
    (`approvals.derive_state`), so editing one would rewrite history.

    `record` checks the rules itself (`approvals.check_event`) before inserting,
    so there is no way to append an event the rules refuse. The unique
    `(estimate_id, sequence)` constraint turns two simultaneous decisions on the
    same state into one recorded event and one refusal.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def estimate_creator(self, estimate_id: str) -> str | None:
        """Who created the estimate (None = not recorded). LookupError if unknown."""
        exists = self.session.execute(
            select(EstimateRow.id).where(EstimateRow.estimate_id == estimate_id)
        ).one_or_none()
        if exists is None:
            raise LookupError(f"Unknown estimate '{estimate_id}'.")
        return self.session.execute(
            select(EstimateAttributionRow.created_by).where(
                EstimateAttributionRow.estimate_id == estimate_id
            )
        ).scalar_one_or_none()

    def for_estimate(self, estimate_id: str) -> list[ApprovalEvent]:
        rows = (
            self.session.execute(
                select(ApprovalEventRow)
                .where(ApprovalEventRow.estimate_id == estimate_id)
                .order_by(ApprovalEventRow.sequence)
            )
            .scalars()
            .all()
        )
        return [ApprovalEvent.model_validate(row, from_attributes=True) for row in rows]

    def record(
        self,
        estimate_id: str,
        kind: str,
        *,
        actor_id: str,
        actor_name: str,
        actor_role: str | None,
        authenticated: bool,
        actor_is_admin: bool,
        separation_enforced: bool,
        comment: str = "",
    ) -> ApprovalEvent:
        created_by = self.estimate_creator(estimate_id)
        events = self.for_estimate(estimate_id)
        check_event(
            events,
            kind,
            actor_id=actor_id,
            actor_is_admin=actor_is_admin,
            created_by=created_by,
            separation_enforced=separation_enforced,
            comment=comment,
        )
        row = ApprovalEventRow(
            event_id=uuid.uuid4().hex,
            estimate_id=estimate_id,
            sequence=len(events) + 1,
            kind=kind,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            authenticated=authenticated,
            comment=comment,
            recorded_at=_utc_naive_now(),
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ApprovalRefused(
                "concurrent_decision",
                "Someone else recorded an approval event on this estimate at the same moment. "
                "Reload it and decide on what is now recorded.",
            ) from exc
        return ApprovalEvent.model_validate(row, from_attributes=True)


class AuditEntry(BaseModel):
    id: int
    recorded_at: datetime
    actor_id: str
    actor_role: str | None = None
    authenticated: bool
    method: str
    route: str | None = None
    path: str
    target_id: str | None = None
    created_ids: list[str]
    status_code: int
    outcome: str


class AuditPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AuditEntry]


class AuditRepository:
    """Appends and lists audit rows. No update or delete.

    Only `auth.AccessMiddleware` calls `record`; nothing else writes here.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        actor_id: str,
        actor_role: str | None,
        authenticated: bool,
        method: str,
        route: str | None,
        path: str,
        target_id: str | None,
        created_ids: list[str],
        status_code: int,
        outcome: str,
    ) -> None:
        self.session.add(
            AuditEventRow(
                recorded_at=_utc_naive_now(),
                actor_id=actor_id,
                actor_role=actor_role,
                authenticated=authenticated,
                method=method,
                route=route,
                path=path[:500],
                target_id=None if target_id is None else target_id[:200],
                created_ids=list(created_ids),
                status_code=status_code,
                outcome=outcome,
            )
        )
        self.session.flush()

    def list(
        self,
        *,
        target_id: str | None = None,
        actor_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> AuditPage:
        """Newest first. `target_id` matches the record a request acted on —
        its path parameter, or a record the request created."""
        query = select(AuditEventRow)
        if target_id:
            escaped = target_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(
                (AuditEventRow.target_id == target_id)
                | AuditEventRow.created_ids.cast(_SqlString).like(
                    f'%"{escaped}"%', escape="\\"
                )
            )
        if actor_id:
            query = query.where(AuditEventRow.actor_id == actor_id)
        total = self.session.execute(
            select(func.count()).select_from(query.subquery())
        ).scalar_one()
        rows = (
            self.session.execute(
                query.order_by(AuditEventRow.id.desc()).limit(limit).offset(offset)
            )
            .scalars()
            .all()
        )
        return AuditPage(
            total=total,
            limit=limit,
            offset=offset,
            items=[AuditEntry.model_validate(row, from_attributes=True) for row in rows],
        )


# --- Iteration 9: emission-factor catalogs (CO2e) ---------------------------

from cad_costing_agent.db_models import EmissionFactorCatalogRow  # noqa: E402
from cad_costing_agent.emission_factors import (  # noqa: E402
    EmissionFactorCatalog,
    EmissionFactorCatalogSummary,
)

_FACTOR_TABLES = ("materials", "process_electricity", "grid", "stage_factors")


class EmissionFactorCatalogRepository:
    """Reads and inserts emission-factor catalogs. There is no update method."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _to_catalog(row: EmissionFactorCatalogRow) -> EmissionFactorCatalog:
        return EmissionFactorCatalog(
            emission_catalog_id=row.emission_catalog_id,
            name=row.name,
            version=row.version,
            description=row.description,
            is_illustrative=row.is_illustrative,
            **(row.factors or {}),
        )

    def list(self) -> list[EmissionFactorCatalogSummary]:
        rows = self.session.execute(select(EmissionFactorCatalogRow)).scalars().all()
        ordered = sorted(rows, key=lambda row: (row.emission_catalog_id, version_key(row.version)))
        return [
            EmissionFactorCatalogSummary(
                emission_catalog_id=row.emission_catalog_id,
                name=row.name,
                version=row.version,
                description=row.description,
                is_illustrative=row.is_illustrative,
                material_count=len((row.factors or {}).get("materials") or {}),
                process_count=len((row.factors or {}).get("process_electricity") or {}),
                regions=sorted((row.factors or {}).get("grid") or {}),
            )
            for row in ordered
        ]

    def get(self, emission_catalog_id: str, version: str | None = None) -> EmissionFactorCatalog:
        rows = (
            self.session.execute(
                select(EmissionFactorCatalogRow).where(
                    EmissionFactorCatalogRow.emission_catalog_id == emission_catalog_id
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            known = (
                self.session.execute(
                    select(EmissionFactorCatalogRow.emission_catalog_id).distinct()
                )
                .scalars()
                .all()
            )
            raise ValueError(
                f"Unknown emission-factor catalog '{emission_catalog_id}'. Known catalogs: "
                f"{', '.join(sorted(known)) or 'none'}"
            )
        if version is None:
            resolved = max(rows, key=lambda row: version_key(row.version))
        else:
            matches = [row for row in rows if row.version == version]
            if not matches:
                known_versions = ", ".join(sorted((row.version for row in rows), key=version_key))
                raise ValueError(
                    f"Emission-factor catalog '{emission_catalog_id}' has no version "
                    f"'{version}'. Known versions: {known_versions}"
                )
            resolved = matches[0]
        return self._to_catalog(resolved)

    def exists(self, emission_catalog_id: str, version: str) -> bool:
        return (
            self.session.execute(
                select(EmissionFactorCatalogRow).where(
                    EmissionFactorCatalogRow.emission_catalog_id == emission_catalog_id,
                    EmissionFactorCatalogRow.version == version,
                )
            ).scalar_one_or_none()
            is not None
        )

    def add(self, catalog: EmissionFactorCatalog) -> None:
        if self.exists(catalog.emission_catalog_id, catalog.version):
            raise ValueError(
                f"Emission-factor catalog '{catalog.emission_catalog_id}' version "
                f"'{catalog.version}' already exists. Catalog versions are immutable — publish "
                "a new version instead."
            )
        payload = catalog.model_dump(mode="json")
        self.session.add(
            EmissionFactorCatalogRow(
                emission_catalog_id=catalog.emission_catalog_id,
                version=catalog.version,
                name=catalog.name,
                description=catalog.description,
                is_illustrative=catalog.is_illustrative,
                factors={key: payload[key] for key in _FACTOR_TABLES},
            )
        )
        self.session.flush()

    def seed(self, catalogs: tuple[EmissionFactorCatalog, ...]) -> int:
        inserted = 0
        for catalog in catalogs:
            if not self.exists(catalog.emission_catalog_id, catalog.version):
                self.add(catalog)
                inserted += 1
        return inserted
