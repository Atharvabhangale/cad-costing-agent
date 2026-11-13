"""Approval of an estimate: an append-only list of events, and the state they imply.

An estimate's approval state is never stored. It is *derived*, every time, by
replaying the estimate's events in order — `submitted`, `approved`, `rejected`,
`withdrawn` — so there is no mutable column that could say "approved" while the
events say otherwise. A recorded event is never edited or deleted; a change of
mind is a new event.

Approval changes no figure. An estimate's totals and snapshots are immutable,
and nothing here reads or writes them: approving says a person looked at the
figures and accepted them, not that the figures became different.

This module is pure. It imports nothing from the package, reads no clock and
touches no database; the repository hands it the events and the caller hands it
the actor.

Separation of duties: when authentication is on, a person may not approve an
estimate they created or the submission they made. With authentication off
every action is attributed to the one `local` actor, so the rule cannot be
applied and is reported as not enforced rather than silently passed.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EventKind = Literal["submitted", "approved", "rejected", "withdrawn"]
ApprovalState = Literal["draft", "submitted", "approved", "rejected", "withdrawn"]

EVENT_KINDS: tuple[str, ...] = ("submitted", "approved", "rejected", "withdrawn")

# Which states each event may follow. `draft` is the state of an estimate with
# no events at all. An approved estimate can be withdrawn (to be reviewed again,
# for instance after an approval recorded with auth off), never approved twice.
ALLOWED_FROM: dict[str, frozenset[str]] = {
    "submitted": frozenset({"draft", "rejected", "withdrawn"}),
    "approved": frozenset({"submitted"}),
    "rejected": frozenset({"submitted"}),
    "withdrawn": frozenset({"submitted", "approved"}),
}

# The event each state is reached by is the state's own name.
_STATE_AFTER: dict[str, str] = {kind: kind for kind in EVENT_KINDS}


class ApprovalEvent(BaseModel):
    """One recorded event. Immutable once written."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    estimate_id: str
    sequence: int = Field(description="1 for the estimate's first event, then 2, 3, ...")
    kind: EventKind
    actor_id: str
    actor_name: str
    actor_role: str | None = None
    authenticated: bool = Field(
        description=(
            "Whether the actor was identified by a credential. False for every event "
            "recorded with auth off, which are all attributed to `local`."
        )
    )
    comment: str = ""
    recorded_at: datetime


class ApprovalRefused(ValueError):
    """An event that the rules do not allow. `code` says which rule."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def derive_state(events: list[ApprovalEvent]) -> ApprovalState:
    """Replay the events in sequence order; the last legal transition is the state.

    Raises `ApprovalRefused("inconsistent_history", ...)` if the stored events
    are not a legal sequence, which can only happen if something wrote to the
    table without going through `check_event`.
    """
    state = "draft"
    for event in sorted(events, key=lambda item: item.sequence):
        if state not in ALLOWED_FROM[event.kind]:
            raise ApprovalRefused(
                "inconsistent_history",
                f"Event {event.sequence} ('{event.kind}') cannot follow state '{state}'.",
            )
        state = _STATE_AFTER[event.kind]
    return state  # type: ignore[return-value]


def latest(events: list[ApprovalEvent], kind: str) -> ApprovalEvent | None:
    found = [event for event in events if event.kind == kind]
    return max(found, key=lambda item: item.sequence) if found else None


def check_event(
    events: list[ApprovalEvent],
    kind: str,
    *,
    actor_id: str,
    actor_is_admin: bool,
    created_by: str | None,
    separation_enforced: bool,
    comment: str,
) -> None:
    """Refuse an event the rules do not allow; return None if it may be recorded."""
    if kind not in ALLOWED_FROM:
        raise ApprovalRefused("unknown_event", f"'{kind}' is not an approval event.")
    state = derive_state(events)
    if state not in ALLOWED_FROM[kind]:
        allowed = ", ".join(sorted(ALLOWED_FROM[kind]))
        raise ApprovalRefused(
            "not_allowed_in_state",
            f"An estimate that is '{state}' cannot be {kind}; this needs one that is {allowed}.",
        )

    submission = latest(events, "submitted")
    if kind == "approved" and separation_enforced:
        if submission is not None and submission.actor_id == actor_id:
            raise ApprovalRefused(
                "separation_of_duties",
                "You submitted this estimate, so you cannot approve it. Another reviewer must.",
            )
        if created_by is not None and created_by == actor_id:
            raise ApprovalRefused(
                "separation_of_duties",
                "You created this estimate, so you cannot approve it. Another reviewer must.",
            )

    if kind == "rejected" and not comment.strip():
        raise ApprovalRefused(
            "comment_required", "A rejection must say why, so the estimator can act on it."
        )

    if kind == "withdrawn" and separation_enforced and not actor_is_admin:
        if submission is None or submission.actor_id != actor_id:
            raise ApprovalRefused(
                "not_the_submitter",
                "Only the person who submitted this estimate, or an admin, can withdraw it.",
            )


class ApprovalStatus(BaseModel):
    """The derived state of one estimate, with every event behind it."""

    estimate_id: str
    state: ApprovalState
    events: list[ApprovalEvent]
    created_by: str | None = Field(
        default=None,
        description=(
            "Who created the estimate: `local` with auth off, a user id with it on, and "
            "null for an estimate stored before attribution existed — not recorded, not "
            "anonymous."
        ),
    )
    approved_with_authentication: bool = Field(
        description=(
            "True only when the state is approved and the approving event was recorded by "
            "an identified reviewer. An approval recorded with auth off does not satisfy "
            "the quotation gate when auth is on."
        )
    )
    auth_enabled: bool
    separation_of_duties_enforced: bool
    note: str


def status_of(
    estimate_id: str,
    events: list[ApprovalEvent],
    *,
    created_by: str | None,
    auth_enabled: bool,
) -> ApprovalStatus:
    state = derive_state(events)
    approval = latest(events, "approved") if state == "approved" else None
    if auth_enabled:
        note = (
            "Approval state is derived from the events below, which are never edited. "
            "Approving changes no figure in the estimate."
        )
    else:
        note = (
            "Auth off — every action is recorded as `local`, so separation of duties "
            "cannot be enforced. Approving changes no figure in the estimate."
        )
    return ApprovalStatus(
        estimate_id=estimate_id,
        state=state,
        events=sorted(events, key=lambda item: item.sequence),
        created_by=created_by,
        approved_with_authentication=bool(approval and approval.authenticated),
        auth_enabled=auth_enabled,
        separation_of_duties_enforced=auth_enabled,
        note=note,
    )
