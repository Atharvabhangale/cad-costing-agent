"""Versioned, user-configurable machining-time assumptions.

The point of this layer is that a predicted time can be *explained* — and stay
explained. So the tests are mostly about identity and history rather than
arithmetic:

- a published version is immutable, and republishing one is a conflict;
- changing an assumption means a new version, which changes *future* estimates
  and leaves earlier ones exactly as they were;
- every predicted time records which profile and version produced it, and the
  parameter values that entered the calculation;
- the correction data carries the profile too, because "the system said 20 and
  the engineer said 32" is uninterpretable without knowing which feeds and
  speeds the 20 came from.

And throughout: a time profile is not a rate set. Duration and money are
separate, separately versioned, and share no fields.
"""

from __future__ import annotations

from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.cad_geometry import read_step_shape
from cad_costing_agent.features import recognize_features
from cad_costing_agent.main import STATIC_PATH, app
from cad_costing_agent.manufacturing_plan import build_plan
from cad_costing_agent.operation_feedback import feedback_from_plan
from cad_costing_agent.operation_suggestions import suggest_operations
from cad_costing_agent.operation_time import TimeAssumptions, TimeStatus, estimate_operation_time
from cad_costing_agent.time_profiles import (
    BASELINE_PROFILE_ID,
    SEED_TIME_PROFILES,
    TimeProfile,
    advisory_notes,
)
from tests import step_fixtures

client = TestClient(app)


# --- helpers -----------------------------------------------------------------


@lru_cache(maxsize=None)
def step(name: str = "plate_with_four_holes") -> bytes:
    return getattr(step_fixtures, name)()


@lru_cache(maxsize=None)
def _suggestions(name: str = "plate_with_four_holes"):
    recognition = recognize_features(read_step_shape(step(name)), filename=f"{name}.step")
    return recognition.units, tuple(suggest_operations(recognition).suggestions)


def plan_under(profile: TimeProfile, name: str = "plate_with_four_holes"):
    units, suggestions = _suggestions(name)
    return build_plan(list(suggestions), units=units, profile=profile, source_filename=name)


def publish(body: dict):
    return client.post("/time-profiles", json=body)


def shop_profile(version: str = "1.0.0", **assumptions) -> dict:
    return {
        "time_profile_id": "pune-shop-time",
        "name": "Pune Shop Machining",
        "version": version,
        "description": "Real shop feeds and speeds.",
        "is_illustrative": False,
        "assumptions": {"is_illustrative": False, **assumptions},
    }


def drilling_of(plan):
    return next(o for o in plan.ordered if o.system_suggested_operation == "drilling")


# --- the catalog -------------------------------------------------------------


def test_the_illustrative_baseline_is_seeded():
    profiles = client.get("/time-profiles").json()

    identities = {(p["time_profile_id"], p["version"]) for p in profiles}
    assert identities == {(p.time_profile_id, p.version) for p in SEED_TIME_PROFILES}
    baseline = next(p for p in profiles if p["time_profile_id"] == BASELINE_PROFILE_ID)
    assert baseline["is_illustrative"] is True


def test_a_time_profile_has_no_currency():
    """Duration is not money, and the model must not blur that."""
    from cad_costing_agent.rate_sets import RateSet

    assert "currency" not in TimeProfile.model_fields
    assert "currency" not in TimeAssumptions.model_fields
    # And the two models overlap only on the fields every versioned thing has.
    shared = set(TimeProfile.model_fields) & set(RateSet.model_fields)
    assert shared == {"name", "version", "description", "is_illustrative"}


def test_creating_a_profile_stores_exactly_what_was_sent():
    response = publish(shop_profile(drilling_feed_mm_per_rev=0.22))

    assert response.status_code == 201
    body = response.json()
    assert body["time_profile_id"] == "pune-shop-time"
    assert body["assumptions"]["drilling_feed_mm_per_rev"] == 0.22
    assert body["is_illustrative"] is False

    fetched = client.get("/time-profiles/pune-shop-time").json()
    assert fetched == body


def test_unspecified_parameters_fall_back_to_the_models_defaults():
    """A blank field must take the documented default, never zero."""
    response = publish(shop_profile(drilling_feed_mm_per_rev=0.22))

    assumptions = response.json()["assumptions"]
    assert assumptions["milling_teeth"] == TimeAssumptions().milling_teeth
    assert assumptions["max_spindle_rpm"] == TimeAssumptions().max_spindle_rpm


# --- immutability and versioning ---------------------------------------------


def test_republishing_a_published_version_is_a_conflict():
    publish(shop_profile())

    again = publish(shop_profile())

    assert again.status_code == 409
    assert "immutable" in again.json()["detail"]


def test_a_new_version_is_published_rather_than_editing_the_old_one():
    publish(shop_profile("1.0.0", drilling_feed_mm_per_rev=0.15))

    second = client.post(
        "/time-profiles/pune-shop-time/versions",
        json={
            "name": "Pune Shop Machining",
            "version": "2.0.0",
            "description": "Slower drilling feed.",
            "is_illustrative": False,
            "assumptions": {"is_illustrative": False, "drilling_feed_mm_per_rev": 0.05},
        },
    )

    assert second.status_code == 201
    v1 = client.get("/time-profiles/pune-shop-time?version=1.0.0").json()
    v2 = client.get("/time-profiles/pune-shop-time?version=2.0.0").json()
    assert v1["assumptions"]["drilling_feed_mm_per_rev"] == 0.15  # untouched
    assert v2["assumptions"]["drilling_feed_mm_per_rev"] == 0.05


def test_omitting_the_version_resolves_the_latest_numerically():
    for version in ["1.0.0", "2.0.0", "10.0.0"]:
        publish(shop_profile(version))

    assert client.get("/time-profiles/pune-shop-time").json()["version"] == "10.0.0"


def test_an_unknown_profile_or_version_is_a_404():
    publish(shop_profile("1.0.0"))

    assert client.get("/time-profiles/no-such-profile").status_code == 404
    assert client.get("/time-profiles/pune-shop-time?version=9.9.9").status_code == 404


def test_there_is_no_update_endpoint():
    """Immutability is structural: the repository has no update method."""
    from cad_costing_agent.repository import TimeProfileRepository

    assert not hasattr(TimeProfileRepository, "update")
    assert not hasattr(TimeProfileRepository, "delete")
    publish(shop_profile())
    assert client.put("/time-profiles/pune-shop-time", json={}).status_code in (404, 405)


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "assumptions",
    [
        {"drilling_feed_mm_per_rev": 0.0},
        {"drilling_feed_mm_per_rev": -0.1},
        {"drilling_feed_mm_per_rev": 800.0},  # a slipped decimal point
        {"milling_teeth": 0},
        {"milling_teeth": 5000},
        {"milling_stepover_ratio": 0.0},
        {"milling_stepover_ratio": 1.5},
        {"turning_depth_of_cut_mm": 0.0},
        {"drilling_cutting_speed_m_per_min": 0.0},
        {"drilling_approach_mm": -1.0},
        {"max_spindle_rpm": 0.0},
    ],
)
def test_invalid_machining_parameters_are_refused(assumptions):
    response = publish(shop_profile(**assumptions))

    assert response.status_code == 422


def test_a_spindle_range_that_would_pin_every_speed_is_refused():
    response = publish(shop_profile(min_spindle_rpm=9000.0, max_spindle_rpm=6000.0))

    assert response.status_code == 422
    assert "min_spindle_rpm" in str(response.json())


def test_the_spindle_clamp_is_kept_because_it_is_part_of_the_model():
    """The one place clamping is right: a machine really does have limits."""
    estimate = estimate_operation_time(
        operation_type="drilling",
        dimensions={"diameter_mm": 0.02, "depth_mm": 1.0},
        assumptions=TimeAssumptions(max_spindle_rpm=6000.0),
    )

    assert estimate.estimated
    assert estimate.parameters["resolved_spindle_rpm"] == 6000.0


def test_validate_reports_a_duplicate_version_without_storing_anything():
    publish(shop_profile("1.0.0"))
    before = len(client.get("/time-profiles").json())

    verdict = client.post("/time-profiles/validate", json=shop_profile("1.0.0")).json()

    assert verdict["valid"] is False
    assert verdict["version_already_exists"] is True
    assert any("never be changed" in message for message in verdict["messages"])
    assert len(client.get("/time-profiles").json()) == before


def test_validate_accepts_a_publishable_profile():
    verdict = client.post("/time-profiles/validate", json=shop_profile("3.0.0")).json()

    assert verdict["valid"] is True
    assert verdict["is_new_profile"] is True


def test_unusual_but_legal_parameters_are_advised_not_refused():
    """The parameters are the user's to choose; the app says so and moves on."""
    body = shop_profile("1.0.0", drilling_cutting_speed_m_per_min=900.0)

    verdict = client.post("/time-profiles/validate", json=body).json()

    assert verdict["valid"] is True
    assert any("outside the usual" in message for message in verdict["messages"])
    assert publish(body).status_code == 201  # and it really does publish


def test_an_illustrative_profile_is_flagged_in_validation():
    body = {**shop_profile("1.0.0"), "is_illustrative": True}

    verdict = client.post("/time-profiles/validate", json=body).json()

    assert any("must not be quoted" in message for message in verdict["messages"])


def test_advisory_notes_are_pure_and_never_reject():
    profile = TimeProfile(
        time_profile_id="x", name="X", version="1.0.0",
        assumptions=TimeAssumptions(turning_stock_allowance_mm=100.0, turning_depth_of_cut_mm=0.1),
    )

    notes = advisory_notes(profile)

    assert any("turning passes" in note for note in notes)


# --- estimating under a profile ----------------------------------------------


def test_an_estimate_records_which_profile_and_version_produced_it():
    profile = TimeProfile.model_validate(publish(shop_profile()).json())

    operation = drilling_of(plan_under(profile))

    assert operation.system_time_profile_id == "pune-shop-time"
    assert operation.system_time_profile_version == "1.0.0"
    assert operation.system_time_profile_name == "Pune Shop Machining"


def test_an_estimate_records_the_parameter_values_it_actually_used():
    """Enough to answer 'why 2.37 minutes?' from the operation alone."""
    profile = TimeProfile.model_validate(
        publish(shop_profile(drilling_feed_mm_per_rev=0.25)).json()
    )

    parameters = drilling_of(plan_under(profile)).system_time_parameters

    assert parameters["drilling_feed_mm_per_rev"] == 0.25
    assert parameters["resolved_spindle_rpm"] > 0
    assert parameters["resolved_feed_rate_mm_per_min"] > 0
    # Only the parameters for this process, not the whole profile.
    assert not any(key.startswith("turning_") for key in parameters)


def test_changing_an_assumption_changes_the_predicted_time():
    fast = TimeProfile.model_validate(publish(shop_profile("1.0.0", drilling_feed_mm_per_rev=0.15)).json())
    slow = TimeProfile.model_validate(publish(shop_profile("2.0.0", drilling_feed_mm_per_rev=0.05)).json())

    under_v1 = drilling_of(plan_under(fast)).system_suggested_cycle_time
    under_v2 = drilling_of(plan_under(slow)).system_suggested_cycle_time

    # Three times the feed, three times the time. Times are reported to two
    # decimals, so at these magnitudes the ratio cannot be exact — the absolute
    # tolerance is the rounding, not slack in the model.
    assert under_v2 == pytest.approx(under_v1 * 3, abs=0.03)
    assert under_v2 > under_v1


def test_the_same_feature_and_version_always_give_the_same_time():
    profile = TimeProfile.model_validate(publish(shop_profile()).json())

    first = plan_under(profile).model_dump()
    second = plan_under(profile).model_dump()

    assert first == second


def test_estimating_with_no_profile_falls_back_to_the_baseline_and_says_so():
    operation = drilling_of(plan_under(None))

    assert operation.system_time_status is TimeStatus.ESTIMATED
    assert operation.system_time_profile_id is None  # nothing published produced it


def test_a_needs_user_input_result_still_names_the_profile():
    """Even a refusal should say which assumptions were in play."""
    profile = TimeProfile.model_validate(publish(shop_profile()).json())

    estimate = estimate_operation_time(
        operation_type="drilling", dimensions={}, profile=profile
    )

    assert estimate.time_status is TimeStatus.NEEDS_USER_INPUT
    assert estimate.time_profile_id == "pune-shop-time"
    assert estimate.cycle_time_minutes is None


def test_explicit_assumptions_win_and_then_no_profile_is_claimed():
    """A caller trying unpublished parameters must not have them attributed."""
    profile = TimeProfile.model_validate(publish(shop_profile(drilling_feed_mm_per_rev=0.15)).json())

    estimate = estimate_operation_time(
        operation_type="drilling",
        dimensions={"diameter_mm": 10.0, "depth_mm": 20.0},
        assumptions=TimeAssumptions(drilling_feed_mm_per_rev=0.5),
        profile=profile,
    )

    assert estimate.parameters["drilling_feed_mm_per_rev"] == 0.5
    assert estimate.time_profile_id is None


# --- the estimate-time endpoint ----------------------------------------------


def payload(units: str = "mm"):
    return {
        "units": units,
        "operations": [
            {
                "operation_id": "OP-001",
                "operation_type": "drilling",
                "dimensions": {"diameter_mm": 10.0, "depth_mm": 20.0},
            }
        ],
    }


def test_the_endpoint_estimates_under_a_named_profile():
    publish(shop_profile("1.0.0", drilling_feed_mm_per_rev=0.15))

    response = client.post(
        "/operations/estimate-time?time_profile_id=pune-shop-time", json=payload()
    )

    body = response.json()
    assert body["time_profile_id"] == "pune-shop-time"
    assert body["time_profile_version"] == "1.0.0"
    assert body["estimates"][0]["estimate"]["time_profile_version"] == "1.0.0"


def test_the_endpoint_can_pin_an_older_version():
    publish(shop_profile("1.0.0", drilling_feed_mm_per_rev=0.15))
    publish(shop_profile("2.0.0", drilling_feed_mm_per_rev=0.05))

    latest = client.post(
        "/operations/estimate-time?time_profile_id=pune-shop-time", json=payload()
    ).json()
    pinned = client.post(
        "/operations/estimate-time?time_profile_id=pune-shop-time&time_profile_version=1.0.0",
        json=payload(),
    ).json()

    assert latest["time_profile_version"] == "2.0.0"
    assert pinned["time_profile_version"] == "1.0.0"
    assert (
        pinned["estimates"][0]["estimate"]["cycle_time_minutes"]
        < latest["estimates"][0]["estimate"]["cycle_time_minutes"]
    )


def test_an_unknown_profile_on_the_endpoint_is_a_404():
    response = client.post("/operations/estimate-time?time_profile_id=nope", json=payload())

    assert response.status_code == 404


def test_the_endpoint_warns_when_explicit_assumptions_override_a_profile():
    publish(shop_profile())

    response = client.post(
        "/operations/estimate-time?time_profile_id=pune-shop-time",
        json={**payload(), "assumptions": {"drilling_feed_mm_per_rev": 0.4}},
    ).json()

    assert response["time_profile_id"] is None
    assert any("was not used" in warning for warning in response["warnings"])


# --- persistence and history -------------------------------------------------


FORM = {
    "material": "Steel",
    "source_unit": "mm",
    "part_id": "P-TIME",
    "part_name": "Plate",
    "quantity": "10",
    "rate_set_id": "poc-default",
}


def cost(plan):
    return client.post(
        "/cost/cad-part",
        files={"file": ("plate.step", step(), "application/octet-stream")},
        data={**FORM, "manufacturing_plan": plan.model_dump_json()},
    )


def test_an_estimate_snapshots_the_profile_its_times_were_made_under():
    profile = TimeProfile.model_validate(publish(shop_profile(drilling_feed_mm_per_rev=0.15)).json())
    plan = plan_under(profile)
    plan = plan.replacing(drilling_of(plan).accepted(use_estimated_time=True))

    estimate_id = cost(plan).json()["estimate_id"]
    snapshot = client.get(f"/estimates/{estimate_id}").json()["time_profile_snapshot"]

    assert snapshot is not None
    stored = snapshot["profiles"]["pune-shop-time@1.0.0"]
    assert stored["assumptions"]["drilling_feed_mm_per_rev"] == 0.15
    assert stored["version"] == "1.0.0"


def test_publishing_a_later_version_does_not_alter_a_historical_estimate():
    """The whole reason versions are immutable."""
    v1 = TimeProfile.model_validate(publish(shop_profile("1.0.0", drilling_feed_mm_per_rev=0.15)).json())
    plan = plan_under(v1)
    plan = plan.replacing(drilling_of(plan).accepted(use_estimated_time=True))
    estimate_id = cost(plan).json()["estimate_id"]
    before = client.get(f"/estimates/{estimate_id}").json()

    publish(shop_profile("2.0.0", drilling_feed_mm_per_rev=0.05))

    after = client.get(f"/estimates/{estimate_id}").json()
    assert after == before
    assert after["time_profile_snapshot"]["profiles"]["pune-shop-time@1.0.0"]["assumptions"][
        "drilling_feed_mm_per_rev"
    ] == 0.15


def test_an_estimate_without_a_profile_stores_no_snapshot():
    plan = plan_under(None)
    plan = plan.replacing(drilling_of(plan).accepted(use_estimated_time=True))

    estimate_id = cost(plan).json()["estimate_id"]

    assert client.get(f"/estimates/{estimate_id}").json()["time_profile_snapshot"] is None


def test_a_snapshot_survives_a_restart():
    profile = TimeProfile.model_validate(publish(shop_profile()).json())
    plan = plan_under(profile)
    estimate_id = cost(plan).json()["estimate_id"]

    reopened = TestClient(app)
    snapshot = reopened.get(f"/estimates/{estimate_id}").json()["time_profile_snapshot"]

    assert "pune-shop-time@1.0.0" in snapshot["profiles"]


# --- the plan and the feedback record ----------------------------------------


def test_the_plan_still_keeps_the_prediction_and_the_users_value_apart():
    profile = TimeProfile.model_validate(publish(shop_profile()).json())
    plan = plan_under(profile)
    original = drilling_of(plan)

    edited = original.with_times(setup_time_minutes=6.0, cycle_time_minutes=3.2)

    assert edited.system_suggested_cycle_time == original.system_suggested_cycle_time
    assert edited.user_cycle_time == 3.2
    assert edited.final_cycle_time == 3.2
    # And the profile behind the prediction is untouched by the correction.
    assert edited.system_time_profile_version == "1.0.0"


def test_a_user_override_still_wins_over_a_profiles_prediction():
    profile = TimeProfile.model_validate(publish(shop_profile()).json())
    plan = plan_under(profile)
    operation = drilling_of(plan)
    plan = plan.replacing(operation.with_times(setup_time_minutes=6.0, cycle_time_minutes=3.2))

    costed = cost(plan).json()["part_cost"]["operations"]

    assert [o["cycle_time_minutes"] for o in costed] == [3.2]
    assert operation.system_suggested_cycle_time != 3.2


def test_feedback_records_the_profile_behind_the_prediction():
    """Without it, 'the system said 20 and the engineer said 32' means nothing."""
    profile = TimeProfile.model_validate(publish(shop_profile(drilling_feed_mm_per_rev=0.15)).json())
    plan = plan_under(profile)
    operation = drilling_of(plan)
    plan = plan.replacing(operation.with_times(cycle_time_minutes=32.0))

    entry = next(f for f in feedback_from_plan(plan) if f.operation_id == operation.operation_id)

    assert entry.time_profile_id == "pune-shop-time"
    assert entry.time_profile_version == "1.0.0"
    assert entry.time_profile_name == "Pune Shop Machining"
    assert entry.time_parameters["drilling_feed_mm_per_rev"] == 0.15
    assert entry.suggested_cycle_time == operation.system_suggested_cycle_time
    assert entry.final_cycle_time == 32.0
    assert entry.cycle_time_changed is True


def test_the_persisted_feedback_carries_the_profile_version():
    profile = TimeProfile.model_validate(publish(shop_profile()).json())
    plan = plan_under(profile)
    plan = plan.replacing(drilling_of(plan).with_times(cycle_time_minutes=9.9))

    estimate_id = cost(plan).json()["estimate_id"]
    feedback = client.get(f"/estimates/{estimate_id}").json()["plan_snapshot"]["feedback"]

    changed = next(f for f in feedback if f["cycle_time_changed"])
    assert changed["time_profile_id"] == "pune-shop-time"
    assert changed["time_profile_version"] == "1.0.0"
    assert changed["time_parameters"]


def test_nothing_is_trained_from_a_profile_or_its_corrections():
    from pathlib import Path

    import cad_costing_agent.time_profiles as module

    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ["sklearn", "torch", "fit(", "train(", "predict("]:
        assert forbidden not in source


# --- nothing about costing changed -------------------------------------------


def test_the_costing_modules_cannot_reach_the_time_profile_layer():
    from pathlib import Path

    import cad_costing_agent.cad_mesh as anchor

    directory = Path(anchor.__file__).parent
    for module in [
        "costing.py",
        "operation_costing.py",
        "additional_costs.py",
        "part_cost.py",
        "cad_part_cost.py",
        "cad_mass.py",
        "rate_sets.py",
    ]:
        source = (directory / module).read_text(encoding="utf-8")
        assert "time_profiles" not in source, module


def test_the_time_profile_layer_imports_no_costing_arithmetic():
    """Checked against code only — the docstrings *discuss* money at length."""
    import ast
    from pathlib import Path

    import cad_costing_agent.time_profiles as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    source = ast.unparse(tree)

    for forbidden in [
        "cad_costing_agent.costing",
        "cad_costing_agent.money",
        "cad_costing_agent.rate_sets",
        "cad_costing_agent.part_cost",
        "Money",
        "RateSet",
        "currency",
    ]:
        assert forbidden not in source


def test_existing_rate_set_behaviour_is_unchanged():
    rates = client.get("/rates").json()

    assert rates
    assert all("currency" in row for row in rates)
    poc = client.get("/rates/poc-default").json()
    assert poc["currency"] == "INR"
    assert poc["operations"]["drilling"]["machine_rate_per_hour"] == 1800.0
    # Publishing a time profile touches nothing about rates.
    publish(shop_profile())
    assert client.get("/rates/poc-default").json() == poc


def test_costing_a_plan_is_unaffected_by_which_profile_predicted_the_times():
    """Same committed times, same money, whatever produced the prediction."""
    v1 = TimeProfile.model_validate(publish(shop_profile("1.0.0", drilling_feed_mm_per_rev=0.15)).json())
    v2 = TimeProfile.model_validate(publish(shop_profile("2.0.0", drilling_feed_mm_per_rev=0.05)).json())

    def priced(profile):
        plan = plan_under(profile)
        plan = plan.replacing(drilling_of(plan).with_times(setup_time_minutes=5.0, cycle_time_minutes=2.0))
        return cost(plan).json()["part_cost"]["total_part_cost"]

    assert priced(v1) == priced(v2)


def test_an_untimed_operation_is_still_not_costed_under_any_profile():
    profile = TimeProfile.model_validate(publish(shop_profile()).json())

    part_cost = cost(plan_under(profile)).json()["part_cost"]

    assert part_cost["operations"] == []
    assert part_cost["total_operation_cost"] == 0.0


# --- the UI ------------------------------------------------------------------


def test_the_ui_has_a_time_setup_area_separate_from_costing_setup():
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    assert 'data-tab="timesetup"' in markup
    assert 'id="tab-timesetup"' in markup
    assert 'src="timesetup.js"' in markup
    assert "Time-assumption profiles" in markup


def test_the_ui_exposes_every_parameter_the_time_model_uses():
    """A parameter the estimator reads but nobody can edit is a hidden constant."""
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    editable = set(
        __import__("re").findall(r'data-param="([a-z_]+)"', markup)
    )
    model_params = set(TimeAssumptions.model_fields) - {"is_illustrative"}

    assert model_params == editable


def test_the_ui_offers_versioning_and_never_an_edit():
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    script = (STATIC_PATH / "timesetup.js").read_text(encoding="utf-8")

    assert 'id="new-time-profile"' in markup
    assert 'id="publish-time-profile"' in markup
    assert 'id="validate-time-profile"' in markup
    assert "new-time-version" in script
    assert "never edited" in script
    assert "PUT" not in script


def test_the_estimate_page_lets_a_profile_and_version_be_chosen():
    markup = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    assert 'id="time_profile_id"' in markup
    assert 'id="time_profile_version"' in markup
    assert "Time profile" in markup


def test_the_ui_says_plainly_that_a_time_profile_is_not_money():
    markup = " ".join((STATIC_PATH / "index.html").read_text(encoding="utf-8").split())

    assert "It holds no money." in markup
    assert "No money here." in markup


def test_the_plan_estimates_under_the_selected_profile_and_shows_it():
    plan_js = (STATIC_PATH / "plan.js").read_text(encoding="utf-8")

    assert "time_profile_id" in plan_js
    assert "Assumptions from:" in plan_js
    assert "system_time_profile_version" in plan_js
