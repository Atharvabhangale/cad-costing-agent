"""Opt-in access control: users, roles, estimate approval, the audit log.

The most important property is the first section: with auth off (the default)
the application answers exactly as it did before, byte for byte. The rest of
the suite running unchanged is the broad proof; the comparison here is the
narrow one, made against the same app with the access middleware removed.
"""

import ast
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cad_costing_agent import approvals, auth, credentials
from cad_costing_agent.approvals import ApprovalEvent, ApprovalRefused, derive_state
from cad_costing_agent.db import _alembic_config, run_migrations
from cad_costing_agent.db_models import AuditEventRow, UserRow
from cad_costing_agent.main import app
from cad_costing_agent.repository import (
    ApprovalEventRepository,
    AuditRepository,
    EstimateRepository,
    UserRepository,
)

client = TestClient(app)
PACKAGE = Path(auth.__file__).resolve().parent
STATIC = PACKAGE / "static"

PART_REQUEST = {
    "part": {"part_id": "P100", "part_name": "Flange", "material": "Steel", "mass_kg": 2.5,
             "quantity": 4},
    "operation_requirements": [{
        "operation_id": "OP1", "operation_name": "Milling", "process_type": "Milling",
        "setup_time_minutes": 30.0, "cycle_time_minutes": 3.0, "quantity": 4,
    }],
    "rate_set_id": "poc-default",
}


def code_without_prose(path: Path) -> str:
    """Source with comments and docstrings removed, so a grep for a forbidden
    name cannot match the paragraph explaining that the name is absent."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    return ast.unparse(tree)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def create_estimate(headers: dict | None = None) -> str:
    response = client.post("/cost/part", json=PART_REQUEST, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()["estimate_id"]


def audit_rows(session_factory) -> list[AuditEventRow]:
    with session_factory() as session:
        return list(session.execute(select(AuditEventRow).order_by(AuditEventRow.id)).scalars())


@contextmanager
def without_access_middleware():
    saved = list(app.user_middleware)
    app.user_middleware[:] = [m for m in saved if m.cls is not auth.AccessMiddleware]
    app.middleware_stack = None
    try:
        yield TestClient(app)
    finally:
        app.user_middleware[:] = saved
        app.middleware_stack = None


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setenv(auth.AUTH_ENABLED_ENV_VAR, "1")


@pytest.fixture
def tokens(isolated_database):
    """One user per role, plus a second reviewer, created through the repository."""
    made = {}
    with isolated_database() as session:
        repository = UserRepository(session)
        for key, role in (("estimator", "estimator"), ("reviewer", "reviewer"),
                          ("reviewer2", "reviewer"), ("admin", "admin")):
            user, token = repository.create(name=key.title(), role=role, created_by="test")
            made[key] = token
            made[key + "_id"] = user.user_id
        session.commit()
    return made


# ============================================================ auth off


def test_auth_is_off_by_default(monkeypatch):
    monkeypatch.delenv(auth.AUTH_ENABLED_ENV_VAR, raising=False)
    assert auth.auth_enabled() is False
    for value in ("0", "false", "No", "off", ""):
        monkeypatch.setenv(auth.AUTH_ENABLED_ENV_VAR, value)
        assert auth.auth_enabled() is False
    for value in ("1", "true", "ON", "yes", "enabled-typo"):
        monkeypatch.setenv(auth.AUTH_ENABLED_ENV_VAR, value)
        assert auth.auth_enabled() is True  # unrecognised fails closed


def test_with_auth_off_responses_are_byte_identical_to_the_app_without_the_middleware():
    estimate_id = create_estimate()
    requests = [
        ("GET", "/health", {}),
        ("GET", "/rates", {}),
        ("GET", "/cad/formats", {}),
        ("GET", "/process-routes", {}),
        ("GET", "/time-profiles", {}),
        ("GET", f"/estimates/{estimate_id}", {}),
        ("GET", f"/estimates/{estimate_id}/quotation", {}),
        ("GET", f"/estimates/{estimate_id}/cost-drivers", {}),
        ("GET", "/estimates/does-not-exist", {}),
        ("GET", "/no-such-route", {}),
        ("POST", "/cost/part", {"json": {"part": {"part_id": "x"}}}),  # 422
        ("POST", "/rates/validate", {"json": {}}),
        ("POST", "/cost/material", {"json": {"material": "Steel", "mass_kg": 1.0,
                                             "rate_per_kg": 2.0}}),
        ("GET", "/openapi.json", {}),
    ]

    def capture(c):
        out = []
        for method, path, kwargs in requests:
            response = c.request(method, path, **kwargs)
            headers = {k: v for k, v in response.headers.items() if k != "date"}
            out.append((response.status_code, headers, response.content))
        return out

    with_middleware = capture(client)
    with without_access_middleware() as bare:
        without = capture(bare)
    assert with_middleware == without


def test_a_new_estimate_is_byte_identical_apart_from_its_own_id_and_time():
    def normalised(c):
        body = c.post("/cost/part", json=PART_REQUEST).content.decode()
        data = json.loads(body)
        return body.replace(data["estimate_id"], "ID").replace(data["created_at"], "T") \
            if "created_at" in data else body.replace(data["estimate_id"], "ID")

    first = normalised(client)
    with without_access_middleware() as bare:
        second = normalised(bare)
    assert first == second


def test_with_auth_off_every_mutating_request_is_audited_once_as_local(isolated_database):
    before = len(audit_rows(isolated_database))
    estimate_id = create_estimate()
    client.get(f"/estimates/{estimate_id}")
    client.get("/estimates")
    rows = audit_rows(isolated_database)[before:]
    assert len(rows) == 1
    row = rows[0]
    assert (row.actor_id, row.authenticated, row.method, row.route) == (
        "local", False, "POST", "/cost/part")
    assert row.status_code == 200 and row.outcome == "succeeded"
    assert row.target_id == estimate_id and row.created_ids == [estimate_id]


def test_with_auth_off_the_approval_flow_works_and_is_recorded_as_local():
    estimate_id = create_estimate()
    assert client.get(f"/estimates/{estimate_id}/approval").json()["state"] == "draft"
    submitted = client.post(f"/estimates/{estimate_id}/approval/submit", json={}).json()
    assert submitted["state"] == "submitted"
    approved = client.post(f"/estimates/{estimate_id}/approval/approve", json={})
    assert approved.status_code == 200
    body = approved.json()
    assert body["state"] == "approved"
    assert body["separation_of_duties_enforced"] is False
    assert body["approved_with_authentication"] is False
    assert {event["actor_id"] for event in body["events"]} == {"local"}
    assert body["created_by"] == "local"
    assert "Auth off" in body["note"]


def test_with_auth_off_a_quotation_is_issued_without_any_approval():
    estimate_id = create_estimate()
    response = client.post(f"/estimates/{estimate_id}/quotation", json={})
    assert response.status_code == 200, response.text


def test_auth_status_says_auth_is_off_and_actions_are_local():
    body = client.get("/auth/status").json()
    assert body["auth_enabled"] is False
    assert body["actor"]["actor_id"] == "local"
    assert body["note"] == "Auth off — actions recorded as local."


# ======================================================= the route table


def test_every_non_get_route_has_a_role_and_every_entry_is_a_real_route():
    declared = set()
    unmapped = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            declared.add((method, route.path))
            if method not in auth.SAFE_METHODS and (method, route.path) not in auth.ROUTE_ROLES:
                unmapped.append((method, route.path))
    assert unmapped == []
    stale = [key for key in auth.ROUTE_ROLES if key not in declared]
    assert stale == []
    assert set(auth.ROUTE_ROLES.values()) <= set(credentials.ROLES)


def test_an_unmapped_mutating_route_is_refused_not_allowed():
    assert auth.required_role("POST", "/a/route/nobody/mapped") == auth.UNMAPPED
    assert auth.required_role("GET", "/estimates") == auth.AUTHENTICATED
    assert auth.required_role("GET", "/health") is None


def test_the_middleware_is_registered_once_and_only_by_main():
    registrations = [m for m in app.user_middleware if m.cls is auth.AccessMiddleware]
    assert len(registrations) == 1
    for path in PACKAGE.rglob("*.py"):
        code = code_without_prose(path)
        if "add_middleware(AccessMiddleware)" in code:
            assert path.name == "main.py"
        # The audit is written in one place.
        if "AuditRepository(session).record" in code:
            assert path.name == "auth.py", path.name


# ============================================================== auth on


def test_without_a_token_a_request_is_refused_401(auth_on, tokens):
    response = client.get("/estimates")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"]["error"] == "not_signed_in"
    assert client.post("/cost/part", json=PART_REQUEST).status_code == 401
    assert client.get("/estimates", headers=bearer("cca_0123456789abcdef_wrong")).status_code == 401
    assert client.get("/estimates", headers=bearer("garbage")).status_code == 401


def test_public_routes_need_no_token(auth_on, tokens):
    assert client.get("/health").status_code == 200
    assert client.get("/ui/").status_code == 200
    status = client.get("/auth/status").json()
    assert status["auth_enabled"] is True and status["signed_in"] is False
    signed = client.get("/auth/status", headers=bearer(tokens["reviewer"])).json()
    assert signed["signed_in"] is True and signed["actor"]["role"] == "reviewer"


def test_each_role_can_do_what_the_table_grants_and_nothing_more(auth_on, tokens):
    estimator, reviewer, admin = (bearer(tokens[k]) for k in ("estimator", "reviewer", "admin"))
    assert client.get("/estimates", headers=estimator).status_code == 200
    create_estimate(estimator)
    create_estimate(reviewer)  # a reviewer can estimate too
    refused = client.post("/rates/validate", json={}, headers=estimator)
    assert refused.status_code == 403
    assert refused.json()["detail"]["required_role"] == "admin"
    assert client.post("/rates/validate", json={}, headers=reviewer).status_code == 403
    assert client.post("/rates/validate", json={}, headers=admin).status_code != 403
    assert client.get("/audit", headers=estimator).status_code == 403
    assert client.get("/audit", headers=admin).status_code == 200
    assert client.post("/auth/users", json={"name": "X", "role": "estimator"},
                       headers=reviewer).status_code == 403
    created = client.post("/auth/users", json={"name": "New", "role": "estimator"}, headers=admin)
    assert created.status_code == 201
    new_token = created.json()["token"]
    assert client.get("/estimates", headers=bearer(new_token)).status_code == 200


def test_an_estimator_cannot_approve(auth_on, tokens):
    estimate_id = create_estimate(bearer(tokens["estimator"]))
    client.post(f"/estimates/{estimate_id}/approval/submit", json={},
                headers=bearer(tokens["estimator"]))
    response = client.post(f"/estimates/{estimate_id}/approval/approve", json={},
                           headers=bearer(tokens["estimator"]))
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "role_not_permitted"


def test_submit_approve_issue(auth_on, tokens):
    estimator, reviewer = bearer(tokens["estimator"]), bearer(tokens["reviewer"])
    estimate_id = create_estimate(estimator)
    figures_before = client.get(f"/estimates/{estimate_id}", headers=estimator).content

    refused = client.post(f"/estimates/{estimate_id}/quotation", json={}, headers=estimator)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "estimate_not_approved"

    assert client.post(f"/estimates/{estimate_id}/approval/submit", json={"comment": "ready"},
                       headers=estimator).json()["state"] == "submitted"
    assert client.post(f"/estimates/{estimate_id}/quotation", json={},
                       headers=estimator).status_code == 409
    approved = client.post(f"/estimates/{estimate_id}/approval/approve", json={},
                           headers=reviewer).json()
    assert approved["state"] == "approved" and approved["approved_with_authentication"] is True
    assert approved["created_by"] == tokens["estimator_id"]

    issued = client.post(f"/estimates/{estimate_id}/quotation", json={}, headers=estimator)
    assert issued.status_code == 200, issued.text
    # Approval changes no figure.
    assert client.get(f"/estimates/{estimate_id}", headers=estimator).content == figures_before


def test_a_reviewer_cannot_approve_their_own_submission(auth_on, tokens):
    reviewer = bearer(tokens["reviewer"])
    estimate_id = create_estimate(bearer(tokens["estimator"]))
    client.post(f"/estimates/{estimate_id}/approval/submit", json={}, headers=reviewer)
    response = client.post(f"/estimates/{estimate_id}/approval/approve", json={}, headers=reviewer)
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "separation_of_duties"
    assert client.get(f"/estimates/{estimate_id}/approval",
                      headers=reviewer).json()["state"] == "submitted"
    other = client.post(f"/estimates/{estimate_id}/approval/approve", json={},
                        headers=bearer(tokens["reviewer2"]))
    assert other.status_code == 200


def test_a_reviewer_cannot_approve_an_estimate_they_created(auth_on, tokens):
    estimate_id = create_estimate(bearer(tokens["reviewer"]))
    client.post(f"/estimates/{estimate_id}/approval/submit", json={},
                headers=bearer(tokens["estimator"]))
    response = client.post(f"/estimates/{estimate_id}/approval/approve", json={},
                           headers=bearer(tokens["reviewer"]))
    assert response.status_code == 403
    assert "created" in response.json()["detail"]["message"]


def test_an_admin_is_bound_by_separation_of_duties_too(auth_on, tokens):
    admin = bearer(tokens["admin"])
    estimate_id = create_estimate(admin)
    client.post(f"/estimates/{estimate_id}/approval/submit", json={}, headers=admin)
    assert client.post(f"/estimates/{estimate_id}/approval/approve", json={},
                       headers=admin).status_code == 403


def test_rules_of_the_state_machine(auth_on, tokens):
    estimator, reviewer = bearer(tokens["estimator"]), bearer(tokens["reviewer"])
    estimate_id = create_estimate(estimator)
    approve = client.post(f"/estimates/{estimate_id}/approval/approve", json={}, headers=reviewer)
    assert approve.status_code == 409 and approve.json()["detail"]["error"] == "not_allowed_in_state"
    client.post(f"/estimates/{estimate_id}/approval/submit", json={}, headers=estimator)
    no_reason = client.post(f"/estimates/{estimate_id}/approval/reject", json={}, headers=reviewer)
    assert no_reason.status_code == 409 and no_reason.json()["detail"]["error"] == "comment_required"
    withdraw_by_other = client.post(f"/estimates/{estimate_id}/approval/withdraw", json={},
                                    headers=bearer(tokens["reviewer2"]))
    assert withdraw_by_other.status_code == 403
    rejected = client.post(f"/estimates/{estimate_id}/approval/reject",
                           json={"comment": "rate set out of date"}, headers=reviewer).json()
    assert rejected["state"] == "rejected"
    resubmitted = client.post(f"/estimates/{estimate_id}/approval/submit", json={},
                              headers=estimator).json()
    assert [e["kind"] for e in resubmitted["events"]] == ["submitted", "rejected", "submitted"]
    assert [e["sequence"] for e in resubmitted["events"]] == [1, 2, 3]
    assert client.post("/estimates/nope/approval/submit", json={},
                       headers=estimator).status_code == 404


def test_an_approval_recorded_with_auth_off_does_not_open_the_gate(monkeypatch, tokens):
    estimate_id = create_estimate()
    client.post(f"/estimates/{estimate_id}/approval/submit", json={})
    client.post(f"/estimates/{estimate_id}/approval/approve", json={})
    monkeypatch.setenv(auth.AUTH_ENABLED_ENV_VAR, "1")
    estimator = bearer(tokens["estimator"])
    refused = client.post(f"/estimates/{estimate_id}/quotation", json={}, headers=estimator)
    assert refused.status_code == 409
    assert "auth was off" in refused.json()["detail"]["message"]
    # The way out is the ordinary one: withdraw, submit, approve.
    assert client.post(f"/estimates/{estimate_id}/approval/withdraw", json={},
                       headers=bearer(tokens["admin"])).json()["state"] == "withdrawn"
    client.post(f"/estimates/{estimate_id}/approval/submit", json={}, headers=estimator)
    client.post(f"/estimates/{estimate_id}/approval/approve", json={},
                headers=bearer(tokens["reviewer"]))
    assert client.post(f"/estimates/{estimate_id}/quotation", json={},
                       headers=estimator).status_code == 200


def test_a_deactivated_user_is_signed_out_and_the_last_admin_stays(auth_on, tokens):
    admin = bearer(tokens["admin"])
    response = client.post(f"/auth/users/{tokens['estimator_id']}/deactivate", headers=admin)
    assert response.status_code == 200 and response.json()["deactivated_at"]
    assert client.get("/estimates", headers=bearer(tokens["estimator"])).status_code == 401
    last = client.post(f"/auth/users/{tokens['admin_id']}/deactivate", headers=admin)
    assert last.status_code == 409


# ================================================================ audit


def test_each_mutating_request_is_audited_exactly_once_with_its_outcome(
        auth_on, tokens, isolated_database):
    before = len(audit_rows(isolated_database))
    client.post("/cost/part", json=PART_REQUEST)  # 401
    client.post("/rates/validate", json={}, headers=bearer(tokens["estimator"]))  # 403
    estimate_id = create_estimate(bearer(tokens["estimator"]))  # 200
    client.post(f"/estimates/{estimate_id}/approval/submit", json={},
                headers=bearer(tokens["estimator"]))
    client.get("/estimates", headers=bearer(tokens["estimator"]))  # not audited
    rows = audit_rows(isolated_database)[before:]
    assert [(r.actor_id, r.status_code, r.outcome) for r in rows] == [
        ("anonymous", 401, "refused"),
        (tokens["estimator_id"], 403, "refused"),
        (tokens["estimator_id"], 200, "succeeded"),
        (tokens["estimator_id"], 200, "succeeded"),
    ]
    assert rows[3].route == "/estimates/{estimate_id}/approval/submit"
    assert rows[3].target_id == estimate_id

    listed = client.get("/audit", params={"target_id": estimate_id},
                        headers=bearer(tokens["admin"])).json()
    assert [item["route"] for item in listed["items"]] == [
        "/estimates/{estimate_id}/approval/submit", "/cost/part"]


def test_no_token_or_secret_reaches_any_table(auth_on, tokens, isolated_database):
    created = client.post("/auth/users", json={"name": "Temp", "role": "reviewer"},
                          headers=bearer(tokens["admin"])).json()
    token = created["token"]
    client.post("/cost/part", json=PART_REQUEST, headers=bearer(token))
    _, token_id, secret = token.split("_", 2)
    everything = []
    with isolated_database() as session:
        for table in inspect(session.get_bind()).get_table_names():
            everything.extend(str(row) for row in session.execute(text(f"select * from {table}")))
    dump = "\n".join(everything)
    every_token = [created["token"]] + [tokens[k] for k in ("estimator", "reviewer", "admin")]
    for value in every_token:
        assert value not in dump
        assert value.split("_", 2)[2] not in dump
    assert token_id in dump  # the public lookup key is stored, and only that


def test_the_audit_model_has_no_body_query_or_header_column():
    columns = set(AuditEventRow.__table__.columns.keys())
    for forbidden in ("body", "query", "headers", "authorization", "token", "payload"):
        assert not any(forbidden in column for column in columns), forbidden


# ============================================================ immutability


@pytest.mark.parametrize("repository", [ApprovalEventRepository, AuditRepository])
def test_events_and_audit_rows_cannot_be_updated_or_deleted(repository):
    public = {name for name in vars(repository) if not name.startswith("_")}
    for name in public:
        for verb in ("update", "delete", "remove", "edit", "set_", "clear", "purge"):
            assert verb not in name, (repository.__name__, name)
    code = code_without_prose(PACKAGE / "repository.py")
    for forbidden in ("delete(ApprovalEventRow", "delete(AuditEventRow", "update(ApprovalEventRow",
                      "update(AuditEventRow"):
        assert forbidden not in code


def test_users_are_deactivated_never_deleted():
    public = {name for name in vars(UserRepository) if not name.startswith("_")}
    assert not {name for name in public if "delete" in name or "remove" in name}


def test_state_is_derived_from_events_and_never_stored():
    from cad_costing_agent.db_models import ApprovalEventRow, EstimateAttributionRow, EstimateRow

    for model in (EstimateRow, ApprovalEventRow, EstimateAttributionRow):
        assert "state" not in model.__table__.columns.keys()
        assert "status" not in model.__table__.columns.keys()
    assert "approv" not in " ".join(EstimateRow.__table__.columns.keys())


def test_derive_state_replays_and_refuses_an_impossible_history():
    def event(sequence, kind):
        return ApprovalEvent(event_id=str(sequence), estimate_id="e", sequence=sequence, kind=kind,
                             actor_id="a", actor_name="a", authenticated=True,
                             recorded_at="2026-11-13T00:00:00")

    assert derive_state([]) == "draft"
    assert derive_state([event(2, "approved"), event(1, "submitted")]) == "approved"
    with pytest.raises(ApprovalRefused):
        derive_state([event(1, "approved")])


def test_the_approval_module_is_pure():
    tree = ast.parse((PACKAGE / "approvals.py").read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    assert not any(name and name.startswith("cad_costing_agent") for name in imported)
    code = code_without_prose(PACKAGE / "approvals.py")
    for forbidden in ("datetime.now", "session", "total_", "cost"):
        assert forbidden not in code, forbidden


def test_the_quotation_gate_lives_in_main_and_quotation_py_is_untouched():
    code = code_without_prose(PACKAGE / "quotation.py")
    # A precise token, not "auth": the module legitimately says "author".
    for forbidden in ("approv", "auth_enabled", "cad_costing_agent.auth"):
        assert forbidden not in code.lower()
    main_code = code_without_prose(PACKAGE / "main.py")
    assert main_code.count("_require_approval_to_issue(session, estimate_id)") == 1


# ============================================================== tokens


def test_a_token_is_stored_only_as_a_salted_pbkdf2_hash(db_session):
    user, token = UserRepository(db_session).create(name="T", role="estimator", created_by=None)
    db_session.commit()
    row = db_session.execute(select(UserRow).where(UserRow.user_id == user.user_id)).scalar_one()
    prefix, token_id, secret = token.split("_", 2)
    assert prefix == "cca" and row.token_id == token_id
    expected = hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(row.token_salt),
                                   row.kdf_iterations).hex()
    assert row.token_hash == expected
    assert secret not in (row.token_hash, row.token_salt)
    assert UserRepository(db_session).by_token(token).user_id == user.user_id
    assert UserRepository(db_session).by_token(token[:-1] + "x") is None
    # A second user with the same secret would still hash differently.
    other, _ = UserRepository(db_session).create(name="U", role="estimator", created_by=None)
    other_row = db_session.execute(select(UserRow).where(UserRow.user_id == other.user_id)).scalar_one()
    assert other_row.token_salt != row.token_salt


def test_the_bootstrap_script_creates_one_admin_and_never_a_default(tmp_path, monkeypatch, capsys):
    import importlib.util

    from cad_costing_agent import db

    monkeypatch.setenv(db.DATABASE_URL_ENV_VAR, f"sqlite:///{tmp_path / 'boot.db'}")
    db.reset_engine()
    script = Path(__file__).resolve().parents[1] / "scripts" / "create_admin.py"
    spec = importlib.util.spec_from_file_location("create_admin", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--name", "First Admin"]) == 0
    printed = capsys.readouterr().out
    assert "cca_" in printed
    assert module.main(["--name", "Second"]) == 1  # an admin exists: use the API
    db.reset_engine()
    source = code_without_prose(script)
    for forbidden in ("password", "default_token", "changeme"):
        assert forbidden not in source.lower()


# ============================================================ migration


def _engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False},
                         poolclass=StaticPool)


def test_a_fresh_database_gets_the_access_tables():
    engine = _engine()
    assert run_migrations(engine) == "created"
    tables = set(inspect(engine).get_table_names())
    assert {"users", "estimate_attributions", "approval_events", "audit_events"} <= tables


def test_an_existing_0008_database_keeps_its_estimates_which_read_as_draft_and_unattributed():
    from alembic import command

    engine = _engine()
    with engine.begin() as connection:
        command.upgrade(_alembic_config(connection), "0008")
    with sessionmaker(bind=engine)() as session:
        estimate_id = EstimateRepository(session).save(
            source="cost/part", part_id="OLD", part_name="Flange", material="Steel", quantity=2,
            rate_set=None, currency="INR", total_material_cost=1.0, total_operation_cost=2.0,
            total_additional_cost=0.0, total_part_cost=3.0, request_snapshot={"before": "0011"},
            result_snapshot={},
        )
        session.commit()
    with engine.connect() as connection:
        before = connection.execute(text("select * from estimates")).fetchall()

    assert run_migrations(engine) == "upgraded"

    with engine.connect() as connection:
        assert connection.execute(text("select * from estimates")).fetchall() == before
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == "0011"
    with sessionmaker(bind=engine)() as session:
        repository = ApprovalEventRepository(session)
        assert repository.estimate_creator(estimate_id) is None
        status = approvals.status_of(estimate_id, repository.for_estimate(estimate_id),
                                     created_by=None, auth_enabled=False)
        assert status.state == "draft"


# ===================================================================== UI


def test_the_ui_scripts_declare_no_globals_and_keep_the_token_in_session_storage():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    order = [s for s in __import__("re").findall(r'<script src="([\w.]+\.js)"', html)]
    assert order.index("auth.js") < order.index("app.js") < order.index("approval.js")
    auth_js = (STATIC / "auth.js").read_text(encoding="utf-8")
    approval_js = (STATIC / "approval.js").read_text(encoding="utf-8")
    assert "sessionStorage" in auth_js and "localStorage" not in auth_js
    assert "try" in auth_js and "catch" in auth_js
    assert "Auth off — actions recorded as local" in auth_js
    for source in (auth_js, approval_js):
        assert not __import__("re").search(r"^(?:async )?function ", source, __import__("re").M)
    assert 'id="tab-audit"' in html and 'data-tab="audit"' in html
