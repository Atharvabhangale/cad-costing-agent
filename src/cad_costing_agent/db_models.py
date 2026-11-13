"""SQLAlchemy ORM models.

These are storage models only. They are deliberately kept separate from the
Pydantic domain models (`RateSet`, `PartCostEstimateResult`, ...): nothing in
the deterministic costing modules imports this file, and the repository layer
is the only place the two representations are translated into each other.

The rate detail (material rates, operation rates, overhead/scrap/tooling) is
stored as a single JSON column rather than in child tables. A rate set version
is immutable and is always read and written as a whole, so splitting it across
tables would add joins and partial-update risk without buying anything here.
Identity — `rate_set_id` and `version` — stays in real indexed columns, since
that is what gets queried.
"""

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cad_costing_agent.money import DEFAULT_CURRENCY


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RateSetRow(Base):
    """One immutable version of a rate set.

    `(rate_set_id, version)` is unique. There is deliberately no update path
    anywhere in the repository: changing rates means inserting a new version.
    """

    __tablename__ = "rate_sets"
    __table_args__ = (UniqueConstraint("rate_set_id", "version", name="uq_rate_set_identity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rate_set_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(512), default="")
    is_illustrative: Mapped[bool] = mapped_column(Boolean, default=True)
    currency: Mapped[str] = mapped_column(String(3), default=DEFAULT_CURRENCY,
                                          server_default=DEFAULT_CURRENCY)
    rates: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)


class TimeProfileRow(Base):
    """One immutable version of a set of machining-time assumptions.

    The exact counterpart of `RateSetRow`, and stored the same way: the
    parameters live in one JSON column because a profile version is immutable
    and always read and written whole, while identity — `time_profile_id` and
    `version` — stays in real indexed columns because that is what gets queried.

    Deliberately has no currency column. Nothing stored here is money.
    """

    __tablename__ = "time_profiles"
    __table_args__ = (
        UniqueConstraint("time_profile_id", "version", name="uq_time_profile_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time_profile_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(512), default="")
    is_illustrative: Mapped[bool] = mapped_column(Boolean, default=True)
    assumptions: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)


class DfmProfileRow(Base):
    """One immutable version of a set of DFM thresholds (advisory flags only)."""

    __tablename__ = "dfm_profiles"
    __table_args__ = (UniqueConstraint("dfm_profile_id", "version", name="uq_dfm_profile_identity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dfm_profile_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(512), default="")
    is_illustrative: Mapped[bool] = mapped_column(Boolean, default=True)
    rules: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)


class EstimateRow(Base):
    """A completed estimate, kept for audit.

    The three JSON snapshot columns are what make a stored estimate
    self-contained: they record what was asked for, which rates were resolved,
    and the full computed breakdown, so a historical estimate stays readable
    and unchanged no matter what rate set versions are added later.
    """

    __tablename__ = "estimates"

    # A monotonic surrogate key, used to order history by true insertion order.
    # `created_at` alone is not enough: the system clock's granularity is coarse
    # enough (~15 ms on Windows) that estimates created in quick succession can
    # share a timestamp, which would leave "newest first" ambiguous.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    estimate_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    source: Mapped[str] = mapped_column(String(32), index=True)

    part_id: Mapped[str] = mapped_column(String(64), index=True)
    part_name: Mapped[str] = mapped_column(String(128))
    material: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer)

    rate_set_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    rate_set_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Denominated once, at creation. A later rate set version cannot change it.
    currency: Mapped[str] = mapped_column(String(3), default=DEFAULT_CURRENCY,
                                          server_default=DEFAULT_CURRENCY)

    total_material_cost: Mapped[float] = mapped_column(Float)
    total_operation_cost: Mapped[float] = mapped_column(Float)
    total_additional_cost: Mapped[float] = mapped_column(Float)
    total_part_cost: Mapped[float] = mapped_column(Float)

    request_snapshot: Mapped[dict] = mapped_column(JSON)
    result_snapshot: Mapped[dict] = mapped_column(JSON)
    rate_set_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The manufacturing plan and its operation feedback as they stood when this
    # estimate was made: what the system suggested, what the engineer chose, and
    # in what order. Nullable because estimates made before Step 20 had no plan,
    # and because a plan is optional even now.
    plan_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The time-assumption profile(s) the plan's predictions were made under,
    # captured whole so publishing a later version can never change what a
    # historical estimate says it assumed. Nullable: estimates made before
    # Step 21, and any made without a plan, have none.
    time_profile_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class QuotationRow(Base):
    """One issued quotation.

    Holds only what a quotation adds to the estimate it presents: the metadata a
    person typed onto the document, and the totals as issued. The figures are
    deliberately **not** copied here — they are re-read from the estimate's own
    immutable snapshots whenever the document is rendered, so there is one source
    for them rather than two that could drift apart. `total_at_issue` exists to
    be checked against a re-render, which is what makes "this is the same
    document that was sent" verifiable instead of merely asserted.

    Like a rate set version, there is no update path anywhere in the repository.
    An issued quotation is a document somebody already has; a correction is a new
    quotation with a new number.
    """

    __tablename__ = "quotations"
    __table_args__ = (UniqueConstraint("quote_number", name="uq_quotation_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quote_number: Mapped[str] = mapped_column(String(64), index=True)
    estimate_id: Mapped[str] = mapped_column(String(32), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)

    customer_name: Mapped[str] = mapped_column(String(200), default="")
    project_name: Mapped[str] = mapped_column(String(200), default="")
    quote_reference: Mapped[str] = mapped_column(String(64), default="")
    prepared_by: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(String(2000), default="")
    validity: Mapped[str] = mapped_column(String(200), default="")

    currency: Mapped[str] = mapped_column(String(3), default=DEFAULT_CURRENCY)
    total_at_issue: Mapped[float | None] = mapped_column(Float, nullable=True)
    per_unit_at_issue: Mapped[float | None] = mapped_column(Float, nullable=True)


class CostActualRow(Base):
    """A cost somebody outside this application states for an estimate's part.

    Recorded against an estimate and never fed back into one. Immutable like a
    quotation: there is no update path anywhere in the repository, and a
    correction is a new row whose `supersedes_actual_id` names the one it
    replaces. That column is unique, so a record is corrected at most once and
    the history of corrections stays a single line.

    The breakdown columns are nullable because a client may supply only a total;
    `NULL` there means "not supplied", which is a different statement from zero.
    """

    __tablename__ = "cost_actuals"
    __table_args__ = (
        UniqueConstraint("actual_id", name="uq_cost_actual_id"),
        UniqueConstraint("supersedes_actual_id", name="uq_cost_actual_supersedes"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actual_id: Mapped[str] = mapped_column(String(32))
    estimate_id: Mapped[str] = mapped_column(String(32), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)

    source: Mapped[str] = mapped_column(String(200))
    # What kind of figure the row is: `actual` or `supplier_quote`. No default
    # here or in the schema; the caller states it. `source` above is the free
    # text saying where the figure came from, which is a different question.
    figure_kind: Mapped[str] = mapped_column(String(16))
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    quote_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cost_basis: Mapped[str] = mapped_column(String(16))
    batch_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3))

    total_cost: Mapped[float] = mapped_column(Float)
    material_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    machining_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    additional_cost: Mapped[float | None] = mapped_column(Float, nullable=True)

    cost_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    client_part_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    drawing_revision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str] = mapped_column(String(2000), default="")
    supersedes_actual_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


# =============================================================================
# Access control: users, estimate approval events, audit log (revision 0011).
# Opt-in: with CAD_COSTING_AUTH_ENABLED off the users table is unused, while
# approval events and audit rows are still written, attributed to `local`.
# =============================================================================


class UserRow(Base):
    """A person who can be identified by an API token.

    The token itself is never stored. `token_id` is a public lookup key (the
    middle part of the token), and `token_hash` is PBKDF2-HMAC-SHA256 of the
    secret part under `token_salt`. The only mutable field is `deactivated_at`:
    a user is switched off, never deleted, so the actor ids in the approval
    events and audit log keep naming someone.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_id"),
        UniqueConstraint("token_id", name="uq_user_token_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(16))
    token_id: Mapped[str] = mapped_column(String(32))
    token_salt: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(128))
    kdf: Mapped[str] = mapped_column(String(32))
    kdf_iterations: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deactivated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class EstimateAttributionRow(Base):
    """Who created an estimate: `local` with auth off, a user id with it on.

    Its own table rather than a column on `estimates`, so the estimate row —
    and every snapshot, query and test written against it — is untouched.
    Written once, in the same flush as the estimate, by `auth.py`'s session
    listener; no endpoint writes it. An estimate stored before revision 0011,
    or outside a request, has no row: "not recorded", which is not the same
    statement as anonymous.
    """

    __tablename__ = "estimate_attributions"
    __table_args__ = (UniqueConstraint("estimate_id", name="uq_estimate_attribution"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    estimate_id: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(String(64))
    authenticated: Mapped[bool] = mapped_column(Boolean)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)


class ApprovalEventRow(Base):
    """One event in an estimate's approval history. Never updated or deleted.

    `(estimate_id, sequence)` is unique, so two reviewers acting on the same
    estimate at once cannot both append to the same state: the second insert
    fails and is refused rather than recorded out of order.
    """

    __tablename__ = "approval_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_approval_event_id"),
        UniqueConstraint("estimate_id", "sequence", name="uq_approval_event_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(32))
    estimate_id: Mapped[str] = mapped_column(String(32), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(64))
    actor_name: Mapped[str] = mapped_column(String(200))
    actor_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    authenticated: Mapped[bool] = mapped_column(Boolean)
    comment: Mapped[str] = mapped_column(String(2000), default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)


class AuditEventRow(Base):
    """One mutating request: who, what, against which record, and how it ended.

    Written by one middleware (`auth.AccessMiddleware`) and nowhere else. It
    holds no request body, no query string and no header, so a token, a
    password or an uploaded file cannot reach it.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    authenticated: Mapped[bool] = mapped_column(Boolean)
    method: Mapped[str] = mapped_column(String(8))
    route: Mapped[str | None] = mapped_column(String(200), nullable=True)
    path: Mapped[str] = mapped_column(String(500))
    target_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    created_ids: Mapped[list] = mapped_column(JSON)
    status_code: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(16))
