"""User-configurable costing setup: authoring profiles and versions from the UI.

These cover the contract the Costing Setup screen depends on — publishing a
profile, publishing a new version that copies the last one, the immutability of
anything already published, and the fact that a profile authored this way is
immediately usable for an estimate and permanently recorded on it.

The rate-set *model* is covered by `test_rate_sets.py` and the authoring
endpoints by `test_rate_set_authoring.py`. What is new here is the setup
workflow: validation-before-publish, the listing a person reads, and the
version lifecycle end to end.
"""

import pytest
import trimesh
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cad_costing_agent.db import get_session, init_db
from cad_costing_agent.main import STATIC_PATH, app

client = TestClient(app)

BOX_MM = (10.0, 20.0, 30.0)


def stl_bytes(x=BOX_MM[0], y=BOX_MM[1], z=BOX_MM[2]) -> bytes:
    return trimesh.creation.box(extents=(x, y, z)).export(file_type="stl")


def profile(**overrides) -> dict:
    """A realistic profile of the shape the setup screen posts.

    Deliberately round numbers: a 10x20x30 mm box is 6000 mm^3, so at 7850
    kg/m^3 one part is 0.0471 kg and at 100/kg it costs 4.71 — which makes the
    expected material cost checkable by hand.
    """
    body = {
        "rate_set_id": "pune-machine-shop",
        "name": "Pune Machine Shop",
        "version": "1.0.0",
        "description": "Real shop rates agreed for FY26.",
        "currency": "INR",
        "is_illustrative": False,
        "materials": {
            "steel": {"name": "Steel", "density_kg_m3": 7850.0, "cost_per_kg": 100.0},
            "aluminium": {"name": "Aluminium", "density_kg_m3": 2700.0, "cost_per_kg": 250.0},
        },
        "operations": {
            "milling": {"machine_rate_per_hour": 1200.0, "labor_rate_per_hour": 600.0},
            "broaching": {"machine_rate_per_hour": 2400.0, "labor_rate_per_hour": 800.0},
        },
        "additional_costs": {
            "overhead_rate_percent": 10.0,
            "scrap_rate_percent": 5.0,
            "tooling_cost_per_batch": 1000.0,
        },
    }
    body.update(overrides)
    return body


def publish(body: dict | None = None):
    return client.post("/rates", json=body if body is not None else profile())


def publish_version(rate_set_id: str, body: dict):
    payload = {key: value for key, value in body.items() if key != "rate_set_id"}
    return client.post(f"/rates/{rate_set_id}/versions", json=payload)


def estimate(rate_set_id: str, version: str | None = None, quantity: int = 10):
    form = {
        "material": "Steel",
        "source_unit": "mm",
        "part_id": "P-001",
        "part_name": "Bracket",
        "quantity": str(quantity),
        "rate_set_id": rate_set_id,
        "operation_requirements": (
            '[{"operation_id":"OP1","operation_name":"Rough mill","process_type":"milling",'
            f'"setup_time_minutes":20,"cycle_time_minutes":2,"quantity":{quantity}}}]'
        ),
    }
    if version is not None:
        form["rate_set_version"] = version
    return client.post(
        "/cost/cad-part",
        files={"file": ("box.stl", stl_bytes(), "application/octet-stream")},
        data=form,
    )


# --- publishing a profile ---------------------------------------------------


def test_a_profile_authored_from_the_setup_screen_is_stored_exactly():
    response = publish()

    assert response.status_code == 201
    stored = response.json()
    assert stored["rate_set_id"] == "pune-machine-shop"
    assert stored["name"] == "Pune Machine Shop"
    assert stored["version"] == "1.0.0"
    assert stored["currency"] == "INR"
    assert stored["is_illustrative"] is False
    assert stored["materials"]["steel"]["cost_per_kg"] == 100.0
    assert stored["operations"]["broaching"]["machine_rate_per_hour"] == 2400.0
    assert stored["additional_costs"]["overhead_rate_percent"] == 10.0


def test_currency_defaults_to_inr_when_the_screen_does_not_send_one():
    body = profile()
    del body["currency"]

    assert publish(body).json()["currency"] == "INR"


def test_process_types_are_not_restricted_to_a_fixed_list():
    """The screen suggests common processes but must accept any shop's own."""
    body = profile(
        operations={
            "honing": {"machine_rate_per_hour": 900.0, "labor_rate_per_hour": 400.0},
            "wire edm": {"machine_rate_per_hour": 3000.0, "labor_rate_per_hour": 700.0},
            "shot peening": {"machine_rate_per_hour": 500.0, "labor_rate_per_hour": 300.0},
        }
    )

    stored = publish(body).json()

    assert set(stored["operations"]) == {"honing", "wire edm", "shot peening"}


def test_a_custom_process_type_can_actually_be_costed():
    publish(
        profile(
            operations={"honing": {"machine_rate_per_hour": 900.0, "labor_rate_per_hour": 300.0}}
        )
    )

    response = client.post(
        "/cost/cad-part",
        files={"file": ("box.stl", stl_bytes(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Bushing",
            "quantity": "5",
            "rate_set_id": "pune-machine-shop",
            "operation_requirements": (
                '[{"operation_id":"OP1","operation_name":"Hone bore","process_type":"honing",'
                '"setup_time_minutes":15,"cycle_time_minutes":3,"quantity":5}]'
            ),
        },
    )

    assert response.status_code == 200
    assert response.json()["part_cost"]["total_operation_cost"] > 0


# --- the listing a person reads ---------------------------------------------


def test_the_listing_carries_the_counts_the_profile_cards_show():
    publish()

    rows = client.get("/rates").json()
    row = next(r for r in rows if r["rate_set_id"] == "pune-machine-shop")

    assert row["material_count"] == 2
    assert row["process_count"] == 2
    assert row["name"] == "Pune Machine Shop"
    assert row["currency"] == "INR"
    assert row["is_illustrative"] is False


def test_seeded_profiles_are_still_marked_illustrative_in_the_listing():
    rows = client.get("/rates").json()

    seeded = [r for r in rows if r["rate_set_id"].startswith("poc-")]
    assert seeded
    assert all(row["is_illustrative"] for row in seeded)


def test_counts_are_reported_per_version_not_per_profile():
    publish()
    publish_version(
        "pune-machine-shop",
        profile(
            version="1.1.0",
            materials={"steel": {"name": "Steel", "density_kg_m3": 7850.0, "cost_per_kg": 110.0}},
        ),
    )

    rows = {
        row["version"]: row
        for row in client.get("/rates").json()
        if row["rate_set_id"] == "pune-machine-shop"
    }

    assert rows["1.0.0"]["material_count"] == 2
    assert rows["1.1.0"]["material_count"] == 1


# --- validation before publishing -------------------------------------------


def test_validate_approves_a_new_profile_without_storing_it():
    response = client.post("/rates/validate", json=profile())

    assert response.status_code == 200
    verdict = response.json()
    assert verdict["valid"] is True
    assert verdict["is_new_profile"] is True
    assert verdict["version_already_exists"] is False
    assert verdict["material_count"] == 2
    assert verdict["process_count"] == 2
    # Nothing was written.
    assert all(row["rate_set_id"] != "pune-machine-shop" for row in client.get("/rates").json())


def test_validate_refuses_a_version_that_is_already_published():
    publish()

    verdict = client.post("/rates/validate", json=profile()).json()

    assert verdict["valid"] is False
    assert verdict["version_already_exists"] is True
    assert any("can never be changed" in message for message in verdict["messages"])


def test_validate_knows_a_new_version_of_an_existing_profile_is_not_a_new_profile():
    publish()

    verdict = client.post("/rates/validate", json=profile(version="2.0.0")).json()

    assert verdict["valid"] is True
    assert verdict["is_new_profile"] is False


def test_validate_warns_about_illustrative_rates():
    verdict = client.post("/rates/validate", json=profile(is_illustrative=True)).json()

    assert verdict["valid"] is True
    assert any("illustrative" in message for message in verdict["messages"])


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"materials": {}}, "prices no materials"),
        ({"operations": {}}, "prices no processes"),
    ],
)
def test_validate_flags_an_empty_profile_without_blocking_it(overrides, expected):
    verdict = client.post("/rates/validate", json=profile(**overrides)).json()

    assert any(expected in message for message in verdict["messages"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"rate_set_id": "Not A Slug"},
        {"version": "1.0"},
        {"version": "v1.0.0"},
        {"currency": "rupees"},
        {"materials": {"steel": {"name": "Steel", "density_kg_m3": 0.0, "cost_per_kg": 100.0}}},
        {"materials": {"steel": {"name": "Steel", "density_kg_m3": -1.0, "cost_per_kg": 100.0}}},
        {"materials": {"steel": {"name": "Steel", "density_kg_m3": 7850.0, "cost_per_kg": -5.0}}},
        {"materials": {"steel": {"name": "", "density_kg_m3": 7850.0, "cost_per_kg": 100.0}}},
        {"operations": {"milling": {"machine_rate_per_hour": 0.0, "labor_rate_per_hour": 600.0}}},
        {"operations": {"milling": {"machine_rate_per_hour": 1200.0, "labor_rate_per_hour": -1.0}}},
        {"additional_costs": {"overhead_rate_percent": -1.0}},
        {"additional_costs": {"overhead_rate_percent": 1001.0}},
        {"additional_costs": {"scrap_rate_percent": 100.0}},
        {"additional_costs": {"scrap_rate_percent": -0.5}},
        {"additional_costs": {"tooling_cost_per_batch": -10.0}},
    ],
)
def test_invalid_values_are_rejected_by_both_validate_and_publish(overrides):
    """The backend is the authority; the screen's own checks only mirror it."""
    body = profile(**overrides)

    assert client.post("/rates/validate", json=body).status_code == 422
    assert client.post("/rates", json=body).status_code == 422


def test_a_rejected_profile_is_not_partially_stored():
    client.post("/rates", json=profile(materials={"steel": {"name": "Steel", "density_kg_m3": 0.0, "cost_per_kg": 1.0}}))

    assert all(row["rate_set_id"] != "pune-machine-shop" for row in client.get("/rates").json())


# --- versions ---------------------------------------------------------------


def test_a_new_version_copies_the_previous_one_and_changes_only_what_was_edited():
    publish()
    previous = client.get("/rates/pune-machine-shop?version=1.0.0").json()

    # Exactly what the screen does: take the previous version, change one rate.
    edited = {key: value for key, value in previous.items() if key != "rate_set_id"}
    edited["version"] = "1.1.0"
    edited["materials"] = dict(previous["materials"])
    edited["materials"]["steel"] = {**previous["materials"]["steel"], "cost_per_kg": 120.0}
    response = client.post("/rates/pune-machine-shop/versions", json=edited)

    assert response.status_code == 201
    published = response.json()
    assert published["materials"]["steel"]["cost_per_kg"] == 120.0
    # Everything not edited came across untouched.
    assert published["materials"]["aluminium"] == previous["materials"]["aluminium"]
    assert published["operations"] == previous["operations"]
    assert published["additional_costs"] == previous["additional_costs"]
    assert published["currency"] == previous["currency"]
    assert published["is_illustrative"] == previous["is_illustrative"]


def test_publishing_a_new_version_leaves_the_previous_one_untouched():
    publish()
    before = client.get("/rates/pune-machine-shop?version=1.0.0").json()

    publish_version(
        "pune-machine-shop",
        profile(
            version="2.0.0",
            materials={"steel": {"name": "Steel", "density_kg_m3": 7850.0, "cost_per_kg": 999.0}},
        ),
    )

    assert client.get("/rates/pune-machine-shop?version=1.0.0").json() == before


def test_republishing_an_existing_version_is_a_conflict():
    publish()

    response = publish()

    assert response.status_code == 409
    assert "immutable" in response.json()["detail"]


def test_republishing_an_existing_version_through_the_versions_route_is_a_conflict():
    publish()

    response = publish_version("pune-machine-shop", profile(version="1.0.0"))

    assert response.status_code == 409


def test_there_is_no_way_to_edit_or_delete_a_published_version():
    """Immutability is enforced by the absence of any mutating route."""
    publish()

    for method in ("PUT", "PATCH", "DELETE"):
        response = client.request(method, "/rates/pune-machine-shop", json=profile())
        assert response.status_code == 405, f"{method} must not be routable"


def test_the_latest_version_is_resolved_numerically():
    publish()
    publish_version("pune-machine-shop", profile(version="2.0.0"))
    publish_version("pune-machine-shop", profile(version="10.0.0"))

    assert client.get("/rates/pune-machine-shop").json()["version"] == "10.0.0"


# --- the profile reaches the estimate ---------------------------------------


def test_a_newly_published_profile_is_immediately_selectable_for_an_estimate():
    publish()

    assert any(row["rate_set_id"] == "pune-machine-shop" for row in client.get("/rates").json())
    assert estimate("pune-machine-shop").status_code == 200


def test_the_estimate_uses_the_rates_that_were_entered():
    """A 10x20x30 mm steel box at 100/kg is 0.0471 kg and 4.71 a part."""
    publish()

    body = estimate("pune-machine-shop", quantity=10).json()
    cost = body["part_cost"]

    assert body["mass"]["mass_kg"] == pytest.approx(0.0471, rel=1e-9)
    assert cost["total_material_cost"] == pytest.approx(47.10, abs=0.01)
    # Milling, 20 min setup and 10 x 2 min run. Labour is charged on run time
    # only; setup is machine rate alone. So 1200 x 1/3 run + 1200 x 1/3 setup
    # + 600 x 1/3 labour = 400 + 400 + 200.
    assert cost["total_operation_cost"] == pytest.approx(1000.00, abs=0.01)
    assert cost["direct_cost_base"] == pytest.approx(1047.10, abs=0.01)
    assert cost["additional_costs"]["overhead_cost"] == pytest.approx(104.71, abs=0.01)
    assert cost["additional_costs"]["scrap_cost"] == pytest.approx(52.36, abs=0.01)
    assert cost["additional_costs"]["tooling_cost"] == pytest.approx(1000.00, abs=0.01)
    assert cost["total_part_cost"] == pytest.approx(2204.17, abs=0.01)
    assert cost["per_unit_cost"] == pytest.approx(220.42, abs=0.01)
    assert cost["currency"] == "INR"


def test_the_estimate_records_which_profile_version_produced_it():
    publish()

    estimate_id = estimate("pune-machine-shop").json()["part_cost"]["estimate_id"]
    stored = client.get(f"/estimates/{estimate_id}").json()

    assert stored["rate_set_id"] == "pune-machine-shop"
    assert stored["rate_set_version"] == "1.0.0"
    assert stored["rate_set_snapshot"]["materials"]["steel"]["cost_per_kg"] == 100.0


def test_a_new_version_changes_new_estimates_but_never_old_ones():
    publish()
    first_id = estimate("pune-machine-shop").json()["part_cost"]["estimate_id"]
    first_total = client.get(f"/estimates/{first_id}").json()["total_part_cost"]

    # Double the steel price and publish it as a new version.
    publish_version(
        "pune-machine-shop",
        profile(
            version="2.0.0",
            materials={
                "steel": {"name": "Steel", "density_kg_m3": 7850.0, "cost_per_kg": 200.0},
                "aluminium": {"name": "Aluminium", "density_kg_m3": 2700.0, "cost_per_kg": 250.0},
            },
        ),
    )

    second = estimate("pune-machine-shop").json()["part_cost"]
    assert second["rate_set"]["version"] == "2.0.0"
    assert second["total_material_cost"] == pytest.approx(94.20, abs=0.01)

    # The first estimate is unchanged, and still points at v1.0.0.
    after = client.get(f"/estimates/{first_id}").json()
    assert after["rate_set_version"] == "1.0.0"
    assert after["total_part_cost"] == first_total
    assert after["total_material_cost"] == pytest.approx(47.10, abs=0.01)


def test_an_older_version_can_still_be_pinned_after_a_newer_one_exists():
    publish()
    publish_version(
        "pune-machine-shop",
        profile(
            version="2.0.0",
            materials={"steel": {"name": "Steel", "density_kg_m3": 7850.0, "cost_per_kg": 200.0}},
        ),
    )

    pinned = estimate("pune-machine-shop", version="1.0.0").json()["part_cost"]

    assert pinned["rate_set"]["version"] == "1.0.0"
    assert pinned["total_material_cost"] == pytest.approx(47.10, abs=0.01)


def test_an_estimate_from_a_real_profile_is_not_flagged_illustrative():
    publish()

    cost = estimate("pune-machine-shop").json()["part_cost"]

    assert cost["rate_set"]["is_illustrative"] is False


# --- the profile is the source of material data, density included -----------


def test_a_material_that_exists_only_in_the_profile_can_be_costed():
    """Regression: mass used to come from the built-in two-material catalog.

    A CAD estimate resolved density through `materials.py` regardless of the
    selected profile, so any material a shop defined itself — "EN8 steel",
    "Aluminium 6061" — failed with "Unknown material" even though the profile
    priced it. That made a self-authored profile unusable for CAD costing.
    """
    publish(
        profile(
            materials={
                "en8 steel": {"name": "EN8 steel", "density_kg_m3": 7850.0, "cost_per_kg": 72.5}
            }
        )
    )

    response = client.post(
        "/cost/cad-part",
        files={"file": ("box.stl", stl_bytes(), "application/octet-stream")},
        data={
            "material": "EN8 steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Bracket",
            "quantity": "1",
            "rate_set_id": "pune-machine-shop",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mass"]["material"]["name"] == "EN8 steel"
    assert body["mass"]["mass_kg"] == pytest.approx(0.0471, rel=1e-9)


def test_the_profiles_density_wins_over_the_built_in_catalogs():
    """A profile that redefines a material must not be half-ignored.

    Cost per kg already came from the profile while density came from the
    built-in catalog, so a profile declaring its own density produced a mass
    that silently disagreed with the rates it was priced at.
    """
    publish(
        profile(
            materials={
                # The built-in catalog says steel is 7850; this profile says 8000.
                "steel": {"name": "Steel", "density_kg_m3": 8000.0, "cost_per_kg": 50.0}
            }
        )
    )

    body = client.post(
        "/cost/cad-part",
        files={"file": ("box.stl", stl_bytes(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Bracket",
            "quantity": "1",
            "rate_set_id": "pune-machine-shop",
        },
    ).json()

    # 6000 mm^3 at 8000 kg/m^3 is 0.048 kg, not the 0.0471 the catalog implies.
    assert body["mass"]["material"]["density_kg_m3"] == 8000.0
    assert body["mass"]["mass_kg"] == pytest.approx(0.048, rel=1e-9)
    assert body["part_cost"]["total_material_cost"] == pytest.approx(2.40, abs=0.01)


def test_without_a_profile_the_built_in_catalog_is_still_used():
    """The fallback path is unchanged for requests that select no profile."""
    body = client.post(
        "/cost/cad-part",
        files={"file": ("box.stl", stl_bytes(), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Bracket",
            "quantity": "1",
        },
    ).json()

    assert body["mass"]["material"]["density_kg_m3"] == 7850.0
    assert body["mass"]["mass_kg"] == pytest.approx(0.0471, rel=1e-9)


def test_a_material_the_profile_does_not_price_is_still_refused():
    publish()

    response = client.post(
        "/cost/cad-part",
        files={"file": ("box.stl", stl_bytes(), "application/octet-stream")},
        data={
            "material": "Titanium",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Bracket",
            "quantity": "1",
            "rate_set_id": "pune-machine-shop",
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "Titanium" in detail
    # The message must name the profile's materials, not the built-in catalog's.
    assert "Steel" in detail and "Aluminium" in detail


# --- persistence across a restart -------------------------------------------


def test_profiles_and_estimates_survive_a_restart(tmp_path):
    """A profile authored in the UI has to still be there tomorrow morning."""
    database_path = tmp_path / "setup.db"

    def open_database():
        engine = create_engine(
            f"sqlite:///{database_path}", connect_args={"check_same_thread": False}
        )
        init_db(engine)
        return engine, sessionmaker(bind=engine, expire_on_commit=False)

    def use(session_factory):
        def override():
            with session_factory() as session:
                yield session

        app.dependency_overrides[get_session] = override

    engine, factory = open_database()
    use(factory)
    publish()
    publish_version(
        "pune-machine-shop",
        profile(
            version="2.0.0",
            materials={"steel": {"name": "Steel", "density_kg_m3": 7850.0, "cost_per_kg": 200.0}},
        ),
    )
    first_id = estimate("pune-machine-shop", version="1.0.0").json()["part_cost"]["estimate_id"]
    engine.dispose()

    # Restart: brand new engine and sessions against the same file.
    engine, factory = open_database()
    use(factory)

    versions = [
        row["version"]
        for row in client.get("/rates").json()
        if row["rate_set_id"] == "pune-machine-shop"
    ]
    assert sorted(versions) == ["1.0.0", "2.0.0"]
    reopened_v1 = client.get("/rates/pune-machine-shop?version=1.0.0").json()
    assert reopened_v1["materials"]["steel"]["cost_per_kg"] == 100.0
    assert client.get("/rates/pune-machine-shop").json()["version"] == "2.0.0"

    reopened = client.get(f"/estimates/{first_id}").json()
    assert reopened["rate_set_version"] == "1.0.0"
    assert reopened["total_material_cost"] == pytest.approx(47.10, abs=0.01)
    engine.dispose()


# --- the setup screen itself ------------------------------------------------


def test_the_setup_script_is_served_and_referenced():
    html = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    assert (STATIC_PATH / "setup.js").is_file()
    assert '<script src="setup.js"></script>' in html
    assert client.get("/ui/setup.js").status_code == 200


def test_the_page_has_a_costing_setup_tab_with_every_wizard_step():
    html = (STATIC_PATH / "index.html").read_text(encoding="utf-8")

    assert 'data-tab="setup"' in html
    for step in ("general", "materials", "operations", "additional", "review"):
        assert f'id="wpane-{step}"' in html
        assert f'data-step="{step}"' in html


def test_the_setup_screen_offers_no_way_to_edit_a_published_version():
    """Changing rates must always mean publishing a new version."""
    setup_js = (STATIC_PATH / "setup.js").read_text(encoding="utf-8")

    for mutation in ('"PUT"', '"PATCH"', '"DELETE"', "method: 'PUT'", "method: 'DELETE'"):
        assert mutation not in setup_js
    # The only route to changed rates is a new version.
    assert "Create new version" in setup_js
    assert "data-newversion" in setup_js


def test_the_setup_screen_contains_no_costing_arithmetic():
    """It configures rates; it never applies them."""
    setup_js = (STATIC_PATH / "setup.js").read_text(encoding="utf-8")

    for formula_fragment in [
        "cost_per_kg *",
        "machine_rate_per_hour *",
        "/ 100 *",
        "* quantity",
        "direct_cost_base",
    ]:
        assert formula_fragment not in setup_js


def test_the_setup_screen_publishes_through_the_existing_endpoints():
    setup_js = (STATIC_PATH / "setup.js").read_text(encoding="utf-8")

    assert '"/rates"' in setup_js
    assert "/versions" in setup_js
    assert '"/rates/validate"' in setup_js


def test_the_setup_screen_defaults_to_inr_and_never_defaults_to_usd():
    html = (STATIC_PATH / "index.html").read_text(encoding="utf-8")
    setup_js = (STATIC_PATH / "setup.js").read_text(encoding="utf-8")

    assert '<option value="INR" selected>' in html
    assert '<option value="USD" selected>' not in html
    assert 'currency: "INR"' in setup_js
