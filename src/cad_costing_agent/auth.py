"""Opt-in access control: who is acting, what they may do, and the audit log.

Off by default. `CAD_COSTING_AUTH_ENABLED` unset (or 0/false/no/off) leaves
every endpoint, response and status exactly as it was: nothing is checked and
every action is attributed to the explicit `local` actor. The audit log and the
approval events are still written, so turning auth on later does not start the
history from nothing — but they say `local`, and with one actor separation of
duties cannot be enforced.

With it on (1/true/yes/on — and any value not recognised as off, so a typo
fails closed), a request needs `Authorization: Bearer <token>` for a user
created with `scripts/create_admin.py` or `POST /auth/users`. This is the
simplest honest identity mechanism for a proof of concept: per-user API tokens
stored as salted PBKDF2 hashes (`credentials.py`). It is not SSO or OIDC, has
no expiry and no rate limiting, and is not production-grade.

Everything is decided in one place, `AccessMiddleware`:

- **Authorization** comes from one table, `ROUTE_ROLES`. Every non-GET route
  must have an entry — a structural test enumerates the app's routes against
  it — and a mutating route missing from it is refused with auth on rather
  than allowed, so a new endpoint cannot silently skip authorization. A GET
  needs any signed-in user unless the table names a role for it.
- **The audit log** gets one row per mutating request (any method other than
  GET, HEAD or OPTIONS), whatever its outcome — including the 401s and 403s.
  It holds who, which route, which record, when and the status code. It never
  holds a request body, a query string or a header, so no token, password or
  uploaded file can reach it.

The middleware reads the database through the same `get_session` dependency
the endpoints use (honouring `app.dependency_overrides`, which is how the
tests' per-test database is reached), in its own short session that is
closed before the endpoint runs and opened again after the endpoint's session
has closed.
"""

import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict
from sqlalchemy import event
from sqlalchemy.orm import Session as OrmSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Match

from cad_costing_agent.db import get_session
from cad_costing_agent.db_models import Base, EstimateAttributionRow, EstimateRow
from cad_costing_agent.repository import AuditRepository, UserRepository

logger = logging.getLogger(__name__)

AUTH_ENABLED_ENV_VAR = "CAD_COSTING_AUTH_ENABLED"
_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


def auth_enabled() -> bool:
    """Read on every request, so a test (or an operator) can switch it."""
    return os.environ.get(AUTH_ENABLED_ENV_VAR, "").strip().lower() not in _OFF_VALUES


ESTIMATOR = "estimator"
REVIEWER = "reviewer"
ADMIN = "admin"
# Not a role a user holds: the requirement "any signed-in user".
AUTHENTICATED = "authenticated"

# What each role may do. A reviewer can also estimate (reviewers are usually
# senior estimators), and an admin can do everything — still subject to
# separation of duties, which is checked against the person, not the role.
ROLE_GRANTS: dict[str, frozenset[str]] = {
    ESTIMATOR: frozenset({ESTIMATOR}),
    REVIEWER: frozenset({ESTIMATOR, REVIEWER}),
    ADMIN: frozenset({ESTIMATOR, REVIEWER, ADMIN}),
}

# Routes that need no credential even with auth on: the page itself, its
# static files, the health check, the sign-in status the page asks first, and
# the API's own schema and docs.
PUBLIC_ROUTES: frozenset[str] = frozenset(
    {"/", "/health", "/auth/status", "/ui", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)

SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

# THE table. (method, route path as declared) -> the role required.
#
# Every non-GET route appears here; a GET appears only when it needs more than
# a signed-in user. Most POSTs under /cad and /cost compute and store nothing,
# but they are mapped like everything else: "is it a POST" is the test, not
# "does it look harmless", so a route that starts storing something later is
# already covered.
ROUTE_ROLES: dict[tuple[str, str], str] = {
    # --- publishing catalogs (admin) ---
    ("POST", "/rates/validate"): ADMIN,
    ("POST", "/rates"): ADMIN,
    ("POST", "/rates/{rate_set_id}/versions"): ADMIN,
    ("POST", "/time-profiles/validate"): ADMIN,
    ("POST", "/time-profiles"): ADMIN,
    ("POST", "/time-profiles/{time_profile_id}/versions"): ADMIN,
    ("POST", "/dfm-profiles/{dfm_profile_id}/versions"): ADMIN,
    # --- users and the audit log (admin) ---
    ("GET", "/auth/users"): ADMIN,
    ("POST", "/auth/users"): ADMIN,
    ("POST", "/auth/users/{user_id}/deactivate"): ADMIN,
    ("GET", "/audit"): ADMIN,
    # --- approval (reviewer decides; the estimator submits and withdraws) ---
    ("POST", "/estimates/{estimate_id}/approval/submit"): ESTIMATOR,
    ("POST", "/estimates/{estimate_id}/approval/withdraw"): ESTIMATOR,
    ("POST", "/estimates/{estimate_id}/approval/approve"): REVIEWER,
    ("POST", "/estimates/{estimate_id}/approval/reject"): REVIEWER,
    # --- records against an estimate (estimator) ---
    ("POST", "/estimates/{estimate_id}/quotation"): ESTIMATOR,
    ("POST", "/estimates/{estimate_id}/actuals"): ESTIMATOR,
    # --- costing that stores an estimate (estimator) ---
    ("POST", "/cost/material"): ESTIMATOR,
    ("POST", "/cost/operation"): ESTIMATOR,
    ("POST", "/cost/part"): ESTIMATOR,
    ("POST", "/cost/cad-part"): ESTIMATOR,
    ("POST", "/cost/assembly"): ESTIMATOR,
    # --- previews and analysis (estimator; nothing stored) ---
    ("POST", "/cost/route-comparison"): ESTIMATOR,
    ("POST", "/cost/matrix-preview"): ESTIMATOR,
    ("POST", "/cad/bulk/preview"): ESTIMATOR,
    ("POST", "/cad/import"): ESTIMATOR,
    ("POST", "/cad/assembly"): ESTIMATOR,
    ("POST", "/cad/assembly/features"): ESTIMATOR,
    ("POST", "/cad/assembly/plans"): ESTIMATOR,
    ("POST", "/cad/geometry"): ESTIMATOR,
    ("POST", "/cad/mesh"): ESTIMATOR,
    ("POST", "/cad/mass"): ESTIMATOR,
    ("POST", "/cad/features"): ESTIMATOR,
    ("POST", "/cad/operations/suggest"): ESTIMATOR,
    ("POST", "/cad/stock/decide"): ESTIMATOR,
    ("POST", "/cad/process/decide"): ESTIMATOR,
    ("POST", "/cad/dfm"): ESTIMATOR,
    ("POST", "/cad/wall-thickness"): ESTIMATOR,
    ("POST", "/cad/draw-direction"): ESTIMATOR,
    ("POST", "/cad/governing-wall"): ESTIMATOR,
    ("POST", "/cad/setups/propose"): ESTIMATOR,
    ("POST", "/cad/setups/apply-proposal"): ESTIMATOR,
    ("POST", "/cad/sheet-metal"): ESTIMATOR,
    ("POST", "/cad/sheet-stock/propose"): ESTIMATOR,
    ("POST", "/cad/sheet-stock/decide"): ESTIMATOR,
    ("POST", "/operations/plan-cost-preview"): ESTIMATOR,
    ("POST", "/operations/estimate-time"): ESTIMATOR,
    ("POST", "/operations/sheet-metal-lines"): ESTIMATOR,
    ("POST", "/operations/moulding-line"): ESTIMATOR,
}

# The requirement of a mutating route that is missing from the table.
UNMAPPED = "unmapped"


def required_role(method: str, route_path: str | None) -> str | None:
    """What a request to this route needs: None (public), AUTHENTICATED, a
    role, or UNMAPPED (a mutating route the table forgot — refused)."""
    if route_path is None or route_path in PUBLIC_ROUTES:
        return None
    lookup_method = "GET" if method == "HEAD" else method
    explicit = ROUTE_ROLES.get((lookup_method, route_path))
    if explicit is not None:
        return explicit
    return AUTHENTICATED if method in SAFE_METHODS else UNMAPPED


def role_allows(role: str | None, required: str) -> bool:
    if required == AUTHENTICATED:
        return role is not None
    return role is not None and required in ROLE_GRANTS.get(role, frozenset())


# ----------------------------------------------------------------- the actor


class Actor(BaseModel):
    """Who a request is attributed to."""

    model_config = ConfigDict(frozen=True)

    actor_id: str
    name: str
    role: str | None = None
    authenticated: bool

    def can(self, required: str) -> bool:
        return self.authenticated and role_allows(self.role, required)


LOCAL_ACTOR = Actor(actor_id="local", name="local", role=None, authenticated=False)
ANONYMOUS_ACTOR = Actor(actor_id="anonymous", name="anonymous", role=None, authenticated=False)


@dataclass
class _RequestContext:
    actor: Actor
    created_ids: list[str] = field(default_factory=list)


_REQUEST: ContextVar[_RequestContext | None] = ContextVar("cad_costing_request", default=None)


def current_actor() -> Actor:
    """FastAPI dependency: the actor the middleware resolved for this request."""
    context = _REQUEST.get()
    if context is not None:
        return context.actor
    return ANONYMOUS_ACTOR if auth_enabled() else LOCAL_ACTOR


# ------------------------------------------------- attribution at insert time
#
# Two ORM listeners, registered once here, so that no endpoint has to remember
# to attribute what it stores:
#
# - when an estimate is flushed during a request, an `estimate_attributions`
#   row naming the request's actor is added in the same flush;
# - after any row is inserted during a request, its identity is noted, so the
#   audit row can say which record a POST created.
#
# Outside a request (a script, a test calling a repository directly) neither
# does anything.

_IDENTITY_COLUMNS: dict[str, tuple[str, ...]] = {
    "estimates": ("estimate_id",),
    "quotations": ("quote_number",),
    "cost_actuals": ("actual_id",),
    "rate_sets": ("rate_set_id", "version"),
    "time_profiles": ("time_profile_id", "version"),
    "dfm_profiles": ("dfm_profile_id", "version"),
    "users": ("user_id",),
    "approval_events": ("event_id",),
}


_NOT_NOTED: frozenset[str] = frozenset({"estimate_attributions"})


@event.listens_for(OrmSession, "before_flush")
def _attribute_new_estimates(session, flush_context, instances) -> None:
    context = _REQUEST.get()
    if context is None:
        return
    for row in list(session.new):
        if isinstance(row, EstimateRow):
            session.add(
                EstimateAttributionRow(
                    estimate_id=row.estimate_id,
                    created_by=context.actor.actor_id,
                    authenticated=context.actor.authenticated,
                )
            )


@event.listens_for(Base, "after_insert", propagate=True)
def _note_creation(mapper, connection, target) -> None:
    context = _REQUEST.get()
    if context is None:
        return
    table = mapper.local_table.name
    if table in _NOT_NOTED:
        return
    columns = _IDENTITY_COLUMNS.get(table)
    if columns:
        identity = "@".join(str(getattr(target, name)) for name in columns)
    else:
        identity = f"{table}#{getattr(target, 'id', '?')}"
    context.created_ids.append(identity)


# ----------------------------------------------------------- the middleware


def _with_session(application, work):
    """Run `work(session)` on a session from the app's own `get_session`
    provider — the override, when a test has installed one."""
    provider = application.dependency_overrides.get(get_session, get_session)
    generator = provider()
    session = next(generator)
    try:
        return work(session)
    finally:
        generator.close()


def _actor_for_token(application, token: str) -> Actor | None:
    def work(session):
        user = UserRepository(session).by_token(token)
        if user is None:
            return None
        return Actor(actor_id=user.user_id, name=user.name, role=user.role, authenticated=True)

    return _with_session(application, work)


def _write_audit(application, **fields) -> None:
    def work(session):
        AuditRepository(session).record(**fields)
        session.commit()

    try:
        _with_session(application, work)
    except Exception:  # an audit failure must not turn a sent response into a crash
        logger.exception("The audit row for %s %s could not be written.", fields.get("method"), fields.get("path"))


def _bearer_token(scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            scheme, _, credential = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and credential.strip():
                return credential.strip()
            return None
    return None


def _matched_route(application, scope):
    """The route the router will dispatch to, as `(route, path_params)`, or
    `(None, {})` when nothing matches fully — a 404, a 405 or a slash redirect,
    none of which reach an endpoint."""
    for route in application.router.routes:
        match, child_scope = route.matches(scope)
        if match == Match.FULL:
            return route, child_scope.get("path_params", {})
    return None, {}


def _target_of(path_params: dict, created_ids: list[str]) -> str | None:
    if len(path_params) == 1:
        return str(next(iter(path_params.values())))
    if path_params:
        return ",".join(f"{key}={value}" for key, value in sorted(path_params.items()))
    return created_ids[0] if created_ids else None


def _outcome(status_code: int) -> str:
    if status_code < 400:
        return "succeeded"
    if status_code < 500:
        return "refused"
    return "failed"


class AccessMiddleware:
    """The one place a request is attributed, authorized and audited."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        application = scope["app"]
        method = scope["method"]
        route, path_params = _matched_route(application, scope)
        route_path = getattr(route, "path", None)
        enabled = auth_enabled()

        refusal: JSONResponse | None = None
        if not enabled:
            actor = LOCAL_ACTOR
        else:
            actor = ANONYMOUS_ACTOR
            token = _bearer_token(scope)
            if token:
                resolved = await run_in_threadpool(_actor_for_token, application, token)
                if resolved is not None:
                    actor = resolved
            needed = required_role(method, route_path)
            refusal = _refusal(actor, needed, method, route_path)

        context = _RequestContext(actor=actor)
        status = {"code": 500}

        async def send_and_note_status(message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        reset_token = _REQUEST.set(context)
        try:
            if refusal is not None:
                await refusal(scope, receive, send_and_note_status)
            else:
                await self.app(scope, receive, send_and_note_status)
        finally:
            _REQUEST.reset(reset_token)
            if method not in SAFE_METHODS:
                await run_in_threadpool(
                    _write_audit,
                    application,
                    actor_id=actor.actor_id,
                    actor_role=actor.role,
                    authenticated=actor.authenticated,
                    method=method,
                    route=route_path,
                    path=scope.get("path", ""),
                    target_id=_target_of(path_params, context.created_ids),
                    created_ids=context.created_ids,
                    status_code=status["code"],
                    outcome=_outcome(status["code"]),
                )


def _refusal(actor: Actor, needed: str | None, method: str, route_path: str | None):
    if needed is None:
        return None
    if not actor.authenticated:
        return JSONResponse(
            {
                "detail": {
                    "error": "not_signed_in",
                    "message": (
                        "Sign in: this server has auth enabled, and the request carried no "
                        "valid API token (Authorization: Bearer <token>)."
                    ),
                }
            },
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if needed == UNMAPPED:
        return JSONResponse(
            {
                "detail": {
                    "error": "route_not_in_role_table",
                    "message": (
                        f"{method} {route_path} has no entry in the role table, so it is refused "
                        "rather than allowed."
                    ),
                }
            },
            status_code=403,
        )
    if not actor.can(needed):
        return JSONResponse(
            {
                "detail": {
                    "error": "role_not_permitted",
                    "message": (
                        f"Your role '{actor.role}' cannot do this: {method} {route_path} "
                        f"needs '{needed}'."
                    ),
                    "required_role": needed,
                    "your_role": actor.role,
                }
            },
            status_code=403,
        )
    return None
