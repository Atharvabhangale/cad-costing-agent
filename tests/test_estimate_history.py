from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cad_costing_agent.db_models import EstimateRow
from cad_costing_agent.estimates import EstimateSummary, compare_estimates
from cad_costing_agent.main import app
from cad_costing_agent.rate_sets import MaterialRate
from cad_costing_agent.repository import EstimateRepository, RateSetRepository

client = TestClient(app)

WORKED_PART = {
    "part_id": "P100",
    "part_name": "Flange",
    "material": "Steel",
    "mass_kg": 2.5,
    "quantity": 4,
}
WORKED_REQUIREMENT = {
    "operation_id": "OP1",
    "operation_name": "Milling",
    "process_type": "Milling",
    "setup_time_minutes": 30.0,
    "cycle_time_minutes": 3.0,
    "quantity": 4,
}
WORKED_REQUEST = {
    "part": WORKED_PART,
    "operation_requirements": [WORKED_REQUIREMENT],
    "rate_set_id": "poc-machine-shop",
    "rate_set_version": "1.0.0",
}


def create_estimate(**part_overrides) -> str:
    part = {**WORKED_PART, **part_overrides}
    # Operation quantity must match part quantity; that rule is unchanged.
    requirement = {**WORKED_REQUIREMENT, "quantity": part["quantity"]}
    response = client.post(
        "/cost/part", json={**WORKED_REQUEST, "part": part, "operation_requirements": [requirement]}
    )
    assert response.status_code == 200
    return response.json()["estimate_id"]


def create_estimate_without_rate_set(**part_overrides) -> str:
    response = client.post("/cost/part", json={"part": {**WORKED_PART, **part_overrides}})
    assert response.status_code == 200
    return response.json()["estimate_id"]


# --- listing ----------------------------------------------------------------


def test_listing_is_empty_on_a_fresh_database():
    body = client.get("/estimates").json()

    assert body["total"] == 0
    assert body["items"] == []


def test_listing_returns_created_estimates():
    estimate_id = create_estimate()

    body = client.get("/estimates").json()

    assert body["total"] == 1
    row = body["items"][0]
    assert row["estimate_id"] == estimate_id
    assert row["part_id"] == "P100"
    assert row["part_name"] == "Flange"
    assert row["rate_set_id"] == "poc-machine-shop"
    assert row["rate_set_version"] == "1.0.0"
    assert row["total_part_cost"] == pytest.approx(1363.20)


def test_listing_excludes_the_audit_snapshots():
    create_estimate()

    row = client.get("/estimates").json()["items"][0]

    assert "request_snapshot" not in row
    assert "result_snapshot" not in row
    assert "rate_set_snapshot" not in row


def test_listing_is_newest_first():
    first = create_estimate(part_id="P1")
    second = create_estimate(part_id="P2")
    third = create_estimate(part_id="P3")

    ids = [row["estimate_id"] for row in client.get("/estimates").json()["items"]]

    assert ids == [third, second, first]


# --- pagination -------------------------------------------------------------


def test_pagination_limits_the_page_but_reports_the_full_total():
    for index in range(5):
        create_estimate(part_id=f"P{index}")

    body = client.get("/estimates", params={"limit": 2}).json()

    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2


def test_pagination_walks_the_whole_history_without_gaps_or_repeats():
    created = [create_estimate(part_id=f"P{index}") for index in range(5)]

    paged = []
    for offset in range(0, 5, 2):
        page = client.get("/estimates", params={"limit": 2, "offset": offset}).json()
        paged.extend(row["estimate_id"] for row in page["items"])

    assert paged == list(reversed(created))
    assert len(set(paged)) == 5


def test_offset_past_the_end_returns_an_empty_page():
    create_estimate()

    body = client.get("/estimates", params={"offset": 50}).json()

    assert body["total"] == 1
    assert body["items"] == []


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
def test_invalid_pagination_is_rejected(params):
    assert client.get("/estimates", params=params).status_code == 422


# --- filtering --------------------------------------------------------------


def test_filter_by_part_id():
    wanted = create_estimate(part_id="WANTED")
    create_estimate(part_id="OTHER")

    body = client.get("/estimates", params={"part_id": "WANTED"}).json()

    assert body["total"] == 1
    assert body["items"][0]["estimate_id"] == wanted


def test_filter_by_rate_set_id():
    with_rate_set = create_estimate()
    create_estimate_without_rate_set(part_id="NO-RATES")

    body = client.get("/estimates", params={"rate_set_id": "poc-machine-shop"}).json()

    assert body["total"] == 1
    assert body["items"][0]["estimate_id"] == with_rate_set


def test_filter_matching_nothing_returns_an_empty_page():
    create_estimate()

    body = client.get("/estimates", params={"part_id": "does-not-exist"}).json()

    assert body["total"] == 0
    assert body["items"] == []


def test_filter_by_date_range():
    estimate_id = create_estimate()
    stored = client.get(f"/estimates/{estimate_id}").json()["created_at"]
    created_at = datetime.fromisoformat(stored)

    inside = client.get(
        "/estimates",
        params={
            "created_after": (created_at - timedelta(minutes=1)).isoformat(),
            "created_before": (created_at + timedelta(minutes=1)).isoformat(),
        },
    ).json()
    assert inside["total"] == 1

    after_the_window = client.get(
        "/estimates", params={"created_after": (created_at + timedelta(minutes=1)).isoformat()}
    ).json()
    assert after_the_window["total"] == 0

    before_the_window = client.get(
        "/estimates", params={"created_before": (created_at - timedelta(minutes=1)).isoformat()}
    ).json()
    assert before_the_window["total"] == 0


def test_timezone_aware_date_filters_are_normalised_to_stored_utc():
    """Stored timestamps are naive UTC, so an aware bound must be converted."""
    create_estimate()

    far_future = datetime.now(timezone.utc) + timedelta(days=1)
    far_past = datetime.now(timezone.utc) - timedelta(days=1)

    assert client.get("/estimates", params={"created_before": far_future.isoformat()}).json()["total"] == 1
    assert client.get("/estimates", params={"created_after": far_past.isoformat()}).json()["total"] == 1
    assert client.get("/estimates", params={"created_after": far_future.isoformat()}).json()["total"] == 0


def test_filters_combine():
    create_estimate(part_id="A")
    wanted = create_estimate(part_id="B")
    create_estimate_without_rate_set(part_id="B")

    body = client.get(
        "/estimates", params={"part_id": "B", "rate_set_id": "poc-machine-shop"}
    ).json()

    assert body["total"] == 1
    assert body["items"][0]["estimate_id"] == wanted


# --- comparison -------------------------------------------------------------


def test_compare_two_estimates():
    baseline = create_estimate(part_id="P1")
    comparison = create_estimate(part_id="P2", quantity=8)

    body = client.get(
        "/estimates/compare", params={"baseline": baseline, "comparison": comparison}
    ).json()

    assert body["baseline"]["estimate_id"] == baseline
    assert body["comparison"]["estimate_id"] == comparison
    assert body["baseline"]["created_at"]
    assert body["comparison"]["created_at"]
    assert body["basis"]


def test_compare_reports_component_deltas():
    baseline_id = create_estimate(part_id="P1")
    comparison_id = create_estimate(part_id="P2", quantity=8)

    baseline = client.get(f"/estimates/{baseline_id}").json()
    comparison = client.get(f"/estimates/{comparison_id}").json()
    body = client.get(
        "/estimates/compare", params={"baseline": baseline_id, "comparison": comparison_id}
    ).json()

    for key, column in [
        ("material_cost", "total_material_cost"),
        ("operation_cost", "total_operation_cost"),
        ("additional_cost", "total_additional_cost"),
        ("total_part_cost", "total_part_cost"),
    ]:
        assert body[key]["baseline"] == pytest.approx(baseline[column])
        assert body[key]["comparison"] == pytest.approx(comparison[column])
        assert body[key]["delta"] == pytest.approx(comparison[column] - baseline[column])


def test_compare_identifies_which_estimate_is_newer():
    older = create_estimate(part_id="P1")
    newer = create_estimate(part_id="P2")

    forwards = client.get(
        "/estimates/compare", params={"baseline": older, "comparison": newer}
    ).json()
    assert forwards["older_estimate_id"] == older
    assert forwards["newer_estimate_id"] == newer
    assert forwards["comparison_is_newer"] is True

    # Argument order must not change which one is actually newer.
    backwards = client.get(
        "/estimates/compare", params={"baseline": newer, "comparison": older}
    ).json()
    assert backwards["older_estimate_id"] == older
    assert backwards["newer_estimate_id"] == newer
    assert backwards["comparison_is_newer"] is False


def test_newer_older_is_decided_by_recording_order_not_the_clock(db_session):
    """Two estimates can share a timestamp; recording order must still decide.

    The clock's resolution (~15 ms on Windows) is coarse enough that estimates
    created in quick succession genuinely collide, which previously made
    newer/older arbitrary.
    """
    first = create_estimate(part_id="P1")
    second = create_estimate(part_id="P2")

    # Force the collision rather than hoping for it.
    shared = datetime(2026, 1, 1, 12, 0, 0)
    for estimate_id in (first, second):
        row = db_session.execute(
            select(EstimateRow).where(EstimateRow.estimate_id == estimate_id)
        ).scalar_one()
        row.created_at = shared
    db_session.commit()

    body = client.get(
        "/estimates/compare", params={"baseline": first, "comparison": second}
    ).json()

    assert body["baseline"]["created_at"] == body["comparison"]["created_at"]
    assert body["older_estimate_id"] == first
    assert body["newer_estimate_id"] == second
    assert body["comparison_is_newer"] is True


def test_compare_percent_change():
    baseline = create_estimate(part_id="P1", quantity=4)
    comparison = create_estimate(part_id="P2", quantity=8)

    body = client.get(
        "/estimates/compare", params={"baseline": baseline, "comparison": comparison}
    ).json()
    material = body["material_cost"]

    assert material["percent_change"] == pytest.approx(
        (material["delta"] / material["baseline"]) * 100
    )


def test_compare_percent_change_is_null_against_a_zero_baseline():
    """A percentage against zero is undefined, not infinite."""
    zero_side = EstimateSummary(
        estimate_id="a",
        sequence=1,
        currency="INR",
        created_at=datetime(2026, 1, 1),
        source="cost/part",
        part_id="P",
        part_name="N",
        quantity=1,
        total_material_cost=0.0,
        total_operation_cost=0.0,
        total_additional_cost=0.0,
        total_part_cost=0.0,
    )
    other = zero_side.model_copy(
        update={
            "estimate_id": "b",
            "sequence": 2,
            "created_at": datetime(2026, 1, 2),
            "total_material_cost": 5.0,
        }
    )

    result = compare_estimates(zero_side, other)

    assert result.material_cost.delta == 5.0
    assert result.material_cost.percent_change is None


def test_compare_an_estimate_with_itself_is_all_zero_deltas():
    estimate_id = create_estimate()

    body = client.get(
        "/estimates/compare", params={"baseline": estimate_id, "comparison": estimate_id}
    ).json()

    assert body["total_part_cost"]["delta"] == 0
    assert body["comparison_is_newer"] is False


@pytest.mark.parametrize("missing", ["baseline", "comparison"])
def test_compare_with_an_unknown_estimate_returns_404(missing):
    known = create_estimate()
    params = {"baseline": known, "comparison": known, missing: "does-not-exist"}

    response = client.get("/estimates/compare", params=params)

    assert response.status_code == 404
    assert "Unknown estimate" in response.json()["detail"]


def test_compare_requires_both_ids():
    assert client.get("/estimates/compare").status_code == 422


def test_compare_route_is_not_shadowed_by_the_single_estimate_route():
    """'/estimates/compare' must not be read as an estimate id."""
    response = client.get("/estimates/compare", params={"baseline": "x", "comparison": "y"})

    # 404 for the unknown ids, not a 422 from the id route or a routing mix-up.
    assert response.status_code == 404


# --- historical integrity ---------------------------------------------------


def test_history_and_comparison_survive_a_newer_rate_set_version(db_session):
    baseline_id = create_estimate(part_id="P1")
    comparison_id = create_estimate(part_id="P2", quantity=8)

    listing_before = client.get("/estimates").json()
    comparison_before = client.get(
        "/estimates/compare", params={"baseline": baseline_id, "comparison": comparison_id}
    ).json()

    # Publish a much more expensive version of the rate set these used.
    repository = RateSetRepository(db_session)
    original = repository.get("poc-machine-shop", "1.0.0")
    repository.add(
        original.model_copy(
            update={
                "version": "9.0.0",
                "materials": {
                    "steel": MaterialRate(name="Steel", cost_per_kg=999.0, density_kg_m3=7850)
                },
            }
        )
    )
    db_session.commit()
    assert repository.get("poc-machine-shop").version == "9.0.0"

    assert client.get("/estimates").json() == listing_before
    assert (
        client.get(
            "/estimates/compare", params={"baseline": baseline_id, "comparison": comparison_id}
        ).json()
        == comparison_before
    )


def test_creating_a_new_estimate_does_not_alter_earlier_ones():
    first = create_estimate(part_id="P1")
    before = client.get(f"/estimates/{first}").json()

    create_estimate(part_id="P2", quantity=8)

    assert client.get(f"/estimates/{first}").json() == before


def test_comparison_reads_stored_totals_rather_than_recalculating(db_session):
    """Doctoring a stored total must show up in the comparison unchanged."""
    baseline_id = create_estimate(part_id="P1")
    comparison_id = create_estimate(part_id="P2")

    row = db_session.execute(
        select(EstimateRow).where(EstimateRow.estimate_id == comparison_id)
    ).scalar_one()
    row.total_part_cost = 4242.0
    db_session.commit()

    body = client.get(
        "/estimates/compare", params={"baseline": baseline_id, "comparison": comparison_id}
    ).json()

    assert body["total_part_cost"]["comparison"] == pytest.approx(4242.0)


# --- repository and isolation ----------------------------------------------


def test_repository_list_returns_a_page_object(db_session):
    create_estimate()

    page = EstimateRepository(db_session).list(limit=10, offset=0)

    assert page.total == 1
    assert page.limit == 10
    assert isinstance(page.items[0], EstimateSummary)


def test_repository_get_summary_rejects_an_unknown_id(db_session):
    with pytest.raises(ValueError, match="Unknown estimate"):
        EstimateRepository(db_session).get_summary("nope")


def test_history_does_not_leak_between_tests(db_session):
    """Many tests above write estimates; isolation means none are visible here."""
    assert db_session.execute(select(EstimateRow)).scalars().all() == []
    assert client.get("/estimates").json()["total"] == 0
